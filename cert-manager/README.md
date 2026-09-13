# cert-manager

`install-crd.sh` installs **v1.21.2**. CRDs are applied from the GitHub release
manifest *before* the chart, and the chart is told not to manage them
(`crds.enabled=false`, `crds.keep=true`) so a `helm uninstall` cannot destroy
live Certificates. The `installCRDs` flag this repo used previously was renamed
`crds.enabled` in cert-manager 1.15.

**The webhook is cluster-wide.** If it is unhealthy, every Certificate and
Issuer admission fails, not just cert-manager's own. After any upgrade, prove
admission works rather than assuming:

```sh
kubectl apply -f - <<'EOF'
apiVersion: cert-manager.io/v1
kind: Issuer
metadata: {name: webhook-probe, namespace: default}
spec: {selfSigned: {}}
EOF
kubectl delete issuer webhook-probe -n default
```

follow the instructions for installing + configuring cert-manager carefully.

ensure you understand the `issuer`/`clusterIssuer` and `ingress` configurations correctly.
