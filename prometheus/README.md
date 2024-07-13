## Prometheus install

```
helm install -n observability -f prom-values.yml prometheus prometheus-community/prometheus
```

**updating the prom config:**
helm upgrade -n observability -f prom-values.yml prometheus prometheus-community/prometheus


Remember - prometheus scrapes external service!  See scrape_configs in prom-values.yml

## Grafana install


```
helm install grafana grafana/grafana -f grafana-values2.yml -n observability
```

then you can get the url by looking at the printed stuff:

```
NAME: grafana
LAST DEPLOYED: Mon Jan 15 17:58:52 2024
NAMESPACE: observability
STATUS: deployed
REVISION: 1
NOTES:
1. Get your 'admin' user password by running:

   kubectl get secret --namespace observability grafana -o jsonpath="{.data.admin-password}" | base64 --decode ; echo


2. The Grafana server can be accessed via port 80 on the following DNS name from within your cluster:

   grafana.observability.svc.cluster.local

   Get the Grafana URL to visit by running these commands in the same shell:
     export NODE_PORT=$(kubectl get --namespace observability -o jsonpath="{.spec.ports[0].nodePort}" services grafana)
     export NODE_IP=$(kubectl get nodes --namespace observability -o jsonpath="{.items[0].status.addresses[0].address}")
     echo http://$NODE_IP:$NODE_PORT

3. Login with the password from step 1 and the username: admin
```

I only need a local grafana url

## Add prometheus as a data source

Set `http://prometheus-server` as the url

- ensure the correct ips in prom-values.yml for the truelist node health check
- communication is through tailscale.  no need to expose a port from AWS
