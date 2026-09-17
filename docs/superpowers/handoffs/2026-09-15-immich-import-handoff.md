# Immich — handoff, import in flight

Date: 2026-09-15
Cluster: `local-k3s`
Repo: `git@github.com:gammons/kube-cluster.git`, branch `master` (direct commits)

**Continues from:** `2026-09-14-immich-import-handoff.md`
**Decision logs:** `2026-09-14-immich-ledger.md` (Tasks 1–10, rulings 1–17), then this
session's ledger at `.superpowers/sdd/progress.md` (rulings 18–20, git-ignored — read it
before it is lost to a `git clean`).

---

## UPDATE 2026-09-16 00:40Z — run 1 FAILED at 82%, run 2 launched

**Read this before the TL;DR below, which describes run 1 while it was still healthy.**

Pass 1 (`immich-go-import-zips`) **died at 19:22:45Z with exit code 1**, having imported
**98,459 of 119,624 assets (82.3%)** and 457 albums. `backoffLimit: 0`, so the Job did not
retry — condition `BackoffLimitExceeded`. **~21,165 assets were never uploaded.**

**The cause could not be determined**, and that is a process failure worth naming:
immich-go's stdout ends mid-write with no error and no summary, and its detailed ~95 MB
internal log lived in the container's writable layer, so it died with the pod — a
terminated container cannot be `exec`ed into. Its own counter showed upload errors jumping
70 → **481** shortly before the end. Server-side `RangeError [ERR_OUT_OF_RANGE] ...
Received -610950` entries exist but are in `MetadataService`/`MediaService` *background*
jobs that only began after the queues resumed, so they are a consequence of the exit
rather than obviously its cause.

**Good news — the post-import pipeline auto-started.** immich-go un-paused the queues on
its way out, so at 00:00Z:

| Queue | State | Waiting |
|---|---|---|
| `metadataExtraction` | drained | 0 |
| `thumbnailGeneration` | active | 63,574 |
| `storageTemplateMigration` | active | 17,897 |

`/data/thumbs` is **4.1 GB** with **11,132 thumbnails + 11,132 previews + 1,218 encoded
videos** generated. Thumbnails are appearing in the UI progressively.

### Two changes made in response

**1. Liveness probe loosened (`ffb09bf`).** The chart default was `timeoutSeconds: 1`,
`failureThreshold: 3`, `periodSeconds: 10` — 30 s of slow responses killed the container,
and it killed `immich-server` **four times** during run 1 (exit 143, "Liveness probe
failed: context deadline exceeded"), each time dropping the uploads in flight. A Node.js
event loop servicing concurrent uploads, hashing and DB writes cannot reliably answer
`/api/server/ping` within one second. Now `timeoutSeconds: 10`, `failureThreshold: 5`,
`periodSeconds: 30` — a kill requires ~150 s of genuine unresponsiveness. Verified live on
the new pod. `startupProbe` deliberately left alone (already 30 × 10 s).

**2. Run 2 launched: Job `immich-go-import-zips-2`** (manifest at
`.superpowers/sdd/scratch/pass1-rerun.yml`, transient so not committed to `import/`).
Differences from run 1:

- `--concurrent-tasks=2` instead of 4 — less server contention, fewer FK races.
- **immich-go's log now survives the pod.** The takeout PVC is mounted a second time at
  `/importlogs` via `subPath: _importlogs`, read-write, while `/takeout` itself stays
  **read-only** so the archives can never be altered. The script probes
  `--help` for a `--log-file` flag and uses it if present — it is
  (`-l, --log-file`), so the log writes straight to
  `/importlogs/run2-<stamp>-native.log` on NFS. An `EXIT`/`INT`/`TERM` trap plus a
  20-minute periodic copy back it up for the SIGKILL case no trap can catch.
- No `set -e`, so the job survives its own failure long enough to preserve evidence.

**immich-go dedupes by content hash**, so run 2 skips the 98,459 already imported and
uploads only the missing ~21,165. Expect a **long lead time** — it must re-read and
re-hash all 552 GiB before it reaches new work. `--pause-immich-jobs` (default true) will
re-pause the thumbnail queue for the duration and release it afterwards, which restores
the intended import-then-derive ordering with no manual queue juggling.

**If run 2 fails at the same point, that is diagnostic** — and this time the log will be
on the PVC at `/importlogs/`, readable from any pod that mounts `immich-takeout`.

---

## TL;DR (run 1, superseded by the update above)

**Pass 1 of the import is RUNNING right now** and healthy. All three pre-import gates
passed. It has roughly **10 hours** left. Nothing needs doing until it finishes.

| Phase | State |
|---|---|
| Tasks 1–8, 10 | complete (earlier sessions) |
| Integrity check of all 552 GiB | **PASSED — 54/54 archives, 0 corrupt** |
| Duplicate chunk 049 | **resolved — deleted, 53 zips + 1 MP4 remain** |
| valkey OOM risk | **fixed — ceiling 512Mi → 2Gi** |
| Task 9 (README runbook) | **complete** — 61 → ~765 lines, 4 review rounds |
| Task 11 Pass 1 dry run | **PASSED** — 119,624 media, 74 albums, 0 errors |
| **Task 11 Pass 1 real run** | **RUNNING** — job `immich-go-import-zips` |
| Task 11 Pass 2 (standalone MP4) | **CANCELLED** — unwanted accidental recording, never imported |
| Task 12 (re-enable ML) | not started — 1–3 days of CPU inference |
| Final whole-branch review | **done — "Ready to merge"** |

---

## THE RULE THAT HAS NOT CHANGED

```bash
kubectl --context local-k3s ...
helm   --kube-context local-k3s ...
velero --kubecontext local-k3s ...
```

The shell default is an **unrelated production cluster**. Note the three different flag
spellings — `--context`, `--kube-context`, `--kubecontext`. All three are now enforced by
a canary in `immich/README.md`:

```bash
grep -rnE '(^|[[:space:]`(])(kubectl|helm|velero)[[:space:]]' immich/ \
  | grep -v 'context local-k3s'
