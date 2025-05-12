# Loki stack

reference chat convo:
https://chatgpt.com/g/g-p-67ee5c885c848191837281d7a833ba2c-kubernetes-homelab/c/67ee5dfd-b3d0-8001-a97e-40d4b6a87ec3

need to annotate the service to expose to tailscale: kubectl annotate service loki -n logging tailscale.com/expose="true"

### retention

sets retention to 7 days

```
helm upgrade loki grafana/loki-stack -n logging -f loki-retention-values.yaml
```


