# PiHole installation

grab the helm chart:

    helm repo add mojo2600 https://mojo2600.github.io/pihole-kubernetes/
    helm repo update

then install it:

    helm install pihole mojo2600/pihole -f pihole-values.yml --namespace pihole


