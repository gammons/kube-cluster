Be sure to install promtail as well - 

```
helm upgrade --install promtail grafana/promtail --set "loki.serviceName=loki" -n monitoring
```

...and then add loki as a data source for prometheus.  For me it is `http://loki-gateway.monitoring.svc.cluster.local`

