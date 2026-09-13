#!/usr/bin/env bash
# The chart runs with crds.enabled=false, so these two commands are not
# independent: the apply below is the *only* thing that installs the CRDs. If it
# fails and the helm upgrade still runs, the result is a cert-manager with a
# live, cluster-wide ValidatingWebhookConfiguration and no CRDs behind it --
# every Certificate and Issuer admission in the cluster then fails. Stop on the
# first error rather than proceeding to make that state.
set -euo pipefail

# CRDs must be applied before the chart.
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.21.2/cert-manager.crds.yaml

helm upgrade --install \
  cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --version v1.21.2 \
  --set crds.enabled=false \
  --set crds.keep=true