```

Expected: **empty**. It scans all of `immich/`, not just the README — a README-only sweep
could not see the unscoped commands that were sitting in `alerts.yml`'s alert
*descriptions*, which are the first thing a paged operator reads.

---

## Watch the import

`--no-ui` writes progress with carriage returns and never terminates the line, so
**`kubectl logs --tail=N` returns stale or partial data.** It twice showed me a frozen
counter that was in fact advancing. Two reliable methods:

```bash
# stdout counter
kubectl --context local-k3s logs -n immich job/immich-go-import-zips > /tmp/imp.log
tr '\r' '\n' < /tmp/imp.log | grep -o 'Assets found: [0-9]*' | tail -1
```

```bash
# immich-go's own structured log, inside the pod — far richer, but ~95 MB and growing,
# so ALWAYS aggregate; never cat or broadly tail it.
P=$(kubectl --context local-k3s get pod -n immich -l job-name=immich-go-import-zips \
      -o jsonpath='{.items[0].metadata.name}')
kubectl --context local-k3s exec -n immich "$P" -- bash -c \
  'L=$(ls /root/.cache/immich-go/*.log)
   grep -oE " (INF|WRN|ERR|FTL) " "$L" | sort | uniq -c
   grep -c "uploaded successfully" "$L"'
```

The authoritative progress measure is the database, and it tracks the log's upload count
almost exactly (2,138 log uploads vs 2,139 rows when last checked):

```bash
kubectl --context local-k3s exec -n immich immich-postgres-0 -- \
  psql -U immich -d immich -tAc 'SELECT count(*) FROM asset;'
```

### Status at handoff (03:04Z, ~32 min in)

- 2,139 assets, 14 albums in the database, climbing
- discovery **complete**: 108,481 images + 11,143 videos = **119,624 media**
- upload rate ~194/min → **ETA roughly 10 hours** from 02:53Z, so about **13:00Z**
- valkey at **9Mi against its 2Gi ceiling** — the raise was ample; 512Mi would likely
  have held too, but with far less margin on an unattended overnight run
- 101 `ERR`, all secondary metadata — see the next section
- zero `FTL`, zero upload failures

---

## !! The one real problem: stack/album/tag 500s !!

**101 errors so far, ~4.7% of uploads. No asset has failed to upload.** Every error is a
*secondary* operation, and they all trace to **one** root cause.

Server-side, in both directions:

```
PostgresError: update or delete on table "asset" violates foreign key constraint
  "stack_primaryAssetId_fkey" on table "stack"
PostgresError: insert or update on table "stack" violates foreign key constraint
  "stack_primaryAssetId_fkey"
