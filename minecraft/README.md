# Minecraft server info

Sets up a namespace, PVC, and minecraft server that utilizes those things

### Upgrading the server

It is self-updating, so simply delete the deployment and re-apply the `minecraft.yml` file to get the latest version

### Backups

The `db_backup` folder has a simple `backup.sh` that gets run via a K8s job.  It creates a gzipped tar archive and uploads it to an s3 bucket.  That bucket has a 30-day lifecycle policy.
