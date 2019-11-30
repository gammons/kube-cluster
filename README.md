# Kube config for my homelab

### MetalLB

First install metalLB: `kubectl apply -f https://raw.githubusercontent.com/google/metallb/v0.8.3/manifests/metallb.yaml`

Then apply the configuration in `metallb-config.yml`

### NFS storage provisioner

`helm install stable/nfs-client-provisioner --set nfs.server=192.168.140.220 --set nfs.path=/nfs -g`

### ELK

Follow the README in the `elk/` directory
