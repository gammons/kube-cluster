# Home assistant helm chart


Create via:
```
helm repo add pajikos http://pajikos.github.io/home-assistant-helm-chart/
helm repo update
helm install home-assistant pajikos/home-assistant --values values.yml -n home-assistant
```

Delete via
`helm uninstall -n home-assistant -f values.yml`
