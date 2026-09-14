# Metallb

The helm chart didn't seem to work for me, but the manifest intstall did:

```bash
kubectl apply -f https://raw.githubusercontent.com/metallb/metallb/v0.16.1/config/manifests/metallb-native.yaml
```

Then apply the metallb config:

```bash
kubectl apply -f metallb-config.yml
```

## Metrics moved in v0.16.0 — check Prometheus after upgrading

v0.16.0 **replaced kube-rbac-proxy with native TLS**. From the upstream release
note: *"The old HTTP endpoints are no longer available, they are now HTTPS served
by self-signed certificates."* Two things changed together:

| | v0.14.5 | v0.16.1 |
|---|---|---|
| metrics port | **7472** | **9120** |
| scheme | plain HTTP | **HTTPS, self-signed** |
| pod annotation | `prometheus.io/port: "7472"` | `prometheus.io/port: "9120"` |
| `prometheus.io/scheme` | absent | **still absent** |

So a scrape config still pointing at 7472 hits a closed port, and one reaching
9120 over HTTP hits a TLS listener. Either way MetalLB's metrics stop arriving
while MetalLB itself keeps working perfectly — LB IPs, L2 announcement and
Ingresses are all unaffected, so **nothing visible breaks and no alert fires.**

**v0.16.1 does not fix this for us.** It fixed exactly this problem, but only in
the **Helm chart** (emitting `scheme: https` on the chart's annotations,
PodMonitor and ServiceMonitor). We install from `metallb-native.yaml`, and that
manifest ships **no ServiceMonitor, no PodMonitor and no `prometheus.io/scheme`
annotation** — so whatever scrapes MetalLB here lives outside the manifest and
the upgrade does not touch it.

After upgrading, confirm metrics are still being ingested:

```bash
kubectl --context local-k3s exec -n monitoring prometheus-kube-prometheus-stack-prometheus-0 -c prometheus -- \
  wget -qO- 'http://localhost:9090/api/v1/query?query=metallb_k8s_client_config_loaded_bool' | \
  jq -r 'if (.data.result|length) == 0 then "NO DATA -- metallb metrics are not being ingested" else (.data.result[]|"\(.metric.job // "?") => \(.value[1])") end'
```

To fix: point the scrape config at port **9120** with `scheme: https` and
`insecureSkipVerify: true` — the certificate is self-signed, so verification
cannot succeed without wiring in a CA.
