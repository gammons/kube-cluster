# SDD ledger — plan: docs/superpowers/plans/2026-09-12-immich-homelab.md

Spec: docs/superpowers/specs/2026-09-12-immich-homelab-design.md (read, reachable)
Branch: master (human partner gave explicit consent to implement on master; repo convention is direct-to-master, no merge commits in history)
Scope this session: Tasks 1-7 only. Human partner elected to stop before Task 8 (Velero), which modifies shared infrastructure protecting 5 other namespaces.
Cluster: local-k3s. ALL kubectl/helm commands must pass --context local-k3s.

NOTE: This plan mutates a live cluster (111 pods, 26 namespaces). Git isolation
does not roll back Helm releases, PVCs, or NFS directories. Treat cluster
changes as the real side effects.

## Pre-flight conflict scan

### Cross-task interface pairs (shared file or interface)

| Pair | Produces -> Consumes | Finding |
|---|---|---|
| T1 -> T3 | PVC immich-postgres-data -> mounted by postgres | OK. Pending until mounted is correct for local-path WaitForFirstConsumer |
| T1 -> T4 | PVCs immich-library / ml-cache / valkey-data -> existingClaim | OK. Verified by render: volume names are `data` (server), `cache` (ML), `data` (valkey) |
| T1 -> T6 | PVC immich-pgdump -> mounted at /dumps | OK |
| T2 -> T3 | DB_STORAGE_TYPE -> `__DB_STORAGE_TYPE__` substitution | OK. Substitution stated explicitly in T3 Step 3 |
| T3 -> T4 | Secret immich-postgres keys + svc FQDN:5432 -> env refs | OK. Verified by render: no duplicate env vars, overrides replace chart defaults |
| T3 -> T6 | Secret keys POSTGRES_USER/PASSWORD/DB -> secretKeyRef | OK. Key names identical in both tasks |
| T3 -> T8 | Pod annotation `data` -> velero exclusion | OK (T8 out of scope this session) |
| T4 -> T5 | tailscale.com/expose + hostname on Service -> operator | OK. Verified annotations render onto immich-server Service |
| T4 -> T7 | metrics.enabled -> ServiceMonitor -> alert `job=~".*server.*"` | OK. ServiceMonitor named immich-server; job label matches regex |
| T4 -> T8 | Pod annotations data/cache -> velero exclusion | OK. Verified `controllers.main.pod.annotations` renders correctly |
| T4 -> T12 | values.yml created -> values.yml modified | OK. Same file, sequential, no conflict |
| T5 -> T11 | Secret immich-api -> API_KEY env | OK (out of scope this session) |
| T6 -> T7 | CronJob name immich-pgdump -> alert selector cronjob="immich-pgdump" | OK. Names match |
| T10 -> T11 | PVC immich-takeout -> mounted read-only | OK (out of scope this session) |

### Per-task self-consistency

| Task | Finding |
|---|---|
| T1 | OK. Creates 3 files, 5 PVCs; verification expects exactly the 2 documented Pending states |
| T2 | OK. Investigation only, produces one value consumed by T3 |
| T3 | DEFECT FOUND AND FIXED — see Ruling 1 |
| T4 | OK. All values keys verified against rendered chart 0.13.2 |
| T5 | OK. Verification-only task; depends on tailnet-side state outside cluster, correctly separated from T4 |
| T6 | OK. Dump size guard present; retention find matches the write path |
| T7 | OK. All 4 metrics confirmed present in live Prometheus |
| T8 | OK but OUT OF SCOPE this session |
| T9 | OK. Documentation, consumes T1-T8 |
| T10-T12 | OK but OUT OF SCOPE — gated on Google Takeout export which does not yet exist |

### Rulings from the scan

Ruling 1: Removed the `PGDATA=/var/lib/postgresql/data/pgdata` override from Task 3.
  Why: image config shows PGDATA is baked in, entrypoint is custom
  (immich-docker-entrypoint.sh) and the vchord preload lives in a custom
  /etc/postgresql/postgresql.conf. The subdir trick exists to dodge lost+found on
  formatted block devices; local-path is a bind-mounted directory with no
  lost+found. Override was unnecessary and risked fighting the custom entrypoint.
  Cost if wrong: if initdb refuses the volume root, Task 3 fails fast at pod
  start and the override is trivially re-added. Low cost, caught in seconds.

