# Metabase

## prereq: set up mariadb

helm install mariadb oci://registry-1.docker.io/bitnamicharts/mariadb --values mariadb-values.yml -n metabase

## Install helm chart:

Use the metabase helm chart:

https://artifacthub.io/packages/helm/pmint93/metabase

```bash
helm install metabase pmint93/metabase -n metabase --create-namespace -f values.yml
```
