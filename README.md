# vcsim model profiles

Build a realistic mock vCenter in [vcsim](https://github.com/vmware/govmomi/tree/main/vcsim) from a YAML profile.
`build_profile.py` drives vcsim through pyVmomi, so each VM carries the guest OS, firmware, boot, CBT and GPU settings you'd see from a real vCenter.

## `profiles/mixed-os-gpu-lab.yaml`

| Area | What's in it |
|------|--------------|
| Compute | `dc-lab`, with `cpu-cluster` (4 hosts, one in maintenance mode, DRS+HA) and `gpu-cluster` (2× A100 hosts, 1× L40S host, tagged via `gpu.model` / `gpu.count` custom fields) |
| Storage | 4 shared local datastores (SSD, capacity, GPU NVMe, templates) |
| Network | `dvs-lab` with VLAN-backed portgroups for mgmt, app, db, PXE and GPU RDMA |
| Guest OS | Ubuntu 20.04/22.04/24.04, RHEL 7/8/9, Windows 10/11, Server 2016/2019/2022/2025 |
| Boot | BIOS, BIOS with CD-ROM-first + boot delay + enter setup, EFI, EFI Secure Boot, Secure Boot + vTPM, EFI PXE-first with boot retry, EFI disk-only |
| CBT | `off`, `on`, `partial` (some disks `ctkEnabled=FALSE`), `stale` (VM CBT off but `ctkEnabled=TRUE` left in extraConfig) |
| Disks | thin / thick / eager-zeroed, independent persistent and non-persistent, PVSCSI / LSI / LSI-SAS / NVMe controllers |
| GPU | NVIDIA vGPU (`grid_a100-*c`, `nvidia_l40s-*q`) and DirectPath passthrough (A100 ×4, L40S), with 64-bit MMIO and locked memory reservation |
| State | powered on / off / suspended, snapshot chains (up to 3 deep), 4 templates |

33 VMs in total. Every VM gets `os.family`, `boot.profile`, `cbt.mode`, `gpu.profile` and `app.role` custom fields, so tests can assert what they expect to see.

## Usage

```bash
pip install -r requirements.txt
python build_profile.py profiles/mixed-os-gpu-lab.yaml --dry-run   # validate, no vcsim needed
```

Start an empty vcsim at API 8.0, then build into it:

```bash
vcsim -dc 0 -api-version 8.0.2.0 -l 127.0.0.1:8989
```

`-api-version` matters: vcsim defaults to 6.5, and pyVmomi silently drops types newer than the negotiated version (vTPM, dynamic GPU passthrough). The builder refuses to run against a version that's too old for the profile.

```bash
python build_profile.py profiles/mixed-os-gpu-lab.yaml
```

Defaults match vcsim's: `https://127.0.0.1:8989/sdk`, user `user`, password `pass`. Datastore folders are created under `%TEMP%/vcsim-lab` (override with `--datastore-root`).

vcsim in Docker sees its own filesystem, so mount a folder and point both flags at it:

```bash
docker run --rm -p 8989:8989 -v "$PWD/vcsim-ds:/vcsim-ds" vmware/vcsim -dc 0 -api-version 8.0.2.0 -l :8989
```

```bash
python build_profile.py profiles/mixed-os-gpu-lab.yaml --datastore-root /vcsim-ds --mkdir-at ./vcsim-ds
```

### Datastore paths

vcsim checks that every local datastore folder exists **where vcsim runs**. `--datastore-root` is that path as vcsim sees it. `--mkdir-at` is where this script creates the folders (it defaults to `--datastore-root`).

| vcsim runs in | Script runs in | Flags |
|---------------|----------------|-------|
| same OS (both WSL, or both Windows) | same | none |
| Windows (`vcsim.exe`) | WSL | `--datastore-root 'C:\vcsim-lab' --mkdir-at /mnt/c/vcsim-lab` |
| Docker, `-v "$PWD/vcsim-ds:/vcsim-ds"` | host | `--datastore-root /vcsim-ds --mkdir-at ./vcsim-ds` |

If the paths don't match, the build stops at the first datastore with a `vcsim rejected path` error.

### Snapshot the model for fast reloads

Building takes a few seconds; saving it lets CI start straight from the finished inventory:

```bash
govc object.save -u 'user:pass@127.0.0.1:8989' -k -d mixed-os-gpu-lab-model
```

```bash
vcsim -api-version 8.0.2.0 -load mixed-os-gpu-lab-model
```

## Writing a profile

- `presets` hold reusable `os`, `boot`, `cbt` and `gpu` settings; VMs reference them by name.
- `defaults` apply to every VM; any VM key overrides them.
- `count: N` with `{n:02d}` in `name` expands into N VMs.
- Quote `"on"` / `"off"` values — bare YAML `on`/`off` are booleans (the script maps them back, but quoting keeps it obvious).
- Re-running is safe: anything that already exists by name is skipped.

## Caveats

- vcsim has no real GPUs, TPM key provider or ISO; vGPU/passthrough devices, vTPM, CD-ROM and boot order go in after the VM is created, in separate reconfigures. If vcsim rejects one, or accepts it but the device isn't there afterwards, the build reports a warning and carries on.
- Reading `vm.runtime`, `vm.summary` or `vm.snapshot` with pyVmomi raises `AttributeError` against vcsim (it sends an empty `faultToleranceState` enum that pyVmomi can't parse). Read nested paths instead, e.g. a PropertyCollector on `runtime.powerState`, or `vm.layoutEx.snapshot`. govc and govmomi aren't affected.
- vcsim quirks the builder works around: an empty reference returned when a datastore is mounted on a second host, hosts passed at DVS creation are ignored, `bootOptions` replaced wholesale on reconfigure, and unpinned VMs placed on maintenance-mode hosts.
- Host hardware (cores, memory, PCI devices) comes from vcsim's built-in ESXi template; GPU hosts are identified by custom fields, not by real PCI inventory.
- `VirtualDisk.changeId` is only set by a real ESXi host, so CBT queries (`QueryChangedDiskAreas`) aren't meaningful against vcsim. Test against the CBT *configuration*.
