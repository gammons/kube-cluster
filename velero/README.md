# Velero backups

Backs up selected namespaces (and their PVC data) to S3 daily.

## What is backed up

Namespaces (all resources + PVC data via kopia filesystem backup):

- home-assistant
- dev-box
- openclaw
- openclaw-dottie
- openclaw-stonk

Schedule: daily at 06:00 UTC, backups expire after 7 days (TTL 168h).
Managed by the `velero-homelab-daily` Schedule created by the Helm chart.

Only PVCs mounted by pods get their data backed up; unmounted/orphaned
PVCs contribute nothing.

## Architecture

- Helm chart `vmware-tanzu/velero` (see `values.yml`), velero v1.18.1 + AWS plugin v1.14.2
- S3 bucket: `gammons-velero-homelab` (us-east-1), SSE-AES256, public access blocked
- IAM user: `velero-homelab`, inline policy `velero-s3-access` scoped to that bucket only
- No volume snapshots (local-path/nfs provisioners have no CSI snapshot support);
  `defaultVolumesToFsBackup: true` with the node-agent DaemonSet (kopia uploader)

## Setup (already done; for reference / rebuild)

1. Create bucket and secure it:

   ```
   aws s3api create-bucket --bucket gammons-velero-homelab --region us-east-1 --profile personal
   aws s3api put-public-access-block --bucket gammons-velero-homelab \
     --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true \
     --profile personal
   aws s3api put-bucket-encryption --bucket gammons-velero-homelab \
     --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' \
     --profile personal
   ```

2. Create IAM user with scoped inline policy (see velero-plugin-for-aws README for the
   policy doc; only the s3 actions are needed, ec2 snapshot actions are unused here).

3. Create access key, write it to `credentials-velero` in this directory
   (git-ignored, chmod 600):

   ```
   [default]
   aws_access_key_id=...
   aws_secret_access_key=...
   ```

4. Install:

   ```
   helm repo add vmware-tanzu https://vmware-tanzu.github.io/helm-charts
   helm install velero vmware-tanzu/velero --version 12.1.0 \
     --namespace velero --create-namespace \
     -f values.yml \
     --set-file credentials.secretContents.cloud=./credentials-velero
   ```

   The credentials file is passed with `--set-file` so the secret stays out of git.

5. velero CLI: installed at `~/bin/velero` (downloaded from the v1.18.1 GitHub release).

## Excluded volumes

The dev-box pod annotation `backup.velero.io/backup-volumes-excludes` skips
volumes that don't need backup:

```
docker,pmbot-raw-ro,pmbot-curated-ro
```

- `docker` -- dind image cache, rebuildable
- `pmbot-raw-ro` / `pmbot-curated-ro` -- 300GB+ of pmbot data the dev-box pod
  mounts read-only; pmbot is intentionally not backed up

**This annotation lives on the pod.** If dev-box is recreated, it is lost and
the next backup would re-ingest all of it. Add it permanently to the dev-box
StatefulSet pod template.

## Bandwidth throttle

Each kopia repo has an upload cap of ~2 MB/s so backups don't saturate the
uplink (Velero honors kopia's repo-stored throttle). To change it or apply it
to a newly created repo (e.g. openclaw-stonk after its first backup):

```
kubectl -n velero get secret velero-repo-credentials -o jsonpath='{.data.repository-password}' | base64 -d > pass
kopia repository connect s3 --bucket=gammons-velero-homelab \
  --prefix=kopia/<namespace>/ --password="$(cat pass)"
kopia repository throttle set --upload-bytes-per-second=<bytes>
kopia repository disconnect
```

## Important: repo password



Kopia's repository password is in the `velero-repo-credentials` secret (velero
namespace). Without it, S3 backup data is unrecoverable if the cluster is lost.
Save a copy somewhere safe (password manager):

```
kubectl -n velero get secret velero-repo-credentials -o jsonpath='{.data.repository-password}' | base64 -d
```

## Operations

```
velero backup get                    # list backups
velero schedule get                  # show schedule
velero backup logs <name>            # troubleshoot
velero restore create --from-backup <name> --include-namespaces home-assistant
```

## Monitoring

- Prometheus scrapes velero via a ServiceMonitor (enabled in `values.yml`).
- Alerts (`velero-alerts.yml`, PrometheusRule in `monitoring`):
  - `VeleroBackupFailed` / `VeleroBackupPartiallyFailed` -- any failure in the last 6h
  - `VeleroNoSuccessfulBackup` -- no successful daily backup in >26h
    (fires until the first scheduled backup completes)
- Grafana dashboard "Velero Backups" (Truelist folder) from `velero-dashboard.yml`:
  hours since last success, backup outcomes, duration/size, PVC volume backups.

Note: `velero_backup_last_successful_timestamp`, `velero_backup_duration_seconds`,
and `velero_backup_tarball_size_bytes` only appear after scheduled backups run.


Restore gotchas:

- Pod volume data restore (kopia DataDownload) only happens when the **pod** is
  included in the restore; restoring just PVCs creates empty volumes.
- home-assistant mounts the Zigbee USB dongle via hostPath -- avoid running a
  restored HA pod while the live one is up.

To browse backup data directly (e.g. on another machine), use kopia CLI with the
repo password and IAM credentials:

```
kopia repository connect s3 --bucket=gammons-velero-homelab \
  --prefix=kopia/<namespace>/ --password=<repo-password> --readonly
kopia snapshot list --all
```
