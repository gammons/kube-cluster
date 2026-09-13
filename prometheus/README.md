# Monitoring

## What is actually running

The live stack is **`kube-prometheus-stack`** (chart `81.5.0`) in the **`monitoring`**
namespace, release name `kube-prometheus-stack`. It bundles Prometheus, Alertmanager,
Grafana, node-exporter and kube-state-metrics.

> The `## Legacy` section further down documents the older standalone
> `prometheus-community/prometheus` chart in an `observability` namespace, along with
> `prom-values.yml` and `alertmanager-values.yml`. **That is not what is deployed.**
> Those files are kept for reference only.

| File | Purpose |
|---|---|
| `kube-prometheus-stack-values.yml` | Helm values for the live release |
| `additional-scrape-configs.yml` | Contents of the `additional-scrape-configs` Secret (external targets: OVH/production via Tailscale, plus the Proxmox host) |
| `proxmox-host-alerts.yml` | `PrometheusRule` for ZFS pool health, SMART/NVMe wear, host reachability |

### Upgrade

```sh
helm upgrade kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n monitoring --version 81.5.0 -f kube-prometheus-stack-values.yml
```

Always pin `--version`. An unpinned upgrade will also move the chart.

### The Slack webhook is not in this repo

Alertmanager reads it from a mounted Secret via `global.slack_api_url_file`, so the
values file is safe to commit. The Secret is **not** in git and must exist before the
release is installed or upgraded:

```sh
kubectl create secret generic alertmanager-slack-webhook -n monitoring \
  --from-literal=slack-api-url='https://hooks.slack.com/services/...'
```

It is mounted via `alertmanager.alertmanagerSpec.secrets` at
`/etc/alertmanager/secrets/alertmanager-slack-webhook/slack-api-url`.
Use `--from-literal` (no trailing newline) — a newline in the value corrupts the URL.

### Alert routing

Prometheus tags everything `cluster: homelab` via `externalLabels`. Routing sends
`homelab` **critical** and **warning** to the `slack-alerts` receiver; `info` and
unlabelled severities go to `null`.

Route order matters — Alertmanager takes the **first** match, so the `slack-alerts`
routes must precede the `null` catch-all. Getting this wrong silently discards
everything, which is exactly what happened before 2026-09-12: every homelab alert was
being dropped, which is why a `DEGRADED` zpool went unnoticed for weeks.

### Scrape config changes

`additional-scrape-configs.yml` is stored in a Secret, not mounted from this repo.
After editing, apply it:

```sh
kubectl create secret generic additional-scrape-configs -n monitoring \
  --from-file=prometheus-additional.yaml=additional-scrape-configs.yml \
  --dry-run=client -o yaml | kubectl apply -f -
```

Prometheus picks it up within a minute or so; confirm with the `proxmox-host` target
showing `health=up` under Status → Targets.

### Known gap

Prometheus runs in VMs hosted by the Proxmox box it monitors, so it cannot alert on
total host failure. See `../proxmox/README.md`.

---

## Legacy

Everything below refers to the older standalone charts in the `observability`
namespace. Kept for reference; not the live deployment.

## Prometheus install

```
helm install -n observability -f prom-values.yml prometheus prometheus-community/prometheus
```

**updating the prom config:**
helm upgrade -n observability -f prom-values.yml prometheus prometheus-community/prometheus


Remember - prometheus scrapes external service!  See scrape_configs in prom-values.yml

## Updating prom config

helm upgrade -n observability -f prom-values.yml prometheus prometheus-community/prometheus

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

## truelist exporter

- make sure you run the `truelist-prometheus-exporter-service.yml` so that a service gets created from the tailnet node that exports prom metrics for truelist prod
