# Kube config for my homelab

## Cluster Setup

### Nodes

| Node | Role | IP | RAM |
|------|------|----|-----|
| k3s-controller | control-plane, master | 192.168.10.1 | 4 GB |
| k3s-node-1 | worker | 192.168.10.2 | - |
| k3s-node-2 | worker | 192.168.10.3 | - |
| k3s-node-3 | worker | 192.168.10.4 | - |

### k3s Installation

k3s was installed using the standard install script (https://docs.k3s.io/quick-start).

**Controller (k3s-controller):**
```
curl -sfL https://get.k3s.io | sh -
```

**Worker nodes (k3s-node-1, k3s-node-2, k3s-node-3):**
```
curl -sfL https://get.k3s.io | K3S_URL=https://k3s-controller:6443 K3S_TOKEN=<token> sh -
```

The token is found on the controller at `/var/lib/rancher/k3s/server/node-token`.

There is no `/etc/rancher/k3s/config.yaml` on any node -- all configuration is default.

### Controller Node Configuration

The controller is dedicated to running only the k3s server (API server, etcd, controller-manager, scheduler). No workloads should run on it.

**Taints:**
- `node-role.kubernetes.io/control-plane:NoExecute` -- evicts all pods that don't explicitly tolerate it
- `node.kubernetes.io/unschedulable:NoSchedule` -- from cordoning the node (`kubectl cordon k3s-controller`)

The `NoExecute` taint was applied manually:
```
kubectl taint nodes k3s-controller node-role.kubernetes.io/control-plane:NoExecute
```

**Important:** This taint is on the node object, not in k3s config. If k3s is reinstalled or the node is re-registered, you need to reapply it. To make it persistent, create `/etc/rancher/k3s/config.yaml` on the controller:
```yaml
node-taint:
  - "node-role.kubernetes.io/control-plane:NoExecute"
```

### DaemonSets on the Controller

Only these DaemonSets should run on the controller (they have tolerations for the `NoExecute` taint):

- `kube-prometheus-stack-prometheus-node-exporter` (monitoring namespace) -- node metrics
- `loki-promtail` (logging namespace) -- log collection

These tolerations were added via `kubectl patch`. If the Helm charts are upgraded, the tolerations may be lost and need to be re-added in the Helm values:

```
# node-exporter (prometheus stack helm values)
nodeExporter:
  tolerations:
    - key: node-role.kubernetes.io/control-plane
      operator: Exists
      effect: NoExecute

# promtail (loki helm values)
promtail:
  tolerations:
    - key: node-role.kubernetes.io/control-plane
      operator: Exists
      effect: NoExecute
```

---

### Dashboard

1. Follow instructions: https://kubernetes.io/docs/tasks/access-application-cluster/web-ui-dashboard/

### MetalLB

1. follow instructions: https://metallb.universe.tf/installation/
2. Then apply the configuration in `metallb-config.yml`

### NFS storage provisioner

- run `local-nfs-client`

### minecraft server

### Unifi

1. run ns.yml
1. run pvc.yml
1. run deployment.yml


