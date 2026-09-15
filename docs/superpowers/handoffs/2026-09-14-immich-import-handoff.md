# Immich — handoff for resuming on another machine

Date: 2026-09-14
Cluster: `local-k3s`
Repo: `git@github.com:gammons/kube-cluster.git`, branch `master` (direct commits, no feature branch)

---

## TL;DR — where this stands

Immich **is deployed and healthy**. The Google Takeout export **is fully downloaded** (552.4 GiB)
onto the NAS. Nothing has been imported yet — 0 assets in the database.

The next milestone is the **import**, which is gated behind an integrity check that is
**already running as a Kubernetes Job right now** and will finish without supervision.

| Phase | State |
|---|---|
| Tasks 1–8 (deploy, storage, DB, Helm, Tailscale, pg_dump, alerts, Velero) | **complete, reviewed** |
| pgdump-into-Velero fix | **complete, proven** |
| Task 10 (Takeout ingest) | **redesigned + downloads complete** |
| Task 11 prep (two-pass import documented) | **complete, not run** |
| Integrity check | **RUNNING** as job `immich-takeout-verify` |
| Task 11 (the actual import) | **not started** |
| Task 12 (re-enable ML) | not started |
| Task 9 (README runbook) | **not started** |

---

## Prerequisites on the new machine

1. **Clone the repo** and confirm you are on `master`. Everything is pushed; `origin/master`
   == local `HEAD`.
2. **kubectl with the `local-k3s` context.** Copy the kubeconfig entry for `local-k3s`
   (server `192.168.10.1`).
3. **helm** (for any `helm upgrade`), and optionally the `velero` CLI.
4. **Tailscale**, only if you want to reach the Immich web UI at
   `http://immich.rya-scala.ts.net:2283`. Not required for the import.

**You do NOT need an NFS mount.** That existed only so the browser could write downloads
onto the NAS. The remaining work runs entirely as in-cluster Jobs against PVC
`immich-takeout`, which every cluster node can already reach.

**Read these three files before doing anything:**
- Spec (binding authority): `docs/superpowers/specs/2026-09-12-immich-homelab-design.md`
- Plan: `docs/superpowers/plans/2026-09-12-immich-homelab.md`
- Full ledger of every decision, ruling and trap: `docs/superpowers/handoffs/2026-09-14-immich-ledger.md`

---

## THE SINGLE MOST IMPORTANT RULE

**Every `kubectl` and `helm` command must be scoped to `local-k3s`:**

```bash
kubectl --context local-k3s ...
helm --kube-context local-k3s ...
```

The shell's default context on the previous machine pointed at an **unrelated production
cluster**. An unscoped command is a serious error, not a style nit. Check your own default
before you start.

---

## What is deployed right now

```
namespace immich
  immich-server              Running   k3s-node-1   chart 0.13.2, image v3.2.0
  immich-machine-learning    Running   k3s-node-1   CPU-only (no GPU in cluster)
  immich-valkey              Running   k3s-node-1
  immich-postgres-0          Running   k3s-node-3   PG17 + VectorChord 1.1.1
```

- Access: Tailscale only — `immich.rya-scala.ts.net:2283`. No Ingress, nothing public.
- Admin account exists (`grant@grant.dev`). Secret `immich-api` exists and authenticates.
- DB: 1 user, **0 assets, 0 albums**.
- Backups: `immich-pgdump` CronJob at 04:00 daily, verified working end-to-end.
  Dumps are mounted read-only into `immich-server` at `/dumps` so Velero captures them.
- Alerts: 5 rules in `monitoring`, PrometheusRule `immich-alerts`.
- Velero: `immich` is in `velero-homelab-daily`. Library, takeout, ml-cache and postgres-data
  volumes are all excluded; only pgdump + valkey + k8s objects are captured.

### The data

PVC `immich-takeout` (RWX, `nfs-bulk`), 552.4 GiB total:
- **53 zip chunks** `takeout-20260913T100905Z-1-0NN.zip`, NN = 001–054 **except 036**
- **1 duplicate** `takeout-20260913T100905Z-1-049 (1).zip`
- **1 standalone video** `PXL_20250517_140916476-036.mp4` (19.5 GB, 4K HEVC, 40 min)

Slot 036 is the video, not a missing chunk — it exceeded the 10 GB zip split size so Google
served it raw. The set is complete.

---

## Resume here — step by step

### Step 1: read the integrity check result

A Job named `immich-takeout-verify` was launched before the handoff and runs unattended.

