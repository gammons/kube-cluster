# immich

## Secrets

The Postgres password is generated at deploy time and lives only in the cluster.
Retrieve it with:

```bash
kubectl --context local-k3s get secret immich-postgres -n immich \
  -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d; echo
```

## Storage notes

`DB_STORAGE_TYPE=SSD` in `postgres.yml`. Reasoning, and its limits:

**Device.** node-3's `/var/lib/rancher/k3s/storage` is on the root filesystem:
`/dev/mapper/ubuntu--vg-ubuntu--lv` → `sda3` → `sda`.

**The rotational flag is not usable evidence.** `sda` reports `rotational=1`,
model `QEMU HARDDISK`. That is QEMU's default for an emulated disk when the
hypervisor does not set `rotation_rate=1`; it describes the emulated device, not
the media. Do not conclude `HDD` from it.

**Synthetic dd tests — discount these.** O_DIRECT reads of `/dev/sda` measured
~300us random 4k reads and 0.8-1.8 GB/s sequential. But the LV is 504GB with only
~133GB used, and those tests read at 100GB/300GB offsets and randomly across the
first 400GB — so most blocks touched were almost certainly never written. On
LVM-thin, sparse qcow2, or a ZFS zvol (all Proxmox defaults) reads of unallocated
blocks are served from metadata with no media access, and would produce these
same numbers on a spinning disk. Recorded only so nobody re-derives them and
mistakes them for proof.

**node_exporter counters — real allocated I/O.** Boot-lifetime block-layer
counters over genuine filesystem reads. No sparse blocks, no synthetic test:

| node       | reads   | read time | avg wait | avg req |
|------------|---------|-----------|----------|---------|
| k3s-node-3 | 95,827  | 70.563s   | 736us    | 61.3 kB |
| k3s-node-2 | 339,701 | 113.370s  | 334us    | 13.1 kB |

Sub-millisecond average wait across hundreds of thousands of real reads is not
achievable by a 7200rpm spindle (~8ms per seek). Re-check any time, read-only:

```bash
curl -s --get http://192.168.10.4:30090/api/v1/query --data-urlencode \
  'query=node_disk_read_time_seconds_total{device="sda"} / node_disk_reads_completed_total{device="sda"}'
```

**Why the choice is robust regardless.** `DB_STORAGE_TYPE` only sets
`random_page_cost`, `effective_io_concurrency` and `max_wal_size`. Whether the
media is NVMe or spindles behind a large flash/DRAM array cache, Postgres
observes sub-millisecond random reads and tolerates a deep queue either way. The
SSD profile matches the *observed* behaviour, which is what those three settings
model — so the decision holds even if the sparse-read caveat above applies in
full. (Postgres's own `random_page_cost=1.2` / `effective_io_concurrency=200` are
*not* evidence of anything: they are a deterministic function of the env var we
set, and only confirm it took effect.)

**Reversing it is cheap.** Change the env var in `postgres.yml` and restart the
pod: no data migration, no reinitialisation. Not a one-way door.
