#!/usr/bin/env bash
# The chart runs with crds.enabled=false, so these two commands are not
# independent: the apply below is the *only* thing that installs the CRDs. If it
# fails and the helm upgrade still runs, the result is a cert-manager with a
# live, cluster-wide ValidatingWebhookConfiguration and no CRDs behind it --
# every Certificate and Issuer admission in the cluster then fails. Stop on the
# first error rather than proceeding to make that state.
set -euo pipefail

# Pinned to v1.17.4, not to the newest cert-manager. This is bounded by the
# CLUSTER's Kubernetes version, not by what cert-manager has released:
#
#   cert-manager 1.21  supports Kubernetes 1.33 -> 1.36
#   cert-manager 1.17  supports Kubernetes 1.29 -> 1.33
#   cert-manager 1.14  supports Kubernetes 1.24 -> 1.31   (installed, EOL Oct 2024)
#
# (cert-manager.io/docs/releases, verified 2026-09-13.)
#
# This cluster is on Kubernetes 1.29, so 1.21 is four minors below its floor and
# must not be installed here. Raise this pin only alongside the Kubernetes
# version, and never ahead of it: 1.21 becomes installable once the cluster
# reaches 1.33, which is also the last release 1.17 supports.
#
# 1.17 is upstream-EOL (Oct 2025). Palo Alto Networks publish a commercial
# 1.17 LTS to Feb 2027. Stopping here is a decision with a horizon, not an
# oversight -- see ../docs/superpowers/specs/2026-09-12-k3s-cluster-upgrade-design.md.
VERSION=v1.17.4

# CRDs must be applied before the chart.
kubectl apply -f "https://github.com/cert-manager/cert-manager/releases/download/${VERSION}/cert-manager.crds.yaml"

helm upgrade --install \
  cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --version "${VERSION}" \
  --set crds.enabled=false \
  --set crds.keep=true