```bash
kubectl --context local-k3s get job immich-takeout-verify -n immich
kubectl --context local-k3s logs -n immich job/immich-takeout-verify --tail=100
```

It tests every zip's full payload with Info-ZIP `unzip -t` (not just the central directory),
ffprobes the MP4, and SHA256s both copies of chunk 049.

- **All archives OK** → proceed to Step 2.
- **Any archive CORRUPT** → that chunk must be re-downloaded from Google Takeout before
  importing. Note the links expire ~7 days from generation (generated 2026-09-13), so this
  is time-sensitive.
- **Job gone / never completed** → re-run it; the recipe is in plan Task 10.

### Step 2: resolve the duplicate chunk 049

Pass 1 of the import will hard-fail while both copies are present.

- If both passed integrity **and** their SHA256s match → delete `takeout-...-049 (1).zip`.
- If they differ, or either failed → **do not delete anything**; the differing one may be the
  only good copy. Investigate first.

### Step 3: fix valkey BEFORE importing

This is the most likely mid-import failure and it is cheap to prevent.

Verified live: valkey has a **hard 512Mi limit**, `maxmemory 0`, `maxmemory-policy noeviction`,
`appendonly no`. It will **not** self-evict — it gets OOMKilled, and because persistence is
RDB-only with `save 3600 1 / 300 100 / 60 10000`, an OOMKill loses up to 60 seconds of queue
writes. That means **queued BullMQ jobs vanish**, mid-import, across ~200k assets.

512Mi is what the approved spec specifies, so raising it is a **deliberate spec deviation** —
make the call consciously and record it. Options: raise the limit to 1–2Gi, or set
`maxmemory` below the cgroup cap with a policy so it degrades instead of dying.

### Step 4: dry run, then import (two passes)

Plan Task 11 has the full detail. Syntax below was verified against the real
immich-go v0.32.0 binary.

**Pass 1 — the 53 zips:**
```
immich-go upload from-google-photos \
  --server=http://immich-server.immich.svc.cluster.local:2283 \
  --api-key=$IMMICH_API_KEY --no-ui --concurrent-tasks=4 --on-errors=continue \
  [--dry-run] /takeout/*.zip
```

**Pass 2 — the standalone video:**
```
immich-go upload from-google-photos \
  --server=http://immich-server.immich.svc.cluster.local:2283 \
  --api-key=$IMMICH_API_KEY --no-ui --concurrent-tasks=1 \
  [--dry-run] /takeout/_standalone036
```

Pass 2 needs the video hardlinked next to its **extracted sidecar**, which lives at
`Takeout/Google Photos/Photos from 2025/PXL_20250517_140916476.mp4.supplemental-metadata.json`
inside **chunk 035**. This matters: the sidecar says `photoTakenTime` **14:49:44 UTC with GPS**,
while the filename says **14:09:16 with no GPS**. Using filename-date parsing would put the
video ~40 minutes off and silently drop its location.

Run `--dry-run` first on both passes and sanity-check the asset count against Google Photos.

### Step 5: verify the import

```bash
kubectl --context local-k3s exec -n immich immich-postgres-0 -- \
  psql -U immich -d immich -c 'SELECT count(*) FROM asset;'
kubectl --context local-k3s exec -n immich immich-postgres-0 -- \
  psql -U immich -d immich -c 'SELECT count(*) FROM album;'
```

Then spot-check in the UI: capture dates must be **historical, not the import date** — that is
the classic Takeout failure. A zero album count means sidecars were not parsed, which defeats
the point of using immich-go.

### Step 6: Task 12 — re-enable ML

Currently `clip`, `facialRecognition`, `ocr` and `duplicateDetection` are **all disabled** so
the import isn't competing with CPU-only inference. Re-enable in `immich/values.yml`, then
`helm upgrade`, then **`kubectl rollout restart deploy/immich-server`** (see traps below).

Expect **1–3 days** of CPU-only background processing for a library this size.

### Step 7: Task 9 — the README runbook

Not started. `immich/README.md` currently holds only the disk-type rationale and the
secret-retrieval command. **Extend it, do not overwrite it.** Must-include list is in the
ledger; the most important gap is that the **DB restore procedure has never been written or
rehearsed**. Velero restores the `.sql.gz` to a PVC but nothing loads it into Postgres.

---

## Traps that already cost time — do not rediscover these

1. **`kubectl` default context is a production cluster.** Always `--context local-k3s`.
2. **`helm upgrade` does NOT restart immich-server.** Chart 0.13.2 puts no config checksum on
   the pod template, so a config-only change updates the ConfigMap while the process keeps
   running the old values. Always follow with `rollout restart deploy/immich-server`.
