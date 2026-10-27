# 1. Use the image you want to clone as the base
FROM vmware/vcsim:latest

# 2. Set the directory inside the container where files will go (optional)
WORKDIR /app

# 3. Copy your local files into the image
# Syntax: COPY <path_on_your_computer> <path_inside_image>
COPY mixed-os-gpu-lab-model /data/mixed-os-gpu-lab-model

# Configure vcsim to load the inventory automatically on startup
# NOTE: CMD skip tls for render.com, since it strips out HTTPs before hitting the pod
ENTRYPOINT ["/vcsim"]
CMD ["-tls=false", "-load", "/data/mixed-os-gpu-lab-model", "-l", "0.0.0.0:8989"]

