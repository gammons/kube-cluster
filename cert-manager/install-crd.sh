# CRDs must be applied before the chart.
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.21.2/cert-manager.crds.yaml

helm upgrade --install \
  cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --version v1.21.2 \
  --set crds.enabled=false \
  --set crds.keep=true
