"""Build a vcsim inventory from a model profile (YAML).

    vcsim -dc 0 -api-version 8.0.2.0 -l 127.0.0.1:8989
    python build_profile.py profiles/mixed-os-gpu-lab.yaml
    python build_profile.py profiles/mixed-os-gpu-lab.yaml --dry-run

The run is re-entrant: anything that already exists by name is skipped.
Features vcsim may not fully emulate (CD-ROM, vTPM, GPU devices, boot order)
are applied in their own reconfigure after the VM is created, and a failure
there is reported as a warning instead of aborting the build.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim

SCSI = {
    "pvscsi": vim.vm.device.ParaVirtualSCSIController,
    "lsilogic": vim.vm.device.VirtualLsiLogicController,
    "lsilogic-sas": vim.vm.device.VirtualLsiLogicSASController,
    "nvme": vim.vm.device.VirtualNVMEController,
}
NIC = {
    "vmxnet3": vim.vm.device.VirtualVmxnet3,
    "e1000e": vim.vm.device.VirtualE1000e,
    "e1000": vim.vm.device.VirtualE1000,
}
DISK_MODES = {"persistent", "independent_persistent", "independent_nonpersistent"}
CBT_MODES = {"off", "on", "partial", "stale"}
POWER = {"on", "off", "suspended"}
GB_KB = 1024 * 1024
warnings: list[str] = []


def warn(msg: str) -> None:
    warnings.append(msg)
    print(f"  ! {msg}")


# ---------------------------------------------------------------- profile model

def flag(value) -> str:
    """YAML 1.1 turns bare on/off into booleans; map them back."""
    if value is True:
        return "on"
    if value is False:
        return "off"
    return str(value)


def expand_vms(profile: dict) -> list[dict]:
    """Merge defaults, resolve presets and expand `count` into concrete VMs."""
    defaults, presets = profile.get("defaults", {}), profile["presets"]
    out = []
    for entry in profile["vms"]:
        for n in range(1, entry.get("count", 1) + 1):
            vm = {**defaults, **entry, "name": entry["name"].format(n=n)}
            try:
                vm["_os"] = presets["os"][vm["os"]]
                vm["_boot"] = presets["boot"][vm.get("boot", "bios")]
                vm["_cbt"] = flag(presets["cbt"][flag(vm.get("cbt", "off"))]["mode"])
                vm["_gpu"] = [presets["gpu"][g] for g in vm.get("gpu") or []]
            except KeyError as e:
                sys.exit(f"{vm['name']}: unknown preset {e}")
            vm["power"] = flag(vm.get("power", "on"))
            if vm["_cbt"] not in CBT_MODES:
                sys.exit(f"{vm['name']}: cbt mode must be one of {sorted(CBT_MODES)}")
            if vm["power"] not in POWER:
                sys.exit(f"{vm['name']}: power must be one of {sorted(POWER)}")
            for d in vm["disks"]:
                if d.get("mode", "persistent") not in DISK_MODES:
                    sys.exit(f"{vm['name']}: disk mode must be one of {sorted(DISK_MODES)}")
            out.append(vm)
    names = Counter(vm["name"] for vm in out)
    if dupes := [n for n, c in names.items() if c > 1]:
        sys.exit(f"duplicate VM names: {dupes}")
    return out


def unit_number(controller: str, index: int) -> int:
    # SCSI unit 7 is reserved for the controller itself
    return index if controller == "nvme" or index < 7 else index + 1


def boot_options(boot: dict, order=None) -> vim.vm.BootOptions:
    # vcsim replaces bootOptions wholesale on reconfigure, so always send every field
    return vim.vm.BootOptions(
        bootDelay=boot.get("boot_delay_ms", 0),
        enterBIOSSetup=boot.get("enter_setup", False),
        efiSecureBootEnabled=boot.get("secure_boot", False),
        bootRetryEnabled="boot_retry_delay_ms" in boot,
        bootRetryDelay=boot.get("boot_retry_delay_ms", 10000),
        bootOrder=order,
    )


def config_spec(vm: dict, nic_backing) -> vim.vm.ConfigSpec:
    """ConfigSpec for CreateVM: controller, disks, NICs, firmware, CBT, GPU prep."""
    os_, boot, cbt = vm["_os"], vm["_boot"], vm["_cbt"]
    ctl_type = os_.get("scsi", "pvscsi")
    prefix = "nvme0" if ctl_type == "nvme" else "scsi0"
    ds = vm["datastore"]

    ctl = SCSI[ctl_type](key=-100, busNumber=0)
    if ctl_type != "nvme":
        ctl.sharedBus = "noSharing"
    changes = [vim.vm.device.VirtualDeviceSpec(operation="add", device=ctl)]
    extra = {}

    for i, d in enumerate(vm["disks"]):
        prov = d.get("provisioning", "thin")
        unit = unit_number(ctl_type, i)
        disk = vim.vm.device.VirtualDisk(
            key=-200 - i,
            controllerKey=-100,
            unitNumber=unit,
            capacityInKB=d["size_gb"] * GB_KB,
            backing=vim.vm.device.VirtualDisk.FlatVer2BackingInfo(
                fileName=f"[{ds}]",
                diskMode=d.get("mode", "persistent"),
                thinProvisioned=prov == "thin",
                eagerlyScrub=prov == "eager_zeroed",
            ),
        )
        changes.append(vim.vm.device.VirtualDeviceSpec(operation="add", fileOperation="create", device=disk))
        if cbt in ("on", "stale"):
            extra[f"{prefix}:{unit}.ctkEnabled"] = "TRUE"
        elif cbt == "partial":
            extra[f"{prefix}:{unit}.ctkEnabled"] = "TRUE" if d.get("ctk", True) else "FALSE"

    for i, pg in enumerate(vm["nics"]):
        nic = NIC[os_.get("nic", "vmxnet3")](
            key=-300 - i,
            backing=nic_backing(pg),
            connectable=vim.vm.device.VirtualDevice.ConnectInfo(startConnected=True, allowGuestControl=True),
            addressType="generated",
        )
        changes.append(vim.vm.device.VirtualDeviceSpec(operation="add", device=nic))

    if cbt != "off":
        extra["ctkEnabled"] = "TRUE"
    if vm["_gpu"]:
        extra["pciPassthru.use64bitMMIO"] = "TRUE"
        extra["pciPassthru.64bitMMIOSizeGB"] = str(128 if len(vm["_gpu"]) == 1 else 256 if len(vm["_gpu"]) == 2 else 512)

    return vim.vm.ConfigSpec(
        name=vm["name"],
        guestId=os_["guest_id"],
        version=os_.get("hw_version"),
        numCPUs=vm["cpu"],
        numCoresPerSocket=vm["cores_per_socket"],
        memoryMB=vm["memory_mb"],
        memoryReservationLockedToMax=bool(vm["_gpu"]) or None,
        files=vim.vm.FileInfo(vmPathName=f"[{ds}]"),
        firmware=boot.get("firmware", "bios"),
        bootOptions=boot_options(boot),
        changeTrackingEnabled=cbt in ("on", "partial"),
        extraConfig=[vim.option.OptionValue(key=k, value=v) for k, v in extra.items()],
        annotation=f"profile={vm.get('_profile', '')} os={vm['os']} boot={vm.get('boot', 'bios')} cbt={cbt}",
        deviceChange=changes,
    )


def gpu_device(gpu: dict, key: int) -> vim.vm.device.VirtualPCIPassthrough:
    if gpu["type"] == "vgpu":
        backing = vim.vm.device.VirtualPCIPassthrough.VmiopBackingInfo(vgpu=gpu["profile"])
    else:
        backing = vim.vm.device.VirtualPCIPassthrough.DynamicBackingInfo(
            deviceName="",
            customLabel=gpu.get("label", ""),
            allowedDevice=[vim.vm.device.VirtualPCIPassthrough.AllowedDevice(
                vendorId=gpu["vendor_id"], deviceId=gpu["device_id"])],
        )
    return vim.vm.device.VirtualPCIPassthrough(key=key, backing=backing)


# ------------------------------------------------------------------- inventory

def child(folder, name: str):
    return next((c for c in folder.childEntity if c.name == name), None)


def wait(task, timeout: float = 300):
    """Poll a task to completion and return its result.

    Not pyVim.task.WaitForTask: while a vcsim task is running it reports
    info.name as a method pyVmomi can't resolve (UnknownManagedMethod), and
    WaitForTask crashes dereferencing it.
    """
    deadline = time.monotonic() + timeout
    while (info := task.info).state in ("queued", "running"):
        if time.monotonic() > deadline:
            raise TimeoutError(f"task {info.key} still {info.state} after {timeout}s")
        time.sleep(0.1)
    if info.state == "error":
        raise info.error
    return info.result


class Builder:
    def __init__(self, si, profile: dict, ds_root: str, mkdir_at: str | None):
        self.si, self.content = si, si.RetrieveContent()
        self.profile, self.inv = profile, profile["inventory"]
        self.ds_root, self.mkdir_at = ds_root, mkdir_at
        self.clusters: dict[str, vim.ClusterComputeResource] = {}
        self.hosts: dict[str, vim.HostSystem] = {}
        self.portgroups: dict[str, vim.dvs.DistributedVirtualPortgroup] = {}
        self.fields: dict[tuple[str, type], int] = {}
        self.placed: Counter = Counter()

    def run(self, vms: list[dict]) -> None:
        self.check_api_version(vms)
        self.datacenter()
        self.custom_fields()
        self.compute()
        self.datastores()
        self.network()
        self.folders()
        for vm in vms:
            self.vm(vm)

    def check_api_version(self, vms: list[dict]) -> None:
        """pyVmomi silently downgrades types newer than the negotiated API (vcsim defaults to 6.5):
        VirtualTPM is dropped and dynamic passthrough backing becomes a plain DeviceBackingInfo."""
        have = self.content.about.apiVersion
        need = max([(6, 7)] * any(vm["_boot"].get("vtpm") for vm in vms)
                   + [(7, 0)] * any(g["type"] == "passthrough" for vm in vms for g in vm["_gpu"]), default=None)
        if need and tuple(int(x) for x in have.split(".")[:2]) < need:
            sys.exit(f"vcsim speaks API {have}, but this profile uses vTPM/GPU passthrough which need "
                     f"{need[0]}.{need[1]}+. Restart vcsim with: vcsim -dc 0 -api-version 8.0.2.0")

    def datacenter(self) -> None:
        name = self.inv["datacenter"]
        self.dc = child(self.content.rootFolder, name) or self.content.rootFolder.CreateDatacenter(name=name)
        print(f"datacenter {name}")

    def custom_fields(self) -> None:
        cfm = self.content.customFieldsManager
        existing = {(f.name, f.managedObjectType): f.key for f in cfm.field or []}
        for kind, mo_type in (("host", vim.HostSystem), ("vm", vim.VirtualMachine)):
            for name in self.inv.get("custom_fields", {}).get(kind, []):
                key = existing.get((name, mo_type))
                if key is None:
                    key = cfm.AddCustomFieldDef(name=name, moType=mo_type).key
                self.fields[(name, mo_type)] = key

    def set_fields(self, entity, mo_type, values: dict) -> None:
        for name, value in values.items():
            if value in (None, ""):
                continue
            key = self.fields.get((name, mo_type))
            if key is None:
                warn(f"{entity.name}: custom field '{name}' not declared in inventory.custom_fields")
                continue
            self.content.customFieldsManager.SetField(entity=entity, key=key, value=str(value))

    def compute(self) -> None:
        for c in self.inv["clusters"]:
            cluster = child(self.dc.hostFolder, c["name"])
            if not cluster:
                spec = vim.cluster.ConfigSpecEx(
                    drsConfig=vim.cluster.DrsConfigInfo(enabled=c.get("drs", False)),
                    dasConfig=vim.cluster.DasConfigInfo(enabled=c.get("ha", False)),
                )
                cluster = self.dc.hostFolder.CreateClusterEx(name=c["name"], spec=spec)
            self.clusters[c["name"]] = cluster
            print(f"cluster {c['name']}")

            have = {h.name: h for h in cluster.host}
            for h in c["hosts"]:
                host = have.get(h["name"])
                if not host:
                    spec = vim.host.ConnectSpec(hostName=h["name"], userName="root", password="vcsim", force=True)
                    host = wait(cluster.AddHost_Task(spec=spec, asConnected=True))
                self.hosts[h["name"]] = host
                self.set_fields(host, vim.HostSystem, h.get("fields", {}))
                if h.get("maintenance") and not host.runtime.inMaintenanceMode:
                    wait(host.EnterMaintenanceMode_Task(timeout=0))
                print(f"  host {h['name']}{' (maintenance)' if h.get('maintenance') else ''}")

            pools = {p.name: p for p in cluster.resourcePool.resourcePool}
            for name in c.get("resource_pools", []):
                if name not in pools:
                    alloc = lambda: vim.ResourceAllocationInfo(  # noqa: E731
                        reservation=0, expandableReservation=True, limit=-1,
                        shares=vim.SharesInfo(level="normal", shares=0))
                    cluster.resourcePool.CreateResourcePool(
                        name=name, spec=vim.ResourceConfigSpec(cpuAllocation=alloc(), memoryAllocation=alloc()))
                print(f"  pool {name}")

    def datastores(self) -> None:
        for d in self.inv["datastores"]:
            path = f"{self.ds_root.rstrip('/')}/{d['path']}"
            if self.mkdir_at is not None:
                Path(self.mkdir_at, d["path"]).mkdir(parents=True, exist_ok=True)
            for cname in d["clusters"]:
                for host in self.clusters[cname].host:
                    if any(ds.name == d["name"] for ds in host.datastore):
                        continue
                    try:
                        host.configManager.datastoreSystem.CreateLocalDatastore(name=d["name"], path=path)
                    except Exception as e:
                        # Mounting an existing datastore on another host succeeds in vcsim, but it returns
                        # an empty reference (<returnval type="">) that pyVmomi can't parse (KeyError '').
                        # So trust the host's datastore list, not the response.
                        if not any(ds.name == d["name"] for ds in host.datastore):
                            sys.exit(f"datastore {d['name']}: vcsim rejected path {path!r} on {host.name} "
                                     f"({getattr(e, 'msg', None) or e!r}).\n"
                                     "The folder must exist where vcsim runs (its host or container), not just "
                                     "where this script runs. Set --datastore-root to that path; see README "
                                     "'Datastore paths'.")
            print(f"datastore {d['name']} -> {path}")

    def network(self) -> None:
        net = self.inv["networks"]
        dvs = child(self.dc.networkFolder, net["dvs"])
        if not dvs:
            spec = vim.DistributedVirtualSwitch.CreateSpec(
                configSpec=vim.dvs.VmwareDistributedVirtualSwitch.ConfigSpec(name=net["dvs"]))
            dvs = wait(self.dc.networkFolder.CreateDVS_Task(spec))
        # vcsim ignores hosts passed at create time, and rejects DVS-backed NICs unless every
        # host in the VM's cluster is a member, so join them in a reconfigure.
        joined = set(dvs.summary.hostMember or [])
        if missing := [h for h in self.hosts.values() if h not in joined]:
            wait(dvs.ReconfigureDvs_Task(spec=vim.dvs.VmwareDistributedVirtualSwitch.ConfigSpec(
                configVersion=dvs.config.configVersion,
                host=[vim.dvs.HostMember.ConfigSpec(operation="add", host=h) for h in missing])))
        self.dvs = dvs
        print(f"dvs {net['dvs']}")

        have = {pg.name for pg in dvs.portgroup}
        new = [
            vim.dvs.DistributedVirtualPortgroup.ConfigSpec(
                name=pg["name"], type="earlyBinding", numPorts=64,
                defaultPortConfig=vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy(
                    vlan=vim.dvs.VmwareDistributedVirtualSwitch.VlanIdSpec(vlanId=pg["vlan"], inherited=False)))
            for pg in net["portgroups"] if pg["name"] not in have
        ]
        if new:
            wait(dvs.AddDVPortgroup_Task(new))
        self.portgroups = {pg.name: pg for pg in dvs.portgroup}
        for pg in net["portgroups"]:
            print(f"  portgroup {pg['name']} vlan {pg['vlan']}")

    def folders(self) -> None:
        self.vm_folders = {}
        for name in self.inv.get("vm_folders", []):
            self.vm_folders[name] = child(self.dc.vmFolder, name) or self.dc.vmFolder.CreateFolder(name)

    def nic_backing(self, pg_name: str):
        pg = self.portgroups[pg_name]
        return vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo(
            port=vim.dvs.PortConnection(switchUuid=self.dvs.uuid, portgroupKey=pg.key))

    # ---------------------------------------------------------------------- VMs

    def vm(self, spec: dict) -> None:
        folder = self.vm_folders.get(spec.get("folder"), self.dc.vmFolder)
        if child(folder, spec["name"]):
            print(f"vm {spec['name']} (exists)")
            return
        cluster = self.clusters[spec["cluster"]]
        pools = {p.name: p for p in cluster.resourcePool.resourcePool}
        pool = pools[spec["pool"]] if spec.get("pool") else cluster.resourcePool
        host = self.hosts[spec["host"]] if spec.get("host") else self.place(cluster)

        vm = wait(folder.CreateVM_Task(config=config_spec(spec, self.nic_backing), pool=pool, host=host))
        print(f"vm {spec['name']}")
        boot = spec["_boot"]

        if boot.get("cdrom"):
            ide = next((d for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualIDEController)), None)
            if ide is None:
                warn(f"{spec['name']}: no IDE controller for CD-ROM")
            else:
                cdrom = vim.vm.device.VirtualCdrom(
                    key=-600, controllerKey=ide.key, unitNumber=0,
                    backing=vim.vm.device.VirtualCdrom.RemotePassthroughBackingInfo(deviceName="", exclusive=False),
                    connectable=vim.vm.device.VirtualDevice.ConnectInfo(startConnected=True, allowGuestControl=True))
                self.reconfigure(vm, "CD-ROM", deviceChange=[vim.vm.device.VirtualDeviceSpec(operation="add", device=cdrom)])
        if boot.get("vtpm"):
            self.reconfigure(vm, "vTPM", deviceChange=[
                vim.vm.device.VirtualDeviceSpec(operation="add", device=vim.vm.device.VirtualTPM(key=-700))],
                expect=lambda devs: any(isinstance(d, vim.vm.device.VirtualTPM) for d in devs))
        if spec["_gpu"]:
            gpus = [gpu_device(g, -800 - i) for i, g in enumerate(spec["_gpu"])]
            backings = Counter(type(g.backing) for g in gpus)
            self.reconfigure(vm, "GPU", deviceChange=[
                vim.vm.device.VirtualDeviceSpec(operation="add", device=g) for g in gpus],
                expect=lambda devs: Counter(type(d.backing) for d in devs
                                            if isinstance(d, vim.vm.device.VirtualPCIPassthrough)) == backings)
        if boot.get("boot_order"):
            self.boot_order(vm, spec)

        self.set_fields(vm, vim.VirtualMachine, {
            "os.family": spec["_os"].get("family"),
            "boot.profile": spec.get("boot", "bios"),
            "cbt.mode": spec["_cbt"],
            "gpu.profile": ",".join(g.get("profile") or g.get("label", "") for g in spec["_gpu"]),
            "app.role": spec.get("role"),
        })

        for snap in spec.get("snapshots") or []:
            wait(vm.CreateSnapshot_Task(name=snap, description=f"{snap} (vcsim profile)", memory=False, quiesce=False))
        if spec.get("template"):
            vm.MarkAsTemplate()
        elif spec["power"] in ("on", "suspended"):
            wait(vm.PowerOnVM_Task())
            if spec["power"] == "suspended":
                wait(vm.SuspendVM_Task())

    def place(self, cluster) -> vim.HostSystem:
        """Least-loaded host not in maintenance mode; vcsim's own pick can land on one that is."""
        ready = [h for h in cluster.host if not h.runtime.inMaintenanceMode]
        if not ready:
            sys.exit(f"cluster {cluster.name}: every host is in maintenance mode")
        host = min(ready, key=lambda h: self.placed[h.name])
        self.placed[host.name] += 1
        return host

    def boot_order(self, vm, spec: dict) -> None:
        devices = vm.config.hardware.device
        first = lambda kind: next((d for d in devices if isinstance(d, kind)), None)  # noqa: E731
        order = []
        for item in spec["_boot"]["boot_order"]:
            if item == "disk" and (d := first(vim.vm.device.VirtualDisk)):
                order.append(vim.vm.BootOptions.BootableDiskDevice(deviceKey=d.key))
            elif item == "network" and (n := first(vim.vm.device.VirtualEthernetCard)):
                order.append(vim.vm.BootOptions.BootableEthernetDevice(deviceKey=n.key))
            elif item == "cdrom":
                order.append(vim.vm.BootOptions.BootableCdromDevice())
            else:
                warn(f"{spec['name']}: boot device '{item}' not present")
        self.reconfigure(vm, "boot order", bootOptions=boot_options(spec["_boot"], order))

    def reconfigure(self, vm, what: str, expect=None, **spec) -> None:
        """Apply an optional feature; `expect(devices)` catches changes vcsim accepts but drops."""
        try:
            wait(vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(**spec)))
        except Exception as e:  # vcsim gaps shouldn't sink the whole build
            warn(f"{vm.name}: {what} not applied ({getattr(e, 'msg', None) or e})")
            return
        if expect and not expect(vm.config.hardware.device):
            warn(f"{vm.name}: {what} accepted but not present on the VM afterwards")