3. **Immich v3.2.0 renamed DB tables to singular** — `asset`, `album`, `"user"` (quoted).
   Not `assets`/`albums`.
4. **The library mounts at `/data`**, not `/usr/src/app/upload` (which doesn't exist in v3).
5. **`/bin/sh` in `ghcr.io/immich-app/postgres` is dash.** `set -o pipefail` is a special-builtin
   failure that **aborts the script on line 1**. Use `/bin/bash`. This silently broke the backup
   CronJob until caught.
6. **There is no official immich-go container image.** Download the release tarball
   (v0.32.0, SHA256 `6e2ad86bafdadb9466d6515de7cb882726c0aea1a21d51164dff361d7d480a97`)
   into `debian:bookworm-slim`.
7. **busybox `unzip` cannot read zip64** — useless on 10 GB archives. Use Info-ZIP.
8. **Any pod mounting `immich-takeout` MUST carry**
   `backup.velero.io/backup-volumes-excludes: takeout` on its **pod template**. The `immich`
   namespace is in the Velero schedule; without it a nightly backup tries to upload 1.5 Ti at
   ~2 MB/s.
9. **Never `2>/dev/null` a verification command.** A sidecar scan using `unzip -l ... 2>/dev/null`
   reported "zero matches across 54 archives" when the real cause was that `unzip` wasn't
   installed. A check that can't distinguish "no results" from "tool missing" is not a check.
10. **`local-path` PVs are not expandable and use `reclaimPolicy: Delete`.** Growing
    `immich-postgres-data` (50Gi) later is destructive — delete/recreate plus restore from dump.
11. **The library disk is a Seagate ST8000DM004 (SMR)**, ~15 ms avg read wait. Large sequential
    writes are fine; the 400k+ small thumbnail writes are SMR's worst case. Expect thumbnail
    generation to be slower than estimates suggest.
12. **Another agent session has been committing to this repo concurrently** (k3s upgrade,
    Proxmox/ZFS monitoring). It once swept unrelated files into its own commit. **Stage by
    explicit path — never `git add -A` or `git add .`.**

---

## Open decisions for the human

- **Cloud backup of the photo library.** Priced out: AWS Glacier Deep Archive ≈ $12/yr per TB
  to store but ~$86 to restore (egress); Backblaze B2 ≈ $83/yr per TB with free egress up to
  3× stored. Currently **nothing** backs up the library — Google Photos is the backup by
  design. This becomes important **if photos are ever deleted from Google Photos**, at which
  point the only copy lives on a single non-redundant 8 TB SMR drive.
- **`dev-box` Velero exclusion is missing** (pre-existing, unrelated to Immich). `velero/README.md`
  documents excludes for `docker`, `pmbot-raw-ro`, `pmbot-curated-ro`, but the annotation is not
  on the pod. ~120 GB/night is uploaded that was meant to be skipped, and `velero-homelab-daily`
  has been `PartiallyFailed` as a result. The human said they would handle this — **do not touch it.**
- **k3s 1.29 is EOL** (since 2025-02-28). A separate upgrade plan already exists at
  `docs/superpowers/plans/2026-09-13-k3s-cluster-upgrade-phases-0-4.md`. Immich was deliberately
  built to survive that upgrade unchanged (plain Postgres StatefulSet, not CloudNativePG).

---

## Suggested prompt for the next agent

> Resume the Immich deployment on the `local-k3s` cluster. Read, in this order:
> `docs/superpowers/handoffs/2026-09-14-immich-import-handoff.md`,
> `docs/superpowers/specs/2026-09-12-immich-homelab-design.md`,
> `docs/superpowers/plans/2026-09-12-immich-homelab.md`, and the prior decision log at
> `docs/superpowers/handoffs/2026-09-14-immich-ledger.md`.
>
> Immich is deployed and healthy; the 552 GiB Google Takeout export is fully downloaded to PVC
> `immich-takeout`; nothing has been imported yet. Start by reading the result of the
> already-running Job `immich-takeout-verify` in namespace `immich`, then continue from
> "Resume here" Step 2 in the handoff.
>
> Every kubectl/helm command must use `--context local-k3s` / `--kube-context local-k3s` — the
> default context is an unrelated production cluster. Another agent session may be committing to
> this repo, so stage files by explicit path and never `git add -A`.
>
> Use superpowers:subagent-driven-development to execute remaining plan tasks. Start a fresh
> ledger and note that it continues from the handoff document.