Ruling 2: Keep `fsGroup: 999` in Task 3 despite it being unnecessary.
  Why: image has no USER directive, so it starts as root and drops privileges
  itself; fsGroup is inert here. Harmless, and protects if the base image ever
  adds a USER directive.
  Cost if wrong: none identified.

Ruling 3: Accept that `controllers.main` env (DB_HOSTNAME, DB_PORT,
  DB_DATABASE_NAME) propagates to the machine-learning pod, which does not use
  those variables.
  Why: that is how the bjw-s common chart shares top-level controller config;
  splitting them per-component adds values complexity for no behavioural gain.
  Unused env vars are inert.
  Cost if wrong: none identified. Cosmetic only.

Ruling 4: Accept that the T7 alert `up{namespace="immich", job=~".*server.*"} == 0`
  matches two series (metrics-api on 8081, metrics-ms on 8082).
  Why: both endpoints should be up; alerting if either is down is desirable, not
  a false positive.
  Cost if wrong: a noisier alert than intended. Adjust the regex if it proves chatty.

### Verified externally before execution (not recalled)

- Chart 0.13.2 renders clean; all values keys take effect as written
- ghcr.io/immich-app/postgres:17-vectorchord1.1.1 exists; User=None, PG_MAJOR=17
- Chart auto-sets IMMICH_TELEMETRY_INCLUDE=all when metrics.enabled -> no false ImmichServerDown
- All 4 alert metrics present in live Prometheus (kube-state-metrics deployed)
- immich-go has NO container image; Task 11 downloads checksum-verified tarball

## Progress

(no tasks complete yet)

Task 1: complete (working tree, uncommitted by plan design; review clean — spec PASS, quality APPROVED)
Task 1: minor (deferred): M1 review package showed `git add -N` staging artifact; contradicts report. Controller-side hygiene.
Task 1: minor (deferred): M2 review package bundled unrelated plan-doc commit 693917d and cited stale HEAD 6d36f13.
Task 1: minor (CARRY FORWARD TO TASK 9 README): M3 `local-path` has allowVolumeExpansion:false. immich-postgres-data (50Gi) and immich-valkey-data (1Gi) can NEVER be resized in place. Growing the DB volume later = delete/recreate PVC + restore from immich-pgdump. Real operational constraint, must be documented in the README.
Task 1: minor (deferred): M4 report wording says ".gitignore staged" when file was untracked. Cosmetic.

Ruling 5: Fix review-package generation for Tasks 2+. Scope the package to the
  task's own deliverables, refresh the HEAD reference at generation time, and do
  not leave `git add -N` artifacts visible in the captured status.
  Why: M1/M2 were my own process defects, not implementer defects; they cost the
  reviewer time and invited a false scope-creep finding.
  Cost if wrong: none — strictly an improvement to review signal.

Ruling 6: Batch Task 2 (investigation: determine disk type) with Task 3
  (Postgres StatefulSet) into a single dispatch.
  Why: Task 2 has no file deliverable and no diff to review — its entire output
  is one value (`SSD` or `HDD`) consumed by Task 3's `__DB_STORAGE_TYPE__`
  placeholder. A separate implementer+reviewer pair for reading a rotational flag
  is a full review seat for a two-command investigation. The skill directs
  batching small same-shape work and reserving per-task dispatch for work needing
  its own judgment or review surface.
  Cost if wrong: the disk-type determination gets less independent scrutiny. Low —
  Task 3's reviewer sees the value, the reasoning, and the resulting manifest
  together, which is arguably better context than reviewing it in isolation.