# ------------------------------------------------------------------------ main

def summarize(vms: list[dict]) -> None:
    print(f"\n{len(vms)} VMs")
    for label, key in (("os", lambda v: v["os"]), ("boot", lambda v: v.get("boot", "bios")),
                       ("cbt", lambda v: v["_cbt"]), ("power", lambda v: "template" if v.get("template") else v["power"]),
                       ("gpu", lambda v: ("vgpu" if v["_gpu"][0]["type"] == "vgpu" else "passthrough") if v["_gpu"] else "none")):
        counts = Counter(key(v) for v in vms)
        print(f"  {label:6} " + ", ".join(f"{k}={n}" for k, n in sorted(counts.items())))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("profile", type=Path)
    ap.add_argument("--url", default="https://127.0.0.1:8989/sdk")
    ap.add_argument("--user", default="user")
    ap.add_argument("--password", default="pass")
    ap.add_argument("--datastore-root", default=str(Path(tempfile.gettempdir(), "vcsim-lab")),
                    help="directory vcsim sees as the parent of each local datastore")
    ap.add_argument("--mkdir-at", help="local dir to create datastore folders in "
                    "(default: --datastore-root; pass '' when vcsim runs elsewhere and you create them yourself)")
    ap.add_argument("--dry-run", action="store_true", help="validate the profile and build specs without connecting")
    args = ap.parse_args()

    profile = yaml.safe_load(args.profile.read_text(encoding="utf-8"))
    vms = expand_vms(profile)
    for vm in vms:
        vm["_profile"] = profile.get("name", args.profile.stem)

    if args.dry_run:
        for vm in vms:
            spec = config_spec(vm, lambda pg: vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(deviceName=pg))
            gpus = ",".join(g.get("profile") or g["label"] for g in vm["_gpu"]) or "-"
            print(f"{vm['name']:24} {spec.guestId:28} {spec.firmware:4} sb={spec.bootOptions.efiSecureBootEnabled!s:5} "
                  f"cbt={vm['_cbt']:7} disks={len(vm['disks'])} gpu={gpus}")
        summarize(vms)
        return

    mkdir_at = args.datastore_root if args.mkdir_at is None else (args.mkdir_at or None)
    url = urlparse(args.url)
    si = SmartConnect(host=url.hostname, port=url.port or 443, user=args.user, pwd=args.password,
                      disableSslCertValidation=True)
    try:
        Builder(si, profile, args.datastore_root, mkdir_at).run(vms)
    finally:
        Disconnect(si)
    summarize(vms)
    if warnings:
        print(f"\n{len(warnings)} warning(s):")
        for w in warnings:
            print(f"  {w}")


if __name__ == "__main__":
    main()
