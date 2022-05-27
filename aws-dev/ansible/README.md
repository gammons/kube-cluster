# Ansible Dev server

Used to provision a dev server.

To run:

```
ansible-playbook -i hosts --limit dev-server dev-server.yml
```

### Tailscale

Tailscale needs to be manually provisioned after it is installed.  

Run `sudo tailscale up` and follow the instructions to authenticate.
