simple helm chart installation of jellyfin.

relies on existing PVC to be created (see pvc.yml)

to install:

helm install jellyfin jellyfin/jellyfin -n jellyfin -f values.yml

