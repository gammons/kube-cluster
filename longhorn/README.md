# Longhorn setup

1. Install via helm https://longhorn.io/docs/1.6.1/deploy/install/install-with-helm/
2. Set up the secret containing the AWS access keys https://longhorn.io/docs/1.6.1/snapshots-and-backups/backup-and-restore/set-backup-target/
3. Set up the ingress (kc apply -f ingress.yaml)

Note: only use the ingress if you need to!  Otherwise there is a chance that longhorn UI controls will be exposed on a subdomain of grant.dev (e.g. internet-public!)