[Microservices:{...,"deleteOnDisk":true}] Unable to run job handler (AssetDelete)
```

immich-go creates a stack (grouping Google's `-edited` variant with its original) while
Immich's background `AssetDelete` job removes a duplicate that the stack references as
`primaryAssetId`. The transaction fails and the enclosing request returns 500. The album
and tag failures are **downstream of the same failed transactions**, not separate bugs.

This is an **Immich v3.2.0 server-side race**, not a misconfiguration, and
`--concurrent-tasks=4` makes it more likely.

Breakdown: 83 stack, 12 tag, 5 album.

**Impact, in order of seriousness:**

1. **Albums** — genuine metadata loss. 3 `failed to create album` against 14 created is a
   worryingly high rate. If it holds, a meaningful fraction of the expected 74 albums
   could be missing. **Check the final album count against the dry run's 74.**
2. **Tags** — some Google Photos *people* labels not applied. Recoverable.
3. **Stacks** — `-edited` variants land as separate assets instead of grouped. Cosmetic.

**Do NOT restart the import to fix this.** `--on-errors=continue` is working as intended,
assets are landing correctly, and a restart costs ~35 minutes of re-discovery.

**Likely repair path — verify, do not assume.** immich-go is idempotent: a second run over
the same zips should skip assets already on the server by content hash and re-attempt the
album, tag and stack operations. That is far cheaper than re-importing. **Test it on a
single chunk first** (`/takeout/takeout-20260913T100905Z-1-001.zip`) and confirm it does
not duplicate assets before running it over all 53. Consider `--concurrent-tasks=1` for
the repair pass to avoid re-triggering the race.

---

## !! There is a large post-import pipeline nobody has accounted for !!

Discovered live, and neither the spec nor the plan mentions it. **Do not read "Pass 1
finished" as "the import is done."**

Every uploaded file is currently sitting in **`/data/upload`**, not `/data/library`:

```
/data/upload          11G      <- created 02:53Z, exactly when uploads began
/data/library         2.0K     <- empty
/data/thumbs          2.0K     <- empty
/data/encoded-video   2.0K     <- empty
```

immich-go's `--pause-immich-jobs` (default **true**) has paused these queues, confirmed
via `/api/jobs`:

| Queue | Paused |
|---|---|
| `metadataExtraction` | **yes** |
| `thumbnailGeneration` | **yes** |
| `videoConversion` | **yes** |
| `faceDetection` | **yes** |
| `smartSearch` | **yes** |
| `storageTemplateMigration` | no |

`storageTemplateMigration` is *not* paused, but it cannot do anything yet: the storage
template is `{{y}}/{{y}}-{{MM}}-{{dd}}/{{filename}}`, which needs the capture date, and
`metadataExtraction` is what produces it. So the relocation is blocked behind a paused
queue.

**After Pass 1 completes, expect this sequence — on an SMR drive:**

1. `metadataExtraction` over ~119,624 assets — produces the dates.
2. `storageTemplateMigration` moves **~543 GB** of files from `/data/upload` into
   `/data/library/{{y}}/{{y}}-{{MM}}-{{dd}}/`.
3. `thumbnailGeneration` — hundreds of thousands of small writes, which is SMR's
   worst case and the slowest step.
4. *Then* Task 12's ML (Smart Search, Face Detection), a further 1–3 days.

**Check the queues are actually running once Pass 1 ends.** immich-go pauses them, so if
the Job is killed or dies rather than exiting cleanly, they may **stay paused** and the
library will look permanently stalled for no visible reason:

```bash
K=$(kubectl --context local-k3s get secret immich-api -n immich \
      -o jsonpath='{.data.API_KEY}' | base64 -d)
kubectl --context local-k3s run jobs-probe --rm -i --restart=Never -n immich \
  --image=curlimages/curl:latest --quiet -- \
  curl -s -H "x-api-key: $K" http://immich-server.immich.svc.cluster.local:2283/api/jobs \
  | tr ',' '\n' | grep -iE '"[a-z]+":\{"queueStatus"|isPaused'
