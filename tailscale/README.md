# Tailscale operator

Installed from the Tailscale Helm repo into the `tailscale` namespace.

```sh
helm repo add tailscale https://pkgs.tailscale.com/helmcharts
helm upgrade --install tailscale-operator tailscale/tailscale-operator \
  -n tailscale --create-namespace --version <version> -f values.yml
```

## The OAuth credential is not in this repo

`values.yml` deliberately omits `oauth.clientId` / `oauth.clientSecret`. The
chart would render them into the `operator-oauth` Secret in plaintext, which
cannot be committed. That Secret is managed out-of-band and **must exist before
install or upgrade**:

```sh
kubectl create secret generic operator-oauth -n tailscale \
  --from-literal=client_id='...' --from-literal=client_secret='...'
```

### Protect the Secret before upgrading

`operator-oauth` is currently owned by the Helm release
(`app.kubernetes.io/managed-by: Helm`). The chart only renders it when
`oauth.clientId` is set — so upgrading with `values.yml`, which omits it, makes
the Secret disappear from the rendered manifest and **Helm will prune it**,
deleting the credential.

Annotate it to survive that, once, before the first upgrade with these values:

```sh
kubectl annotate secret operator-oauth -n tailscale \
  helm.sh/resource-policy=keep --overwrite
```

## What it does here

It manages annotation-driven **egress** proxies (`ts-*` StatefulSets) so this
cluster can scrape Prometheus metrics from the OVH and production clusters. See
the `ovh-*` and `production-*` jobs in
`../prometheus/additional-scrape-configs.yml`.

It does **not** serve LAN traffic — that is MetalLB's job, and the two are not
interchangeable.

`kubectl` access to this cluster does **not** depend on the operator; it goes
through the node's own `tailscaled`. Upgrading the operator cannot lock you out.

## Upgrading

The `ts-*` StatefulSets are recreated, so cross-cluster Prometheus targets flap
for a minute or two. Compare the `up` count for `cluster="ovh"` and
`cluster="production"` before and after rather than judging immediately.
