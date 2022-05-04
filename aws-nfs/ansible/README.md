# Ansible NFS server

Used to provision an NFS server.

To run:

```
ansible-playbook -i hosts --limit nfs-server nfs-server.yml
```

### Tailscale

Tailscale needs to be manually provisioned after it is installed.  

Run `sudo tailscale up` and follow the instructions to authenticate.
