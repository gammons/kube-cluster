# Uptime kuma

install via:

```bash
helm install uptime-kuma uptime-kuma/uptime-kuma --version 2.18.0 -n uptime-kuma --create-namespace -f values.yml
```

**Note:** Assumes an existing pvc named `uptime-kuma-pvc` is present in the same namespace

Nothing too special needed!  Just make sure uptime.grant.dev is pointed to the right IP address
