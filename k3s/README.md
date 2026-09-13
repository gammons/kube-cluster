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
read -rs SUDO_PASSWORD          # keeps it off the command line and out of history

printf '%s\n' "$SUDO_PASSWORD" | \
  ssh grant@k3s-controller 'sudo -S -p "" mkdir -p /etc/rancher/k3s'

{ printf '%s\n' "$SUDO_PASSWORD"; cat k3s/config.yaml; } | \
  ssh grant@k3s-controller 'sudo -S -p "" tee /etc/rancher/k3s/config.yaml >/dev/null'

# ~30-60s API outage
printf '%s\n' "$SUDO_PASSWORD" | \
  ssh grant@k3s-controller 'sudo -S -p "" systemctl restart k3s'
```

`sudo` prompts for a password on these nodes, and one stdin stream carries both
it and the file: `sudo -S` reads the password from stdin and consumes exactly
the first line, so everything after that line is what `tee` writes. `-p ""`
suppresses the prompt string so it does not end up in the output.

The `{ ...; } |` grouping is not stylistic. Piping *and* redirecting into the
same `ssh` — `printf ... | ssh ... 'sudo -S ... tee ...' < k3s/config.yaml` —
only feeds both under zsh's MULTIOS; in `bash` and `sh` the redirect silently
wins, the password is discarded, and the config file is offered as the password.
Concatenating into a single stream is POSIX-portable.

Authenticating sudo interactively beforehand does **not** work instead. sudo
caches credentials with `timestamp_type=tty` by default (the option formerly
called `tty_tickets`), so an interactive session's ticket is bound to that tty
and a later `ssh host 'sudo …'` does not match it. With no terminal at all sudo
falls back to per-parent-process scoping, so each `ssh` gets its own record. It
prompts again either way. `scp` to a temp path then `sudo install` hits the same
wall.

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
