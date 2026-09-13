# immich

## Secrets

The Postgres password is generated at deploy time and lives only in the cluster.
Retrieve it with:

```bash
kubectl --context local-k3s get secret immich-postgres -n immich \
  -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d; echo
```

## Storage notes

`DB_STORAGE_TYPE=SSD` — node-3's `/var/lib/rancher/k3s/storage` sits on
`/dev/mapper/ubuntu--vg-ubuntu--lv` → `sda3` → `sda`. `sda` reports
`rotational=1` and model `QEMU HARDDISK`, but that is just the QEMU default the
hypervisor advertises. Measured from the node: 300 random 4k O_DIRECT reads took
0.22s against a 0.13s process-fork baseline (~300us/read, ~3300 IOPS at QD1) and
sequential O_DIRECT reads ran at 0.8-1.8 GB/s. A 7200rpm spindle cannot exceed
~120 IOPS or ~200 MB/s, so the backing Proxmox storage is flash.
