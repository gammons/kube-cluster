# cert-manager

> ## The upgrade is DEFERRED. This pin reflects what is INSTALLED.
>
> `install-crd.sh` pins **v1.14.5**, which is what is running on the cluster
> right now. That is *not* a recommendation and not a target — it is there so
> that running the script is consistent with the deployed state rather than
> silently performing an unreviewed upgrade. A previous revision pinned v1.17.4;
> that number came from reasoning that has since been discarded.
>
> cert-manager was **removed from the scope** of the k3s upgrade plan after the
> reasoning about it was found confidently wrong three separate times, each time
> recorded as settled. The verified facts live in
> `../docs/superpowers/specs/2026-09-12-k3s-cluster-upgrade-design.md`, section
> "cert-manager: removed from scope".
>
> **Anyone raising this pin must first verify CRD ownership from the chart
> source** — not from release notes, and not by inference. See
> [Before raising the pin](#before-raising-the-pin) below. Getting this wrong
> deletes every Certificate, CertificateRequest, Issuer, ClusterIssuer, Order and
> Challenge in the cluster.

CRDs are applied from the GitHub release manifest *before* the chart, and the
chart is told not to manage them, so a `helm uninstall` cannot destroy live
Certificates. Note that `crds.enabled`/`crds.keep` were introduced in
cert-manager **1.15** — on the 1.14 chart pinned here the equivalent key is
`installCRDs`, and it defaults to `false`, which is the intent either way.

## Version support matrix

Verified against cert-manager.io/docs/releases on 2026-09-13:

| cert-manager | Supported Kubernetes | Upstream EOL | LTS |
|---|---|---|---|
| 1.21 | 1.33 → 1.36 | current | — |
| 1.18 | 1.29 → 1.33 | **Mar 2026 (already passed)** | none |
| 1.17 | 1.29 → 1.33 | Oct 2025 | commercial, Palo Alto Networks, **Feb 2027** |
| 1.14 | **1.24 → 1.31** | Oct 2024 | — |

Two corrections to claims this repo previously made, both load-bearing:

- **1.14 is not a blocker.** It supports Kubernetes up to **1.31**. The earlier
  claim that its webhooks "fail against a 1.30+ API server" was false, and it was
  false in the direction that made cert-manager look like a hard gate for the
  Kubernetes hops. It is not one.
- **1.17 is not "the newest release supporting 1.29".** 1.18 supports the same
  1.29 → 1.33 range. 1.17's only real advantage over 1.18 is the commercial LTS
  to Feb 2027; 1.18 reached EOL in Mar 2026 with no LTS at all. Choose between
  them on that basis, not on a support range that is identical.

**Never raise this pin ahead of the cluster's Kubernetes version.** That rule
still holds and still matters — 1.21 requires Kubernetes ≥1.33, so it cannot be
installed on this 1.29 cluster.

The installed **v1.14.5 has been EOL since Oct 2024**, so there is a real reason
to move eventually. It is a known, accepted gap, not an emergency: nothing in the
current plan depends on it, and 1.14 covers the cluster's Kubernetes version with
two minors to spare.

## Before raising the pin

Do these in order. Steps 1 and 3 require the live cluster; step 2 does not.

1. **Read what the live CRDs actually carry.** Nothing in this repo can tell you
   this, and the chart evidence below only proves the chart did not supply it:

   ```sh
   kubectl --context local-k3s get crd \
     certificates.cert-manager.io certificaterequests.cert-manager.io \
     issuers.cert-manager.io clusterissuers.cert-manager.io \
     orders.acme.cert-manager.io challenges.acme.cert-manager.io \
     -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.annotations.helm\.sh/resource-policy}{"\n"}{end}'

   # ...and whether Helm believes it owns them at all:
   helm --kube-context local-k3s get manifest cert-manager -n cert-manager \
     | grep -c CustomResourceDefinition
   ```

2. **Confirm from the chart source how CRDs are rendered**, for both the
   installed version and the target:

   ```sh
   helm pull jetstack/cert-manager --version <version> --untar
   ls cert-manager/crds/            # absent in 1.14.5
   grep -rn resource-policy cert-manager/   # zero hits in 1.14.5
   grep -n 'if .Values' cert-manager/templates/crds.yaml
   ```

   For **v1.14.5 this was verified**: there is no `crds/` directory, the six CRDs
   are in `templates/crds.yaml` gated on `{{- if .Values.installCRDs }}`, and
   there are **zero** `resource-policy` occurrences in the entire chart. They are
   ordinary templated resources, subject to Helm's pruner, with no `keep`
   annotation coming from the chart.

3. **Apply the target release's `cert-manager.crds.yaml` before the chart
   upgrade.** That file carries `helm.sh/resource-policy: keep` on all 6 CRDs
   (verified for v1.17.4: 6 CRDs, 6 annotations). `--set crds.keep=true` does
   **not** do this — it only annotates CRDs the chart itself renders, and with
   `crds.enabled=false` it renders none, so it is inert. The ordering is the
   protection, not bookkeeping.

**The failure mode this guards against:** if the live CRDs are in the release
manifest and unannotated, an upgrade that renders zero CRDs — which is exactly
what `--set crds.enabled=false` does — removes six resources that were in the old
manifest and absent from the new one. Helm prunes all six, and CRD deletion
cascades to every Certificate, CertificateRequest, Issuer, ClusterIssuer, Order
and Challenge in the cluster.

An earlier revision of this repo stated that "the chart never manages them". That
was **false for this release**, and it appeared twice as the reassuring version.

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
