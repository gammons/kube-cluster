# Metallb

The helm chart didn't seem to work for me, but the manifest intstall did:

```bash
kubectl apply -f https://raw.githubusercontent.com/metallb/metallb/v0.14.5/config/manifests/metallb-native.yaml
```

Then apply the metallb config:

```bash
kubectl apply -f metallb-config.yaml
```