Task 2+3: fix round 1/5 (1 addressed, 0 open — Important#1 README disk-type justification; commits 6296be3..5d8c9ed)
Task 2+3: complete (commits 693917d..5d8c9ed, review clean — spec PASS, quality APPROVED, fix loop closed round 1)
  Deployed: immich-postgres-0 Running on k3s-node-3, 0 restarts, PVC Bound, vchord 1.1.1, shared_preload_libraries=vchord.so
  DB_STORAGE_TYPE=SSD (justified by node_exporter counters on allocated I/O, independently re-derived twice)

Task 2+3: minor (CARRY FORWARD TO TASK 9): README.md now holds the disk-type rationale + secret-retrieval command.
  Task 9 MUST EXTEND this file, never overwrite it. Also fold in the local-path allowVolumeExpansion:false constraint (Task 1 M3).
Task 2+3: minor (deferred): max_wal_size wrongly listed in README's "DB_STORAGE_TYPE only sets" list. Image templates differ by exactly 2 lines (effective_io_concurrency, random_page_cost); max_wal_size=5GB identical in both. Error overstates blast radius, so robustness argument holds a fortiori. Fix on next touch.
Task 2+3: minor (deferred): README Prometheus query returns 8 series, not 3 — lacks job="node-exporter" selector, so it also returns OVH cluster + Proxmox host (15ms, reads like a contradiction). Add the selector.
Task 2+3: minor (deferred): README hardcodes NodePort 192.168.10.4:30090, which will rot.
Task 2+3: minor (deferred): one stray node-debugger pod survived cleanup; report overstated "zero left behind". ~32 pre-existing stale debugger pods in default ns, unrelated.
Task 2+3: minor (deferred): task-2-brief command defects — `sda` not derivable from df on LVM root; /host/dev/sda needs --profile=sysadmin; busybox date lacks %N; cleanup selector `-l app=node-debugger` matches nothing (real label app.kubernetes.io/managed-by=kubectl-debug).

FINDING — MATERIAL, SURFACE TO HUMAN PARTNER:
  The re-reviewer discovered the Proxmox host IS scraped by this Prometheus as job="proxmox-host" (192.168.5.1:9100).
  Physical layer is therefore knowable, and it changes what we know about the LIBRARY disk:
    - VM zvols live on main-pool = NVMe (Crucial T500), confirming DB_STORAGE_TYPE=SSD from the physical layer.
    - bulk-pool (the 8TB disk holding the ENTIRE Immich photo library) is /dev/sda = Seagate ST8000DM004.
      That model is SMR (shingled magnetic recording). Measured avg read wait 15.1ms — textbook spindle.
  Implication for Tasks 10-12: SMR drives degrade badly under sustained random writes. Photo originals are
  large sequential writes (tolerable), but thumbnail/preview generation for ~200k assets means 400k+ small
  file writes, which is the SMR worst case. Import and ML thumbnail passes may be much slower than estimated.
  Not blocking Tasks 4-7. Must inform import expectations.

Task 4: fix round 1/5 (2 addressed, 0 open — I1 DB creds fanned out to valkey+ML; spec-gap valkey resources; commits 295e08f..3013e20)
Task 4: complete (commits 5d8c9ed..3013e20, review clean — spec PASS, quality APPROVED, fix loop closed round 1)
  Deployed: immich-server + machine-learning + valkey Running on k3s-node-1, none on node-2. helm rev 2, chart 0.13.2, app v3.2.0.
  DB migrations ran (71 tables), API /api/server/ping = 200, valkey QoS now Burstable.

Ruling 7: Dismissed reviewer finding I2 (valkey `data` volume has no Velero exclusion annotation).
  Why: not a gap. The spec explicitly INCLUDES immich-valkey-data in Velero's backup set
  ("What Velero does capture: the namespace's Kubernetes objects, immich-pgdump, and
  immich-valkey-data"). It is 1Gi and holds the job queue. Excluding it would contradict the spec.
  Cost if wrong: a 1Gi volume is backed up that need not be. Negligible.

Ruling 8: Escalated reviewer's M3 (valkey no resources) from Minor to a spec gap and fixed it.
  Why: reviewer graded against the Task 4 brief, which omitted valkey resources. But the design
  spec's resource table specifies valkey at 100m/256Mi -> 1CPU/512Mi. The spec is the binding
  authority, so this was a spec compliance gap, not a nice-to-have. BestEffort QoS on the pod
  holding the job queue makes it first to be OOM-killed under node pressure.
  Cost if wrong: none — brings deployment into line with the approved spec.

Task 4: minor (deferred): report said node-1 carries 3Gi requests; actual is 4Gi. Conclusion unaffected.
Task 4: minor (deferred): `helm template` run without --kube-context. Client-side only, no cluster contact. Harmless.
Task 4: minor (deferred): valkey has no readinessProbe, only exec livenessProbe. Service routes to it before RDB load completes. Pre-existing chart behaviour.
  WITHDRAWN 2026-09-12 (final fix wave): factually wrong. Live `deploy/immich-valkey` has all three probes — startupProbe (failureThreshold 30), readinessProbe (initialDelaySeconds 5) and livenessProbe (initialDelaySeconds 30), each running `sh -c "valkey-cli ping | grep PONG"`. No action needed; do not re-investigate.
Task 4: PLAN DEFECT (fix in downstream briefs): plan expects tables `assets`/`users`/`albums`. Immich v3.2.0 renamed these to singular `asset`/`user`/`album` via the StandardizeNames migration. Verified against live DB and kysely_migrations.

FINDING — FORWARD RISK FOR IMPORT PHASE (Tasks 10-12):
  valkey now has a HARD 512Mi limit with NO maxmemory / maxmemory-policy configured, so it will
  not self-evict — it will be OOMKilled instead. During the bulk import, BullMQ queue depth is the
  variable that decides whether 512Mi holds. If valkey OOMKills mid-import, THIS is the cause.
  512Mi is what the approved spec specifies, so not a defect — but the import task should watch it
  and consider raising the limit or setting a maxmemory policy before ingesting ~200k assets.

Task 5: complete-partial (steps 1-3 verified by controller; steps 4-6 require a browser, handed to human partner)
  VERIFIED: ts-immich-server-qfl94-0 Running in tailscale ns; device `immich` = 100.117.220.23 (tagged-devices)
  VERIFIED: http://immich.rya-scala.ts.net:2283/api/server/ping = 200 {"res":"pong"}; version 3.2.0
  OUTSTANDING (human): Step 4 create admin account via browser; Step 5 generate API key + create secret immich-api; Step 6 verify key
  /api/server/config reports "isInitialized": false -> admin account genuinely not yet created.
  NOT BLOCKING Tasks 6-7. The immich-api secret is consumed only by Task 11 (import), which is out of scope this session.

Ruling 9: Do not attempt to script admin-account creation via the API to avoid the browser step.
  Why: /api/auth/admin-sign-up returns 404 on v3.2.0 and the onboarding flow is UI-driven. Forcing it
  would mean guessing at an undocumented endpoint and could leave a half-initialised instance.
  Cost if wrong: none — the human does a 60-second signup. Task 11 is out of scope regardless.

Ruling 10: Batch Task 6 (pg_dump CronJob) with Task 7 (PrometheusRule alerts) in one dispatch.
  Why: same shape (add one manifest, apply, verify), and Task 7's ImmichPgDumpFailed alert depends on
  Task 6 having produced a successful CronJob run. Batching makes the dependency natural rather than
  forcing an artificial handoff, and gives the reviewer both halves in one context.
  Cost if wrong: slightly larger review surface. Low — both are small single-file manifests.

Task 6+7: fix round 1/5 (1 addressed, 0 open — absent() blind spot in ImmichPgDumpFailed; commits 0415e16..a632e4e)
Task 6+7: complete (commits 3013e20..a632e4e, review clean — spec PASS, quality APPROVED)
  Deployed: CronJob immich-pgdump (0 4 * * *, Forbid); verified dump 18,344,596 B gz -> 52,247,405 B,
    238,430 lines, 71 CREATE TABLE = 71 COPY = 71 live tables. Complete, not truncated.
  Deployed: PrometheusRule immich-alerts in monitoring, 5 rules, all inactive health=ok, 0 eval errors.

PLAN DEFECT FOUND AND FIXED BY IMPLEMENTER: brief's CronJob used `/bin/sh` with `set -euo pipefail`,
  but the image's /bin/sh is dash. `set` is a special builtin, so the failure ABORTED the script on
  line 1 — the job was structurally incapable of ever producing a backup (BackoffLimitExceeded in 13s).
  Fixed to /bin/bash (5.2.15, present in image). pipefail retained deliberately: reviewer verified the
  counterfactual `false | gzip -c > f` exits 0 and writes a valid 20-byte gzip, so without pipefail a
  totally failed pg_dump reports success.

Ruling 11: DEFER reviewer finding I-1 (ImmichServerDown and ImmichPostgresDown share the same
  absent-series blind spot we just fixed in ImmichPgDumpFailed) to a follow-up task. Not fixing now.
  Why: the reviewer explicitly judged it non-blocking and out of the brief's scope. More importantly
  the two cases are not equivalent in severity. A silently-missing pg_dump is uniquely dangerous
  because it is the ONLY recovery path for the database (Velero deliberately excludes the Postgres
  data dir) AND its failure is invisible — nobody notices a backup that did not happen. A deleted
  immich-server Deployment has obvious independent detection: Immich is simply down and the user sees
  it. Fixing the backup case and deferring the liveness case is a severity-ordered choice, not a
  half-finished job.
  Cost if wrong: if someone deletes the Deployment or StatefulSet, that specific alert stays silent.
  Mitigated by the failure being user-visible. Recorded here so the follow-up is not lost.

Ruling 12: ACCEPT reviewer finding I-2 (ImmichLibraryVolumeFilling measures the ~7.14TiB nfs-bulk
  pool via statfs, not the 2Ti PVC request) with no change.
  Why: the pool IS the real constraint. nfs-subdir-external-provisioner enforces no quota, so the 2Ti
  PVC figure is pure bookkeeping and alerting on it would be alerting on a number that means nothing.
  The alert annotation already states "bulk pool ... single 8TB disk", which matches the actual
  semantics. Effective trigger point is ~6.07 TiB of pool fill.
  Cost if wrong: the library could consume far more than its nominal 2Ti request without alerting.
  That is intended — the physical disk is the limit that matters.

Ruling 13: Amended the plan's Task 10 rclone Job from `set -euo pipefail` to `set -eu` (commit below).
  Why: reviewer M-6 correctly flagged that rclone/rclone is Alpine-based, so /bin/sh is busybox ash.
  Busybox may support pipefail since 1.31 but it is untested here, and the script contains NO PIPES,
  so pipefail is unnecessary. Added an inline comment citing the Task 6 failure so nobody re-adds it.
  Cost if wrong: none — strictly removes an untested dependency from a script that does not need it.

Task 6+7: minor (deferred): M-1 report PrometheusRule counts (33/34) wrong; actual 35 total, 34 non-immich.
Task 6+7: minor (deferred): M-2 report overstated repo severity convention as unanimous; velero grades staleness critical, mysql grades it warning. Grading choice still defensible.
Task 6+7: minor (deferred): M-3 `-mtime +14` deletes at >=15 days, so effective retention is 15 days not 14. Over-satisfies requirement.
Task 6+7: minor (deferred): M-4 `find -delete` runs under `set -e` after the dump is written; a transient NFS error there fails the job and the retry writes a second dump. Cosmetic on a 20Gi PVC.
Task 6+7: minor (deferred): M-5 ImmichPgDumpMissing will fire ~6h after any fresh deploy until the first 04:00 run. Documented in the annotation, consistent with velero precedent.
Task 6+7: UNTESTED: the 04:00 CronJob controller path has never run (lastScheduleTime unset). Only the manual `create job --from=cronjob` path has executed. First real test is 04:00.

FINAL WHOLE-BRANCH REVIEW: APPROVED FOR MERGE. Zero Critical. Fix wave applied (ee0e41a, a5aaba2),
scoped re-review APPROVED, all 5 findings addressed, no new breakage.

Ruling 14: Disable Immich's built-in nightly DB backup (backup.database.enabled: false).
  Why: default-on, ran at 02:00 writing to /data/backups on the immich-library PVC — the volume the
  spec declares un-backed-up and which is Velero-excluded. It created a SECOND, unmonitored DB
  recovery path that would look like safety to a future reader, and put write load on the SMR disk.
  The immich-pgdump CronJob is the single documented, alerted path. Verified nothing was lost:
  /data/backups held only a 13-byte .immich marker, disabled before the first 02:00 run ever fired.
  Cost if wrong: we rely solely on the pg_dump CronJob. That is intentional and it IS alerted
  (ImmichPgDumpFailed + ImmichPgDumpMissing), which the built-in backup was not.

Ruling 15: Fix OCR/duplicateDetection/SMR defaults NOW rather than deferring to Task 10.
  Why: these were live and doing harm — OCR ran CPU-only inference on a GPU-less cluster, and
  integrityChecks.checksumFiles ran a full-library checksum nightly at 03:00 over NFS against an SMR
  drive, capped at 1h so it would never finish and would grind every night forever.
  Cost if wrong: none identified. All are reversible values edits.

Ruling 16: Adjudicated residual #1 (plan/repo values.yml drift) by annotating the plan snippet as
  non-authoritative rather than dispatching a second fix wave.
  Why: the skill permits no second fix wave, but leaving it silent means a future rebuild from the
  plan yields a materially worse deployment (built-in backup on, nightly checksum on, thumbnail
  concurrency 3, valkey BestEffort). A controller-authored doc annotation is the proportionate close.
  Also recorded the helm-upgrade-does-not-restart trap there.
  Cost if wrong: the plan is slightly redundant with the repo. Trivial.

Ruling 17: Do NOT delete this workspace, contrary to the skill's finish step.
  Why: that step assumes plan completion. Only 7 of 12 tasks ran. This ledger carries forward items
  Tasks 8-12 depend on: the Task 9 README must-fix list, the valkey OOM forward risk, the SMR
  implications for import, and the Task 8-before-Task-10 ordering constraint. Deleting it discards
  exactly the context the remaining work needs.
  Cost if wrong: a gitignored directory persists. Zero.

MUST-DO BEFORE CLOSING (from final review triage):
  - Verify tomorrow: `kubectl --context local-k3s get cronjob immich-pgdump -n immich` -> LAST SCHEDULE
    must be populated. The 04:00 controller path has NEVER fired; only the manual --from=cronjob path
    has run. ImmichPgDumpMissing CANNOT catch a broken schedule (lastSuccessfulTime is already set).
  - Task 8 MUST be done BEFORE Task 10, or not until after Task 12. Never interleaved: the takeout
    PVC's exclusion annotation lives on a Job pod template, so if that Job exists while immich is
    already in the Velero schedule, a nightly backup may start ingesting 1.5TB at ~2MB/s.
  - Until Task 8 lands, immich-pgdump has NO offsite copy. It sits on the same NAS as everything else.

=== 2026-09-13 MORNING SESSION ===

RESOLVED — the one open verification item from last night:
  04:00 CronJob controller path FIRED SUCCESSFULLY.
  lastScheduleTime=2026-09-13T04:00:00Z, lastSuccessfulTime=2026-09-13T04:00:10Z, duration 10s.
  Log: "dumping to /dumps/immich-20260913-040005.sql.gz / dump size: 18344623 bytes / done"
  Two dumps now retained (manual 01:01 + scheduled 04:00). Zero immich alerts firing.
  The schedule is proven end-to-end. This closes the last untested path in Tasks 1-7.

STATE: admin account created (isInitialized: true, 1 user). 0 assets, 0 albums. Library 1.0M used of 7.2T.
STILL OPEN: no `immich-api` secret -> Task 5 Step 5 (API key) not yet done. Gates Task 11 only.

Observation (minor, no action): the pgdump job pod ran on k3s-node-2, the node excluded for Immich
  workloads. The CronJob has no nodeAffinity. Harmless — 10s job, 100m/256Mi requests — but noting
  that the node-2 exclusion is not applied uniformly across immich workloads.

Task 8: complete (commit 5938f5e, immich added to velero-homelab-daily). Other 5 namespaces intact.
  BUT the task revealed a DESIGN FLAW of mine — see below.

CRITICAL DESIGN FLAW FOUND (mine, in the approved spec):
  `immich-pgdump` will NEVER be captured by Velero.
  Velero's fs-backup (kopia) can only read a PVC that is mounted by a RUNNING pod. immich-pgdump is
  mounted ONLY by the CronJob pod, which runs at 04:00, finishes in ~10s, and is phase=Succeeded long
  before the 06:00 backup. Velero logs: "Skip pod volume dumps ... phase=Succeeded: pod is not running".
  Confirmed live: only 1 PodVolumeBackup for immich (immich-valkey-data, 3928 B).
  CONSEQUENCE: because immich-postgres-data is also (correctly) excluded, a Velero restore of the
  immich namespace produces an EMPTY DATABASE while the backup reports "Completed". That is precisely
  the false-confidence failure the spec set out to avoid, and I designed it in.
  The spec text "What Velero does capture: ... immich-pgdump" is factually wrong and must be corrected.

PRE-EXISTING CLUSTER PROBLEM (NOT ours, predates this work, affects the human partner's other backups):
  dev-box-0 has NO `backup.velero.io/backup-volumes-excludes` annotation at all, despite
  velero/README.md:71 documenting that it excludes docker, pmbot-raw-ro, pmbot-curated-ro.
  Result: every nightly backup uploads docker (13.1 GB) + pmbot-curated-ro (60.3 GB) + home (48.3 GB)
  and then FAILS on pmbot-raw-ro -> velero-homelab-daily is PartiallyFailed (errors: 1) every night.
  The README explicitly says pmbot is "300GB+ ... intentionally not backed up".
  So ~120 GB/night is being uploaded that was meant to be excluded, and the nightly backup has been
  silently PartiallyFailed. Worth the human partner's attention independently of Immich.

TASK 10 REDESIGN REQUIRED (my planning defect):
  Plan assumed Takeout delivered via "Add to Drive" + an rclone Job. The human partner's export was
  delivered as DOWNLOAD LINKS, which are browser-authenticated and expire (~7 days, limited retries).
  The rclone-from-Drive design does not apply. Human partner runs Arch Linux and can mount the NAS.

DESIGN FLAW FIXED (commit 55f0dc9): immich-pgdump now mounted read-only at /dumps in immich-server.
  Human partner confirmed intent: DB backup wanted (~$0.003/mo); only the LIBRARY is cost-prohibitive.
  Values key (from vendored bjw-s common 5.1.0 schema, not guessed):
    server.persistence.immich-pgdump -> globalMounts [{path: /dumps, readOnly: true}]
  PROOF: PodVolumeBackup immich-pgdump on immich-server = 36,689,219 B, exactly the sum of the two
  dumps (18344596 + 18344623). No PVB for library `data`, none for immich-postgres-0.
  Read-only proven 3 ways (touch fails, rm fails, /proc/mounts shows ro).
  The spec's claim "Velero captures immich-pgdump" is now TRUE. No spec correction needed.

Human partner decision: LEAVE dev-box velero annotation alone. They will handle it. Do not touch.

OPEN (deferred, from fix report): a DB restore runbook exists only as prose in the plan. Velero restores
  the .sql.gz to a PVC but nothing loads it into Postgres. Should be written AND rehearsed once.
  Belongs in Task 9 README.
OPEN (pre-existing): pgdump CronJob pod schedules onto k3s-node-2 — the one immich workload the
  nodeAffinity exclusion does not cover. Harmless (10s job) but inconsistent.

Task 10 REDESIGNED + applied (content in commit 489a287 — see collision note below).
  PVC immich-takeout Bound, 1500Gi, RWX, nfs-bulk.
  Browser download target: /mnt/takeout/immich-immich-takeout-pvc-e995c66a-e1ed-444f-a66b-51227a1db4c9
  Unprivileged write PROVEN as uid 1000 (no sudo): file created uid=1000 gid=1000 mode=644, then removed.
  rclone/Drive purged from the plan except deliberate "do not use" callouts + harmless .gitignore entry.
  Velero exclusion `backup-volumes-excludes: takeout` now stated in 4 places across Tasks 10 and 11.

!! COMMIT COLLISION — ANOTHER AGENT SESSION IS ACTIVE IN THIS REPO !!
  Commits at 06:42:15/22/30 (`add proxmox host zfs monitoring`, `track kube-prometheus-stack config...`,
  `add proxmox bulk storage and k3s upgrade design docs`) are NOT from this session.
  That session's `git add -A` swept our immich/import/takeout-pvc.yml and plan rewrite into its
  unrelated ZFS-monitoring commit 489a287. Content is correct and on master; history is muddled.
  This WILL keep happening while two sessions share one working tree. Surface to human partner.

RELEVANT INTEL from the other session's commit message:
  "main-pool ran degraded on a single disk for weeks without alerting."
  main-pool backs the `nfs` storage class = where immich-pgdump lives. Our DB dumps were sitting on a
  degraded pool. Checked now: zpool_health main-pool=0 and bulk-pool=0 (both healthy), dumps intact
  (2 files, 18344596 + 18344623 bytes). Now monitored by that session's new textfile collector.

/mnt/takeout fstab analysis: NOT in fstab. Assessed the "reboot mid-download" risk as SELF-MITIGATING:
  when unmounted, /mnt/takeout is an empty root-owned dir (755), so the PVC subdirectory path does not
  exist and an unprivileged browser write FAILS LOUDLY rather than silently landing on the laptop's
  root disk. Laptop root has 1.8T free so disk-full was never the real risk; wrong-destination was, and
  the path simply vanishes. Offer fstab for convenience, not safety.

=== TAKEOUT DOWNLOAD FINDINGS (2026-09-14) ===
538 GiB downloaded, 51 complete + 4 in flight. bulk-pool 8% used, 6.7T free.

FINDING 1 — "missing chunk 036" is NOT missing. RESOLVED, no data loss.
  Slot 036 is `PXL_20250517_140916476-036.mp4`, 19.5 GB (18.17 GiB).
  Verified by magic bytes: genuine "ISO Media, MP4 Base Media v1", NOT a misnamed zip.
  ffprobe reads it cleanly: duration 2426.66s (40.4 min), 3840x2160, HEVC, moov atom intact.
  It exceeds the 10 GB zip split size, so Google Takeout served it standalone.
  Accounting: 49 zips (001-035, 037-050) + 1 standalone mp4 (036) = slots 001-050 complete.

  !! CONSEQUENCE FOR TASK 11 !!
  `immich-go upload from-google-photos /takeout/*.zip` will NOT import this file:
    (a) the *.zip glob does not match it, and
    (b) it has no adjacent .json sidecar, so capture date / album / GPS would be lost.
  Needs a SEPARATE import pass. Its filename encodes the timestamp (PXL_20250517_140916476
  -> 2025-05-17 14:09:16.476), which immich-go can parse from the filename.
  TODO before import: check whether a matching .json sidecar for this video exists inside one
  of the zips; if so, extract it and place it alongside the mp4 so metadata is preserved.

FINDING 2 — duplicate download.
  `takeout-20260913T100905Z-1-049 (1).zip` AND `takeout-20260913T100905Z-1-049.zip` both present,
  both 9.99 GiB. Chrome's "(1)" suffix = the file was downloaded twice.
  immich-go dedupes by content hash so it is not a correctness risk, but it wastes ~10 GB and an
  extra archive's worth of import time. Verify both pass the integrity check, then delete one.
  Do NOT delete before the integrity check - if they differ, one of them is the corrupt copy.

API KEY DONE (2026-09-14): secret immich-api created, key len 42.
  VERIFIED it actually authenticates: GET /api/users/me -> grant@grant.dev, isAdmin, storageLabel=admin.
  This closes Task 5 Step 5/6. Last non-download blocker for Task 11 is gone.
  Minor: the admin account still reports shouldChangePassword: true.

=== DOWNLOADS COMPLETE (2026-09-14) ===
552.4 GiB. 54 zip files on disk = 53 unique chunks (001-054, 036 absent) + 1 duplicate (049 (1)).
Plus PXL_20250517_140916476-036.mp4 (18.17 GiB). Zero .crdownload remaining. No gaps.

CONTROLLER ERROR (mine) — silently-failing verification command:
  I supplied `unzip -l "$z" 2>/dev/null | grep ...` to search the zips for the video's sidecar.
  `unzip` is NOT installed on this laptop, so every invocation failed and 2>/dev/null swallowed the
  error. It reported ZERO hits across all 54 archives — a confident, wrong answer.
  Subagent caught it and redid the scan with Python zipfile.
  LESSON: never route stderr to /dev/null in a verification command. A verification that cannot
  distinguish "no matches" from "tool missing" is not a verification.

SIDECAR FOUND (and it matters):
  Takeout/Google Photos/Photos from 2025/PXL_20250517_140916476.mp4.supplemental-metadata.json
  inside takeout-...-035.zip (sole hit across all 54 zips).
  sidecar photoTakenTime = 14:49:44 UTC, WITH GPS.
  filename-derived time  = 14:09:16, NO GPS.
  So --date-from-name would have been ~40 min wrong and dropped location. Pass 2 hardlinks the MP4
  beside the extracted sidecar and uses from-google-photos folder mode instead.
  Also learned: --date-from-name exists only on `from-folder`, not `from-google-photos`.

Task 11 prep committed as 3712cb1 (two-pass import documented).
Concurrent agent session still active in this repo (abb2c05 cert-manager work).

PRE-IMPORT GATES REMAINING:
  1. Integrity-check all 53 zips + the MP4  <- running next
  2. Resolve duplicate 049 (Pass 1 hard-fails until then)
  3. Fix valkey before Pass 1 (512Mi hard cap, maxmemory 0 / noeviction = OOMKill under deep queue)
  4. Dry run, then import
