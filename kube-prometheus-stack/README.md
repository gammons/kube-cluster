Experiments with kube-prometheus stack, which implements the prometheus-operator pattern for Kubernetes clusters, and is probably a good replacement for my current monitoring setup.

installed via:

`helm upgrade -i --create-namespace -n monitoring kube-prometheus-stack prometheus-community/kube-prometheus-stack -f values.yaml`

the values.yaml has one modification to enable ingress for grafana at grafana.local.grant.dev.

