# k3s configuration

Single server (`k3s-controller`) with the default **SQLite** datastore — there is
no etcd, so there is no `etcd-snapshot` tooling. Backups come from Proxmox VM
snapshots; see `../proxmox/README.md`.

Agents join using `K3S_URL`/`K3S_TOKEN` in
`/etc/systemd/system/k3s-agent.service.env`, but the installer never reads that
file — it picks server vs agent from the environment and rewrites the env file
from scratch. Both variables must therefore be supplied explicitly on every agent
upgrade; see [Upgrading](#upgrading).

## config.yaml

`config.yaml` belongs at `/etc/rancher/k3s/config.yaml` on the **controller**.
It is declarative and survives reinstalls, unlike the `ExecStart` line the
install script bakes into the systemd unit.

Run from the repo root on your workstation — the controller needs no checkout:

```sh
ssh grant@k3s-controller 'sudo mkdir -p /etc/rancher/k3s'
ssh grant@k3s-controller 'sudo tee /etc/rancher/k3s/config.yaml >/dev/null' < k3s/config.yaml
ssh grant@k3s-controller 'sudo systemctl restart k3s'   # ~30-60s API outage
```

`sudo` prompts for a password on these nodes, and the `tee` line's stdin is
already the config file, so it cannot also carry one. Authenticate sudo in an
interactive session first, or `scp` the file to a temp path and `sudo install` it.

## Upgrading

One minor version at a time — the control plane does not support skipping minors.
Controller first, then agents.

```sh
# controller
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=vX.Y.Z+k3sN sh -
# agents -- K3S_URL and K3S_TOKEN are REQUIRED. The installer picks server vs
# agent from the environment, not from what is already installed, and rewrites
# k3s-agent.service.env from scratch. Omitting them installs a SERVER here.
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=vX.Y.Z+k3sN \
  K3S_URL=https://k3s-controller:6443 K3S_TOKEN=<token> sh -
```

`<token>` lives on the controller at `/var/lib/rancher/k3s/server/node-token`
(`sudo cat` it). Omitting it on a worker does not fail loudly: the installer
derives the unit name from the mode it picked, so it installs, enables and starts
a second `k3s.service` in **server** mode alongside the untouched
`k3s-agent.service` — which is never restarted, so the agent is not upgraded.

Always snapshot first: `../proxmox/snapshot-cluster.sh create pre-k3s-vX-Y-Z`.
