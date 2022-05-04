# NFS server

setting up an NFS server in AWS, using a 100gb EBS volume
then, use tailscale to have the NFS server be part of the "local" network, sharing the EBS volume via NFS.

1. Follow instructions in the `terraform` dir to create the server
2. Provision the NFS server using the `ansible` dir

### Other resources

https://www.digitalocean.com/community/tutorials/how-to-set-up-an-nfs-mount-on-ubuntu-20-04

### Setting up the NFS provisioner on k8s

Follow the helm instructions here: https://github.com/kubernetes-sigs/nfs-subdir-external-provisioner