```

Un-pause from the admin UI (Administration → Jobs) if anything is still paused.

### Related trap: `df -h /data` massively overstates the library

`df` reported **553G** while the library was actually **11G**. The takeout PVC and the
library PVC are **both** on the same `nfs-bulk` export (`192.168.5.1:/bulk-pool/k3s-bulk`),
so `df` reports *pool* usage — 543G of which is the takeout. An operator reading 553G
would conclude the import had already finished.

The plan's Task 11 Step 8 uses `du -sh /data`, which is correct. Use `du`, not `df`:

```bash
kubectl --context local-k3s exec -n immich deploy/immich-server -- du -sh /data
kubectl --context local-k3s exec -n immich deploy/immich-server -- du -sh /data/*
```

This is the same statfs-versus-PVC confusion recorded as Ruling 12 in the previous
ledger, where the `ImmichLibraryVolumeFilling` alert was deliberately left measuring the
pool. Consistent behaviour, but it surprises people twice.

---

## Resume here

### Step 1 — wait for Pass 1, then check counts against the dry run

```bash
kubectl --context local-k3s get job immich-go-import-zips -n immich
kubectl --context local-k3s exec -n immich immich-postgres-0 -- \
  psql -U immich -d immich -tAc 'SELECT count(*) FROM asset;'
kubectl --context local-k3s exec -n immich immich-postgres-0 -- \
  psql -U immich -d immich -tAc 'SELECT count(*) FROM album;'
```

The dry run is the benchmark, and it is a good one — it enumerated the same 53 chunks:

| Metric | Dry-run figure | Meaning if the real run falls short |
|---|---|---|
| media | **119,624** (108,481 img + 11,143 vid) | assets missing |
| albums | **74** created, 183 updated | album creation races (see above) |
| stacks | 42,846 | `-edited` variants not grouped |
| tagged | 66,614 | people labels missing |

`backoffLimit: 0` and no `activeDeadlineSeconds`, so the Job will not retry or be killed.
If it shows `1/1` it completed; if the pod is `Error`, read the log — do **not** blindly
re-run.

### Step 2 — ~~Pass 2, the standalone MP4~~ **CANCELLED 2026-09-16**

> **DO NOT RUN PASS 2.** The human partner identified
> `PXL_20250517_140916476-036.mp4` as an **accidental recording made while running**, and
> does not want it in the library. Verified it was never imported:
> `SELECT ... FROM asset WHERE "originalFileName" ILIKE 'PXL_20250517_140916476%'`
> returns **0 rows** — Pass 1's `/takeout/*.zip` glob could never match an `.mp4`. For
> contrast, 488 other `PXL_2025*` videos imported fine, so this was specific to that file
> rather than a video-handling problem.
>
> Nothing to delete from Immich. The 19.5 GB file and its orphaned sidecar in chunk 035
> simply go away when the takeout PVC is deleted. Deleting it early is optional and low
> value — `bulk-pool` has ~6.3 TB free.
>
> The original procedure is kept below only because the staged-folder + hardlink technique
> is the correct pattern if a similar oversized standalone file ever appears in a future
> Takeout export.

#### (retained for reference only — not to be run)

Only after Pass 1 completes. `immich/import/immich-go-job.yml` holds both Jobs; apply
**one at a time**.

**The plan's `jobsel` helper does not work on this machine** — it needs `python3-yaml`,
which is not installed (nor is `yq`). Use awk on the document separator instead, which I
verified produces a byte-identical Job:

```bash
cd /home/dev/local_code/kube-cluster
# Pass 2 is the SECOND document
awk '/^---$/ { d++; next } d==2 { print }' immich/import/immich-go-job.yml \
  > /tmp/pass2.yml
diff <(sed -n '95,182p' immich/import/immich-go-job.yml) /tmp/pass2.yml   # must be empty
kubectl --context local-k3s apply --dry-run=server -f /tmp/pass2.yml
```

For a **dry run**, inject `DRY_RUN=1` and rename the Job — the script guards it with
`${DRY_RUN:+--dry-run}`, which is safe under `set -euo pipefail` because the `:+` form is
exempt from `nounset` (verified):

```bash
awk '/^          env:$/ && !d { print; print "            - name: DRY_RUN";
     print "              value: \"1\""; d=1; next } { print }' /tmp/pass2.yml \
  | sed 's/^  name: immich-go-import-standalone$/  name: immich-go-import-standalone-dryrun/' \
  > /tmp/pass2-dry.yml
```

**Expect exactly one asset**, dated **2025-05-17**, with GPS. The dry run already proved
the sidecar pairing works — it read chunk 035's sidecar as
`date=2025-05-17 14:49:44`, which is the correct time. The filename says `14:09:16` and
carries no GPS, so **do not** fall back to `from-folder --date-from-name` unless the dry
run reports zero assets; it would be ~40 minutes wrong and drop location.

Verify afterwards:

```bash
kubectl --context local-k3s exec -n immich immich-postgres-0 -- psql -U immich -d immich -c \
  "SELECT \"originalFileName\", \"fileCreatedAt\" FROM asset
   WHERE \"originalFileName\" LIKE 'PXL_20250517_140916476%';"
```

Then remove the staging tree (the hardlink, not the 19.5 GB original).

### Step 3 — spot-check in the UI

`http://immich.rya-scala.ts.net:2283` (Tailscale required). Confirm capture dates are
**historical, not today** — all-today dates mean sidecars were not read, and the fix is
the invocation, not a re-import on top of bad data. Also check albums exist, GPS appears,
and the 40-minute 4K HEVC video sits on 2025-05-17.

### Step 4 — Task 12, re-enable ML

`immich/values.yml` currently has `clip`, `facialRecognition`, `duplicateDetection` and
`ocr` all `false`. Per the plan, re-enable the first three and **leave `ocr` false**
deliberately — it is a separate CPU-only inference pass over every asset on a GPU-less
cluster, and few people search text in images.

```bash
helm --kube-context local-k3s upgrade immich \
  oci://ghcr.io/immich-app/immich-charts/immich \
  --version 0.13.2 -n immich -f immich/values.yml
kubectl --context local-k3s rollout restart deploy/immich-server -n immich
```

**`helm upgrade` alone will NOT restart the server** — chart 0.13.2 puts no config
checksum on the pod template, so the ConfigMap updates while the process keeps serving
old values. The rollout restart is mandatory, not optional. Confirm via
`/api/server/features` (derived from loaded config, so it reflects the process, not the
file), then queue **Smart Search** and **Face Detection** with "Missing" in the admin UI.

Expect **1–3 days**. The library is a single non-redundant 8 TB Seagate ST8000DM004
(**SMR**); hundreds of thousands of small thumbnail writes are SMR's worst case.

### Step 5 — delete the takeout PVC

Only after Steps 1–4 confirm a good import. `archiveOnDelete: "true"` means the NFS
directory is archived rather than destroyed.

```bash
kubectl --context local-k3s delete job immich-go-import-zips \
  immich-go-import-standalone immich-takeout-verify immich-takeout-dedup \
  -n immich --ignore-not-found
kubectl --context local-k3s delete -f immich/import/takeout-pvc.yml
```

Note the plan's Task 12 Step 7 names a stale job, `immich-go-import`. The real names are
`immich-go-import-zips` and `immich-go-import-standalone`.

---

## What this session changed

| Commit | Change |
|---|---|
| `67f4668` | gitignore `.superpowers/` — the concurrent session's `git add -A` would otherwise sweep the SDD ledger into an unrelated commit |
| `0cee269` | valkey `limits.memory` 512Mi → **2Gi** |
| `a71a91c` … `ff90f1c` | `immich/README.md` 61 → ~765 lines (Task 9), 4 review rounds |
| `23ee445` | final-review fixes: Postgres readiness gate in the restore path, PVC check before the dump-listing Job, `alerts.yml` scoping (applied to the cluster), widened canary |
| `9c365ef` | scoped the last unscoped `kubectl` reference; canary now returns empty |

Cluster changes: valkey ceiling raised (helm rev 5), `alerts.yml` re-applied, duplicate
049 deleted, three transient Jobs run (`immich-takeout-verify`, `immich-takeout-dedup`,
`immich-go-import-zips-dryrun` — the first two Succeeded, the third deleted).

### The valkey deviation, recorded

**2Gi deviates from the approved spec's resource table, which says 512Mi.** The human
partner decided this consciously. Requests stay `100m / 256Mi`, so scheduling and
`Burstable` QoS are unchanged — only the ceiling moved. Reason: valkey runs `maxmemory 0`
with `maxmemory-policy noeviction` and cannot self-evict, and persistence is RDB-only
(`appendonly no`, `save 3600 1 / 300 100 / 60 10000`), so an OOMKill loses up to 60 s of
queue writes and queued BullMQ jobs vanish mid-import.

**The previous handoff's alternative — set `maxmemory` with an eviction policy so it
"degrades instead of dying" — was rejected and should not be revisited.** BullMQ requires
`noeviction`. `allkeys-lru` would silently evict queue entries and corrupt queue state,
which is worse than an OOMKill because an OOMKill is at least visible as a restart. More
memory is the only correct fix. `noeviction` stays.

Live evidence says 9Mi at 2,139 assets, so headroom is generous.

---

## Traps — the earlier list still applies, plus these

Everything in `2026-09-14-immich-import-handoff.md` "Traps" remains true. New this session:

1. **In `--dry-run`, immich-go logs `uploaded successfully`, `metadata updated`,
   `tagged`, `stacked` and `added to album`.** Reading the log, a dry run is
   indistinguishable from a real one. I had to establish it three ways: `--dry-run`
   present in `/proc/1/cmdline`, `printenv DRY_RUN` = `1`, and — decisively —
   `SELECT count(*) FROM asset` still `0`. **Check the database, not the log wording.**
2. **`kubectl logs --tail=N` lies on this Job** (carriage-return progress line, never
   newline-terminated). See "Watch the import".
3. **The plan's `jobsel` helper needs `python3-yaml`, which is not installed.** Use the
   awk form above and `diff` the result against the committed file.
4. **`kubectl wait --for=condition=complete` blocks for the full timeout on a failed Job**
   (`backoffLimit: 0` means a failed Job never gets that condition), and `--for` is
   single-valued in the v1.29 client so you cannot wait on complete and failed together.
   The README uses `logs -f` then a short classifying `wait` instead.
5. **`--pod-running-timeout` does not wait for `Running`.** kubectl's `GetFirstPod`
   returns the first listed pod immediately whatever its phase and never consults the
   timeout; only with zero pods does it watch, and then only for the object to *appear*.
   So `logs -f` can fail with `is waiting to start: ContainerCreating` and the following
   classifier can print a **false** FAILED. The README documents the tell (`failed=`
   empty means still running, `failed=1` is genuine).
6. **The `.superpowers/` ledger is git-ignored scratch.** `git clean -fdx` destroys it.

---

## Open items for the human

- **`velero/values.yml` has never been applied.** Commit `460ab05` converted it to an
  `excludedNamespaces` deny-list, but the live `velero-homelab-daily` Schedule still
  carries the old `includedNamespaces` allow-list
  (`[home-assistant, dev-box, openclaw, openclaw-dottie, openclaw-stonk, immich]`).
  `immich` is covered either way, so the import is safe — but applying that file would
  silently add ~20 namespaces to a nightly backup capped at ~2 MB/s. It also means
  `immich/README.md`'s step-8 check reads `includedNamespaces` and would return empty
  once the file is applied. Not introduced by the Immich work; left alone because you
  said you would handle Velero.
- **`dev-box` Velero exclusion still missing** (pre-existing, untouched as instructed).
- **The DB restore procedure is written but UNREHEARSED.** `immich/README.md` now has real
  runnable steps including a Postgres readiness gate, and says plainly that it has never
  been tested. Rehearse it once against a scratch database. It is the *only* database
  recovery path — Velero deliberately excludes `immich-postgres-data` because a kopia copy
  of a running data directory is not crash-consistent.
- **Cloud backup of the library** — still nothing. Google Photos is the backup by design.
  If photos are ever deleted there, the only copy is on one non-redundant SMR drive.
- **k3s 1.29 is EOL.** Separate plan at
  `docs/superpowers/plans/2026-09-13-k3s-cluster-upgrade-phases-0-4.md`. Immich was built
  to survive it unchanged (plain Postgres StatefulSet, not CloudNativePG).

---

## Suggested prompt for the next agent

> Resume the Immich import on the `local-k3s` cluster. Read, in this order:
> `docs/superpowers/handoffs/2026-09-15-immich-import-handoff.md`,
> `docs/superpowers/specs/2026-09-12-immich-homelab-design.md`, and
> `docs/superpowers/plans/2026-09-12-immich-homelab.md`. Also read
> `.superpowers/sdd/progress.md` if it still exists — it is the git-ignored decision log
> for the previous session and contains rulings 18–20.
>
> Pass 1 of the import was launched at 02:53Z on 2026-09-15 as job
> `immich-go-import-zips` and should now be finished. Start by checking whether it
> completed and comparing the final asset and album counts against the dry-run benchmark
> of 119,624 media and 74 albums, which is in the handoff. Then continue from "Resume
> here" Step 2 (Pass 2, the standalone MP4).
>
> Pay particular attention to the album count: Pass 1 was throwing `failed to create
> album` 500s caused by an Immich v3.2.0 foreign-key race on `stack_primaryAssetId_fkey`,
> and albums are the one affected category that is real metadata loss rather than cosmetic.
> The handoff describes a candidate repair — a second idempotent immich-go pass — which
> must be tested on a single chunk before being run over all 53.
>
> Every kubectl/helm/velero command must be scoped to `local-k3s` (note the three
> different flag spellings). Another agent session may be committing to this repo, so
> stage files by explicit path and never `git add -A`.
