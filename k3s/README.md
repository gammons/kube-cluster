# k3s configuration

Single server (`k3s-controller`) with the default **SQLite** datastore — there is
no etcd, so there is no `etcd-snapshot` tooling. Backups come from Proxmox VM
snapshots; see `../proxmox/README.md`.

Agents join using `K3S_URL`/`K3S_TOKEN` persisted in
`/etc/systemd/system/k3s-agent.service.env`, so re-running the install script
during an upgrade preserves their join config.

## config.yaml

`config.yaml` belongs at `/etc/rancher/k3s/config.yaml` on the **controller**.
It is declarative and survives reinstalls, unlike the `ExecStart` line the
install script bakes into the systemd unit.

```sh
sudo mkdir -p /etc/rancher/k3s
sudo tee /etc/rancher/k3s/config.yaml < config.yaml
sudo systemctl restart k3s     # ~30-60s API outage
```

## Upgrading

One minor version at a time — the control plane does not support skipping minors.
Controller first, then agents.

```sh
# controller
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=vX.Y.Z+k3sN sh -
# agents (join config is preserved from k3s-agent.service.env)
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=vX.Y.Z+k3sN sh -
```

Always snapshot first: `../proxmox/snapshot-cluster.sh create pre-k3s-vX-Y-Z`.
