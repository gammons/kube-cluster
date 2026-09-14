#!/usr/bin/env bash
# ============================================================================
# THE CERT-MANAGER UPGRADE IS DEFERRED. THIS SCRIPT PINS WHAT IS *INSTALLED*.
# ============================================================================
#
# VERSION below is v1.14.5 because that is what is running on the cluster right
# now -- it is NOT a recommendation, a target, or a floor. The pin was moved back
# to it so that running this script is consistent with the deployed state instead
# of silently performing an upgrade nobody reviewed. A previous revision pinned
# v1.17.4; that number came out of reasoning that has since been discarded.
#
# cert-manager was removed from the scope of the k3s upgrade plan after the
# reasoning about it was found confidently wrong three separate times, each time
# recorded as settled. The verified facts are in
# ../docs/superpowers/specs/2026-09-12-k3s-cluster-upgrade-design.md under
# "cert-manager: removed from scope". Two that matter most here:
#
#   * 1.14 is NOT a blocker. It supports Kubernetes 1.24 -> 1.31, so it does not
#     gate the deferred k8s hops. The claim that its webhooks "fail against
#     1.30+" was false.
#   * The live CRDs are almost certainly UNPROTECTED. Chart v1.14.5 has no
#     crds/ directory; its CRDs live in templates/crds.yaml gated on
#     `installCRDs`, making them ordinary templated resources that Helm's pruner
#     will delete. There are ZERO `resource-policy` occurrences anywhere in the
#     1.14.5 chart, so the chart cannot have applied a `keep` annotation.
#
# BEFORE RAISING THIS PIN, VERIFY CRD OWNERSHIP FROM THE CHART -- NOT FROM
# RELEASE NOTES AND NOT BY INFERENCE. Concretely, and in this order:
#
#   1. Read the live annotations. This is the only thing that establishes what
#      protection actually exists today; the repo cannot tell you:
#        kubectl --context local-k3s get crd \
#          certificates.cert-manager.io certificaterequests.cert-manager.io \
#          issuers.cert-manager.io clusterissuers.cert-manager.io \
#          orders.acme.cert-manager.io challenges.acme.cert-manager.io \
#          -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.annotations.helm\.sh/resource-policy}{"\n"}{end}'
#      Also check whether they are in the release manifest at all:
#        helm --kube-context local-k3s get manifest cert-manager -n cert-manager \
#          | grep -c CustomResourceDefinition
#   2. Pull the TARGET chart and confirm how it renders CRDs, from source:
#        helm pull jetstack/cert-manager --version <target> --untar
#        ls cert-manager/crds/ ; grep -rn resource-policy cert-manager/
#   3. Apply the target's cert-manager.crds.yaml release manifest BEFORE the
#      chart upgrade. That file -- not `crds.keep=true` -- is what carries
#      `helm.sh/resource-policy: keep`. The ordering is load-bearing: an upgrade
#      that renders zero CRDs while the old manifest contained six prunes all
#      six, cascading to every Certificate, CertificateRequest, Issuer,
#      ClusterIssuer, Order and Challenge in the cluster.
#
# ---------------------------------------------------------------------------
# The chart runs with crds.enabled=false, so the two commands below are not
# independent: the apply is the *only* thing that installs the CRDs. If it fails
# and the helm upgrade still runs, the result is a cert-manager with a live,
# cluster-wide ValidatingWebhookConfiguration and no CRDs behind it -- every
# Certificate and Issuer admission in the cluster then fails. Stop on the first
# error rather than proceeding to make that state.
set -euo pipefail

# What is installed on this cluster today. See the header before changing it.
VERSION=v1.14.5

# CRDs must be applied before the chart. This release manifest carries
# helm.sh/resource-policy: keep on all 6 CRDs; the chart does not.
kubectl apply -f "https://github.com/cert-manager/cert-manager/releases/download/${VERSION}/cert-manager.crds.yaml"

# NOTE: `crds.enabled`/`crds.keep` were introduced in cert-manager 1.15. On the
# 1.14 chart pinned above the corresponding key is `installCRDs`, and these two
# --set flags are inert unknown values. They are kept so the invocation does not
# change shape when the pin is eventually raised, and because leaving
# `installCRDs` at its default of false is exactly the intent: the apply above
# owns the CRDs, not the chart.
helm upgrade --install \
  cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --version "${VERSION}" \
  --set installCRDs=false \
  --set crds.enabled=false \
  --set crds.keep=true
