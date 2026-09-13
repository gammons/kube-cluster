# cert-manager

`install-crd.sh` installs **v1.17.4**. CRDs are applied from the GitHub release
manifest *before* the chart, and the chart is told not to manage them
(`crds.enabled=false`, `crds.keep=true`) so a `helm uninstall` cannot destroy
live Certificates. The `installCRDs` flag this repo used previously was renamed
`crds.enabled` in cert-manager 1.15.

## Why v1.17.4 and not the newest release

The pin is bounded by the **cluster's** Kubernetes version, not by what
cert-manager has shipped (cert-manager.io/docs/releases, verified 2026-09-13):

| cert-manager | Supported Kubernetes | Upstream EOL |
|---|---|---|
| 1.21 | 1.33 → 1.36 | current |
| 1.17 | **1.29 → 1.33** | Oct 2025 (commercial LTS from Palo Alto Networks to Feb 2027) |
| 1.14 | 1.24 → 1.31 | Oct 2024 |

This cluster runs Kubernetes **1.29**, so 1.21 is four minors below its floor
and cannot be installed here at all. 1.17 is the newest release that supports
1.29, and it also covers every hop up to 1.33 — so it does not need touching
again until then.

**Never raise this pin ahead of the cluster's Kubernetes version.** 1.21 becomes
installable once the cluster reaches 1.33, which is also the last release 1.17
supports; that overlap at 1.33 is the only window in which to move between them.

The installed **v1.14.5** has been EOL since Oct 2024. That — not a version
ceiling — is the reason to upgrade: 1.14 supports Kubernetes up to 1.31, so it
is not blocking the first Kubernetes hops. Stopping at a release that is itself
upstream-EOL is a deliberate trade with a known horizon, recorded in
`../docs/superpowers/specs/2026-09-12-k3s-cluster-upgrade-design.md`.

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
