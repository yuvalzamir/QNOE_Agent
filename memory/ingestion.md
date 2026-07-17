# Ingestion & RAG Pipeline
*Last updated: 2026-07-17 (full server re-ingest COMPLETE — 101% coverage, M58 closed; SP coverage audit run — noe-group General at 53%)*

> File discovery, chunking, embedding, Qdrant indexing, watcher daemon, QCoDeS scanner.
> Watcher design: [[WATCHER_PLAN]] · Repo mapping: [[REPO_MAPPING]] · Memory design: [[INFERENCE_MEMORY]]

## Full server re-ingest (2026-07-16) — recovering the silent ~2/3 gap

**Why:** [[memory/mistakes#M58]] — the server doc corpus was ~2/3 UNINDEXED for months with zero errors. Two gap classes: (1) **ACL** — the `/ICFO/groups/NOE` mount uses the `yzamir` cred, ACL-denied on 645+ folders (Theses 19/3345, etc.); (2) **find-timeout** — M7's 300s `find` cap silently truncated big *readable* folders (Manuscripts 311/5450). M7 was fixed in `qcodes_scanner` but never propagated to `run_ingest`. Neither surfaced as an error → looked "covered."

**Fix — one-time full re-ingest through the broad mount:**
- **Read `/mnt/noe`** (cred `sberlanga`, uid=qnoe-ai, mode 0755 → broad access), **store `/ICFO/groups/NOE` paths** (so stored `source` matches what the nightly/agent see). Normalization: `run_ingest._store_key()` rewrites `INGEST_READ_ROOT`→`INGEST_STORE_ROOT`.
- Launcher `scripts/run_full_server_ingest.sh`; orchestrator `agent/ingest/parallel_server_ingest.py`; plan [[FULL_SERVER_INGEST_PLAN]].
- **Cached find-manifest** (`/home/yzamir/qnoe_server_data/full_scan_filelist.txt`, 48,564 files) — no re-`find` on resume; `--refresh-find` to rescan. **No timeout on find** (M7 lesson, finally applied here too).
- **Excludes:** Fabrication (kept, user reversed), Personal, Notebook (77 private per-person notebooks stay `/ICFO`-scoped — must NOT be recovered via the broad mount), `.txt` (raw measurement dumps).
- Coverage-scoped SERVER_FOLDERS expanded to 20 level-0 folders in `ingest_server.py`.

**Parallelism — memory-gated semaphore, NOT ProcessPoolExecutor:** a persistent pool worker ballooned to 31GB on one Docling file → OOM-killer reaped all workers ([[memory/mistakes#M58]] sibling). Replaced with subprocess batches: launch a new `run_ingest --file-list` subprocess (BATCH_SIZE=40 files) only while `concurrent < WORKERS AND MemAvailable ≥ MIN_FREE_GB`. Each batch **exits and frees Docling memory every 40 files** (crash-isolated + resumable). `_mem_available_gb()` reads `/proc/meminfo`. Defaults: WORKERS=8, MIN_FREE_GB=50.

**Speed stack (~5× — days→~1 day):**
- `PDF_TEXTLAYER_FAST=1` — born-digital PDFs (pypdf finds a text layer) take the fast pypdf path, skipping the slow Docling layout pass; only genuinely-scanned (no-text) PDFs go to Docling+OCR (`splitter._chunk_pdf` Step 4).
- `INGEST_STAGE_LOCAL=1` — read each new file **once** over CIFS (`path.read_bytes()`), hash + extract from the same bytes via a `/tmp` temp; CIFS bandwidth is the bottleneck, this halves it.
- `INGEST_SKIP_IF_INDEXED=1` — skip manifest-present files WITHOUT re-reading them over CIFS (makes resume instant). **Nightly leaves this OFF** (it must re-hash to detect changes).
- `DOCLING_MAX_FILE_BYTES=25MB` — skip explosion-prone huge PDF/PPTX.

**Ran as a sprint with vLLM OFF (D-note):** vLLM (gpt-oss-120b, 4×64K KV) holds ~92GB RAM; coexisting with ingest workers (~11GB each) throttled the semaphore to ~0.6 files/min (~48 days). Verdict: **stop vLLM, sprint the ingest (~1 day), agent down meanwhile** — cleaner than a fortnight of degraded coexistence. (I mis-stated vLLM's footprint three times before measuring it — measure `free -g` with the service up, don't trust memory of it.)

**Acceptance + standing check:** `scripts/coverage_audit.py` — per-folder PRESENT (via `/mnt/noe` find-cache) vs INDEXED (manifest under `/ICFO`), flags <80%. `--json`/`--line`. Run it after the sprint as the acceptance test (want all folders ≥ threshold); **STILL OPEN** — wire it into the nightly report (standing check, like the context-block tally) + point the ongoing nightly scan at `/mnt/noe` (with `/ICFO` normalization + Notebook special-case). `scripts/coverage_gap.sh` = the raw ACL-diff (`comm -12` of `/ICFO` unreadable vs `/mnt/noe` readable) that first quantified the gap.

### RESULTS — COMPLETE (2026-07-17 20:56)

**Ran 2026-07-16 14:33 → 2026-07-17 20:56 (~30 h wall).** `1215/1215 batches, 4 failed` (batch-level OOMs, self-handled — never lost, swept after). **Coverage: 48,879 / 48,564 = 101%** — every present file indexed + legacy/duplicate manifest rows (indexed > present because "present" is the *filtered* find-cache while the manifest also carries pre-exclusion `.txt`/Notebook rows and path-variant dupes from older nightlies). **Per-folder acceptance: all folders ≥80% except `Presentations` 26/33 (79%)** — 7-file residual (oversized/unsupported or the OOM-deferred tail); addressed by the straggler sweep (`re-run, cache reused` → skip-if-indexed fast-forwards the 48K, re-processes only the un-done). Recovered the corpus from ~1/3 → effectively complete (M58 closed).

**Operational lessons (reusable for SP + future big ingests):**
- **Floor is a live tuning dial, not set-and-forget.** Started `MIN_FREE_GB=50`; Qdrant's growing in-RAM footprint pushed baseline available under 50 → semaphore throttled to ~1 worker (~4× slowdown). Dropped to **35** (relaunch, resumable) → back to ~5–8 workers. Trade: 35 GB headroom occasionally < a Docling balloon → **4 batch OOMs total over ~30 h, all self-handled** (orchestrator never died, watchdog never fired). Raising the floor alone risks *stalling* (workers can't launch); the better escalation lever on repeated OOM is **fewer workers** (lowers peak), which is what the watchdog does.
- **The bottleneck is CPU + thread oversubscription, NOT worker count.** At 8 workers: `user=98%, idle=0%, iowait=0%`, **loadavg 443 on a 20-core box** — each worker's torch/onnxruntime/BLAS grabs all cores, so hundreds of threads thrash 20 cores. **More workers = slower.** The real speed lever for next time: **cap threads per worker** (`OMP_NUM_THREADS=2` etc.) so 8 workers × ~2–3 threads ≈ matches core count. (Not applied mid-run at 80% — bake into the launcher for SP/future.)
- **DGX-side self-healing beats session-side monitoring.** Interactive background timers kept getting harness-killed, so monitoring + auto-recovery were moved onto the box: `sprint_watchdog.sh` (5-min loop, relaunches on orchestrator death with escalating fewer-workers/higher-floor, self-exits on completion) + `sprint_status_logger.sh` (hourly `coverage/oom/mem` line to `sprint_status.log`). Pattern to reuse for any long unattended job. Both self-exit on `Parallel ingest complete`.
- **vLLM must stay OFF for the whole window** — an accidental `systemctl start vllm.service` mid-run (another agent) exited clean with **no damage** (llama.cpp mmaps, was stopped fast) but ~92 GB would collide instantly with ingest. Nightly cron (`02:00 nightly_run`, `07:00 post_report`) commented out in yzamir's crontab (`#SPRINT-SKIP`, backup `crontab.bak-sprint-20260716`) for the duration.
- **Wind-down order (executed 2026-07-18 00:38, agent back online):** straggler sweep (vLLM off, recovered ~441 files, `1215/1215, 3 failed`) → final `coverage_audit` → `sudo systemctl start vllm.service` → restore crontab (`crontab -l | sed 's/^#SPRINT-SKIP //' | crontab -`). Automated via a detached `winddown.sh` that waits for ingest to fully drain first. Then fix CavityQED stored paths (/mnt/noe→/ICFO) via `--refresh-find` (still pending).
- **Two finish-line gotchas:** (1) a single **pathological Docling file can hang a worker ~20 min** — state `Rl` (CPU-grinding a complex/scanned PDF's layout, not I/O-hung `D`), 16 GB RSS, **zero log output** — and block run completion; `kill <batch pid>` lets the orchestrator finish (`N failed`, batch resumable). Diagnose via log-age + `ps -o stat,rss`. (2) After a long vLLM-off window the **Hermes gateway is left `inactive`** — restarting vLLM alone does NOT restore the agent; also `sudo systemctl start qnoe-hermes-sandbox.service`, and only **after** the LLM endpoint actually serves (`curl :8000/v1/models`, ~40 s mmap warm-up) or the gateway boots against a dead LLM.
- **Residual (oversized files):** `Presentations` finished 26/33 (79%) — the 7 misses are >25 MB (`DOCLING_MAX_FILE_BYTES`) / unsupported PDFs that skip by design (`WARNING Oversized (44 MB): …`). Not a coverage bug; raise the cap for a targeted pass only if those specific docs matter.

## Ingestion CLI

`agent/ingest/run_ingest.py` — hash-based dedup via `index_manifest` SQLite table.

Key options: `--team`, `--repo-path`, `--force`, `--file-list`, `--dry-run`

Server ingestion uses separate manifest: `AGENT_DATA_DIR=/home/yzamir/qnoe_server_data`

## Supported Extensions

`.py`, `.ipynb`, `.md`, `.txt`, `.rst`, `.pdf`, `.pptx`, `.docx`

Docling used for PDF/DOCX/PPTX (50MB cap). Oversized files logged to `/tmp/oversized_files.log`.

## Exclusions — Single Source of Truth

`agent/ingest/excluded.py` reads `config/watcher.yaml` → `find_prune_args()`.
All `find` commands (run_ingest, qcodes_scanner) use this function.

Excluded folders: QDphotodetector, TopoNanop, HighQuality_Plamons, Low_temperature_polaritons, mid-IR_Plasmonic_detector_Seb, Graphene Optomechanics, `Personal/Sergi/QTM - Copy` (bundled Python env).

**Path substring exclusions** (`exclude_path_substrings` in watcher.yaml, applied via `find ! -path` in `_targeted_find`):
`/PyInstaller/`, `/_pyinstaller/`, `/venv/`, `/.venv/`, `/site-packages/`, `/node_modules/`, `/__pycache__/`, `/.ipynb_checkpoints/`

**Parallel change queue runner** (`/tmp/parallel_queue.py`): 6-worker ProcessPoolExecutor, runtime path filter matching same exclusions, `mark_processed` called after all workers complete. Use when change queue has large backlog (e.g. after manifest DB reset). Command: `cd /opt/qnoe-agent && N_WORKERS=6 setsid bash -c 'nohup venv/bin/python /tmp/parallel_queue.py >> logs/parallel_queue.log 2>&1' > /dev/null &`

## Qdrant Collections

8 collections: `group-wide`, `qtm`, `photocurrent`, `qed`, `superconductivity`, `qsim`, `xchiral`, `qcodes-runs`

Mapping rules: `config/repo_collections.yaml`

## QCoDeS Scanner

`agent/ingest/qcodes_scanner.py` — async, incremental, stat-based fingerprint (size + mtime).

- 75 DBs, 75,477 runs indexed (as of 2026-06-30)
- No timeout on `find` (CIFS scan takes 2h+)
- Column name: `run_description` (not `description`) — see [[memory/mistakes#M6 — QCoDeS column name]]

## Watcher Daemon

`agent/watcher/smb_watcher.py` — 14 tests pass.

- `watch_subfolder_level`: Projects, Notebook, Notebooks, Setups, Personal, Fabrication
- `Notebook/Antenna+graphene` intentionally KEPT
- Cache: ~37K files, full rebuild ~44 min
- Change queue processed by nightly `task_process_change_queue()`

## SharePoint Pipeline

**New source added 2026-07-03.** Two Teams sites indexed via Microsoft Graph API.

Files: `agent/ingest/sharepoint_client.py` (Graph API wrapper), `agent/ingest/sharepoint_sync.py` (full + delta sync).

**Key design decisions:**
- No local file cache — each file streamed to `/tmp/qnoe-sharepoint/`, chunked, embedded, temp deleted immediately (even on failure via `try/finally`)
- **Dedup:** etag from Graph API (not SHA-256) stored in `sp_manifest` table in `/opt/qnoe-agent/memory/sharepoint.db`
- **Delta sync:** Graph delta API, delta links stored in `sharepoint_delta` table in watcher DB
- **Token refresh:** auto-refreshes every 45 min mid-sync (MSAL tokens expire at 60 min — critical for large syncs)
- **Exclusions:** checked per-item at processing time (not during listing); `list_drive_items()` always walks full tree
- **Source field** in Qdrant chunks: set to SharePoint web URL (not temp path)
- **`repo` field:** set to site name (`twisted-materials` or `noe-group`)

**Flow:** `SharePointPoller` thread (every 30 min) → `delta_sync()` → if no baseline → `full_sync()` → `list_drive_items()` → per-item: download → `chunk_file()` → `embed_documents()` → `_upsert_chunks()` → `_record_item()` → `dest.unlink()`

**First run timing:** Full listing of 2429-item drive takes ~2.5 min. Docling PDFs 30s–3min each. NOE-Group is larger — expect 2–4h total first sync.

**Orphan cleanup:** `sweep_orphans()` in `run_ingest.py` only covers `index_manifest`. SP chunks tracked via `sp_manifest` — no orphan sweep yet (future work).

**`web_url` column (2026-07-10, for `find_file` tool):** `sp_manifest` now stores the SharePoint web link. `_record_item()` writes it on every delta/full sync (auto-migrates via `PRAGMA`/`ALTER` in `_get_sp_manifest_conn`). Existing 22,102 rows backfilled from Qdrant chunk payloads (`source` field) by `agent/indexing/backfill_sp_weburl.py` — idempotent, only touches `web_url IS NULL/''`, batched `client.retrieve` per collection. Wired into nightly `task_sync_sharepoint` as a safety net (runs AFTER the sites loop → skipped if SP auth fails that night; the one-time backfill already made the manifest 100% complete).

**Nightly SP creds — ANALYZED 2026-07-10, already fixed (no action needed):** older nightly `task_sync_sharepoint` called `authenticate()` without loading the secrets file, so it failed under cron (`Missing credentials`, traceback at old `nightly_run.py:154`) — the cron runs as `yzamir`, whose env lacks `SHAREPOINT_USERNAME/PASSWORD`. The current code loads `/opt/qnoe-agent/secrets/sharepoint.env` (640 qnoe-ai:qnoe-ai; `yzamir` reads it via the qnoe-ai group) via `os.environ.setdefault` before auth. Log confirms the 3 failures were on the OLD code; the last 2 runs (incl. Jul 10 02:01 cron) returned `OK` (processed=0 is normal — the 30-min poller already advanced the shared delta link). Replaying the loader as `yzamir` sets both vars non-empty. Residual brittleness only: creds depend on that one file + in-code parse (rotate/move breaks it), and the new web_url backfill sits *after* `authenticate()` so it's skipped on any night auth fails.

## SharePoint coverage audit (2026-07-16) — SP had its own silent gap

**`scripts/sharepoint_coverage_audit.py`** (per [[SHAREPOINT_COVERAGE_AUDIT_PLAN]]) — read-only present-vs-indexed reconciliation, the SP analog of `coverage_audit.py`. Streams the Graph delta listing page-by-page (never accumulates the ~676K-page noe-group item dicts — RAM-safe during the ingest sprint), mirrors the sync's exact filters (extensions, exclude_folders, `EXCLUDE_PATH_SUBSTRINGS`, `max_file_mb`) so present = "sync should have indexed it", reads `sp_manifest` with `mode=ro&immutable=1` (M52 cross-UID WAL trap), Qdrant point-count cross-check, tenant `/sites?search=*` discovery, 401/403 → "denied" section. **Exit code 1 = findings (unit shows "failed" — by design).** Runs as qnoe-ai via one-shot `qnoe-sp-audit.service` (EnvironmentFile-style `source` of `secrets/sharepoint.env`); output → `logs/sp_coverage_audit.{txt,log}`. Full noe-group run ≈ 43 min (~3,400 delta pages @ ~1.5 pages/s).

**First-run findings (2026-07-16, audit only — re-sync deliberately NOT run):**
- **noe-group: 53% coverage in General** — 13,376/25,058 indexed, ~11.7K files missing (Conferences, Grants, Proposals…). Patents 60/94 (63%), NOE-FAB 5/8. Site totals: present 25,261 / indexed 21,859 / missing 11,722 / **orphans 8,320** (indexed rows whose SP path no longer exists — no SP orphan sweep exists, see above).
- **twisted-materials healthy:** QTOM 97%, SpectroMag 93%; 55 orphans (likely pre-exclusion rows: General/OneNote Uploads/etc. were indexed before `exclude_folders` landed).
- **Filter gap found:** SP sync's `EXCLUDE_PATH_SUBSTRINGS` lacks `.ipynb_checkpoints/` (watcher.yaml has it for CIFS; M56 class) — 1 junk file present, flag it when next touching the sync.
- **Tenant scan:** 45 sites visible to the credential, 43 not in config — mostly ICFO-wide social/admin (deliberate exclusions), but **"QNOE AI" (`/sites/QNOEAI`) and "Galleries" (`/sites/NOE-Group/Galleries`, a NOE-Group subsite)** look lab-relevant → user decision whether to add to `config/sharepoint.yaml`.
- **Denied section: empty** (no 401/403). Qdrant cross-check **inconclusive** — point-count timed out (30s) under ingest-sprint load; re-run when the box is idle.
- Remediation (NOT executed): `sharepoint_sync.py --full --site noe-group` (etag dedup makes it incremental) + wire the audit into the nightly report.

**ACCEPTANCE AUDIT + ORPHAN SWEEP (2026-07-18, run #2 of the audit, ~45 min, with `--dump-live`):**
- **Recovery confirmed:** Patents 63%→**92%**, NOE-FAB 62%→**87%**, Meetings 96%; `.ipynb_checkpoints` gone from the gap list (exclusion live). twisted-materials 94/97%.
- **General still flags 56% — but the residual gap is DELIBERATE, not silent:** present-not-indexed = 10,403 ≈ 9.2K plot-export/figure PDFs (no-text, skipped by policy) + 957 scanned PDFs (user dropped the OCR pass 2026-07-18) + small tail (reingest failures + files added while the agent/poller was down). ⚠️ **The nightly `task_sp_coverage` will keep flagging General <80% until a policy call:** either add the plot-export class to the sync+audit filters (makes the standing check meaningful again) or accept the standing flag. **USER DECISION.**
- **"8,320 orphans" premise REVERSED:** by item_id vs the live dump, **0 manifest rows are truly gone and only 1 moved** — the "orphans" were rows indexed under OLD rules that the audit's present-filter now excludes: **7,717 `.ipynb_checkpoints/` notebooks** (indexed by the original full sync before the exclusion existed — M56 class) + **1,831 legacy `.txt`** (pre-scope-change). Audit-orphans ≠ deletions; check ids before believing "gone".
- **Sweep EXECUTED (`sp_orphan_sweep.py --purge-excluded --execute`):** deleted 7,719 junk rows + **230,277 Qdrant points** (checkpoint notebooks are chunk-heavy — big retrieval-noise cut in group-wide); manifest 23,348→15,629; backup `sharepoint.db.bak-pre-sweep-20260718`; 0 orphans. **OPEN: the 1,831 legacy `.txt` rows** (a different class — needs an ext-purge mode + user nod).
- **Nightly standing check LIVE:** `task_sp_coverage` in nightly_run (90-min cap, warns on gaps, FAILURE on crash, refreshes `logs/sp_live_items.jsonl` for future sweeps); tested end-to-end via `SP_AUDIT_EXTRA_ARGS='--site twisted-materials'` (41s, OK). Qdrant cross-check timed out again under the parallel CIFS job (30s cap) — still pending one idle-box run.
- **M50 near-miss during deploy:** live `sharepoint_sync.py` carried the OTHER session's uncommitted forkserver + sp_retry_queue improvements — diffed first, mirrored live→repo verbatim (commit 7d08cd4), THEN applied the exclusion on top. Always `md5`-compare live vs `git show HEAD:` before overwriting a deployed file.

**Targeted re-ingest EXECUTED 2026-07-17 (~2h, semaphore 6×25-item recycled batches, PDF_TEXTLAYER_FAST=1, chunk timeout 1800s):** 98/98 batches, **0 failed batches, 0 chunk timeouts**; of 2,443 attempted **1,218 indexed** (+1,213 manifest rows → 23,072). **General coverage 53% → 91%** (vs Jul-8 present set). Still missing 1,225 (`logs/sp_reingest_failed.txt`): **957 are no-text-layer scanned/image PDFs** (in `/tmp/empty_pdfs.log`; recoverable only via a DOCLING_OCR=1 pass — user decision, slow), ~22 Graph 404s (deleted/moved since Jul-8, incl. `PhD_Thesis_Riccardo_FINAL_20260703.pdf` — the `posters/` chapter copy WAS recovered), remainder (~250 ipynb/py/pptx/docx) unclassified — spot-check before chasing. Note: run indexed some `.ipynb_checkpoints` junk (SP sync still lacks that exclusion). Fresh `sharepoint_coverage_audit.py` re-run (~45 min listing) still pending as formal acceptance.

**Missing-set forensics (2026-07-17, via the surviving Jul-8 listing cache `/tmp/qnoe-sp-listing-cache/b_6T8n2h74….jsonl` + `logs/sharepoint_full_sync.log`):** the 11,723 missing noe-group files are NOT a path artifact (samples confirmed absent from manifest) and NOT one contiguous resume hole (misses scattered across listing order). Breakdown by class: **(A)** 7,809 numeric plot-export PDFs (`…/Notebooks/…/pdf/118_2.pdf`, QCoDeS notebook backups; 213 MB) + 1,471 manuscript-figure PDFs (1.4 GB) — near-zero text value; **(B)** 1,121 real PDFs ≥100 KB (**8.1 GB**) incl. 733 files under `Theses…/Thesis` (Riccardo's FINAL PhD thesis 160 MB) and all Fastera Nature-Comm manuscript versions (87.8 MB each) — the part that matters; **(C)** 203 pptx (2.3 GB) + 171 ipynb (1.8 GB) + 69 docx; **(D)** 131 `.py` + 13 `.md` — trivial text files that can't legitimately fail. **Root causes (log-confirmed):** (1) Docling 300s kill-on-timeout + OOM worker crashes (`BrokenExecutor`) under memory pressure — the final Jul-9 resume run logged **1,515** "chunk_file timed out or worker crashed" ERRORs yet reported `errors: 0, failed_files: []` (they're counted as "skipped", lumped with etag-skips); (2) **fail-once = fail-forever** — a failed/zero-chunk file writes NO manifest row and NO failure record, and the 30-min delta poller only revisits CHANGED items, so an unchanged failed file is never retried until the next `--full`; (3) `--skip-files N` resumes skipped listing ranges wholesale, so files attempted only in a crashed earlier window were never re-tried (proof: the `.ipynb_checkpoints` COPY of `lakeshore_model_335.py` is indexed while the real one is missing). Fix directions: record failures (activity log `skipped_files` exists for delta but not full_sync), retry queue for failed items, larger timeout for thesis-size PDFs on an idle box, and consider excluding class-A plot exports by policy.

## Collection health audit — group-wide growth & SP duplicates (2026-07-10)

Triggered by a health-check flag: `group-wide` grew ~634K (07-09 BM25 snapshot) → **1,060,398** points, and the question was whether the web_url work duplicated content.

- **web_url backfill is NOT the cause — it adds zero Qdrant points.** `backfill_sp_weburl.py` does read-only `client.retrieve` + a SQLite `UPDATE`. SP payloads already carried the URL in `source` (that's what it copies *from*), so the "old points without URLs / new with" framing doesn't apply.
- **The +426K is legitimate SharePoint content**, not my change. `sp_manifest` (22,102 files) owns **406,848** chunks (noe-group 405,274 · twisted-materials 1,574) — ~= the growth. The 634K baseline was measured before the big SP full sync finished landing.
- **No large-scale duplication.** Method: manifest `point_ids` are the *authoritative current* SP ids; any SP point in Qdrant not in that set = an orphaned duplicate. Sampled 120K points (Qdrant scrolls in ~random UUID order): 45,844 SP points, only **94 orphans = 0.2%** (a re-add-without-delete would be ~50%). SP ingest upserts correctly (`_process_item` → `_delete_old_chunks` by stored ids → `_upsert_chunks`, keyed by `item_id`). BM25 stats not meaningfully skewed.
- The 2,682 `(source,start_line)` collisions in the sample are **not** dup pairs — both members are in the current manifest; they're multiple current chunks of one file sharing a `start_line` (page-relative offsets / text-vs-table). Benign.
- **Payload fields:** `text, source, repo, chunk_type, start_line`. **No payload indexes** (`payload_schema: []`) → exact `count` with a `repo` filter hits Qdrant's 60s server-side timeout. A `repo` payload index would make health-check counts instant (candidate follow-up, not done). SP source = web URL (`http…`); CIFS/server source = filesystem path, repo like `server/Notebook`.
- Residual: ~0.2% SP orphans (~800 points extrapolated) — trivial; would be cleared by the not-yet-built SP orphan sweep (see above).

## Nightly Tasks

1. `task_qdrant_snapshot` — snapshot all collections, prune >7 days
2. `task_index_repos` — incremental re-index of 41 GitHub repos
3. `task_sync_sharepoint` — full sync of both SP sites (safety net for missed deltas)
4. `task_process_change_queue` — process watcher's stable entries
5. `task_orphan_cleanup` — remove chunks for files missing 7+ days
6. `task_context_blocks` — read-only summary of threat-scanner context drops (24h) from the hourly `qnoe-context-tally.timer` outputs in `logs/`; stale/missing tally = task FAILURE, never "clean" (2026-07-16, see [[memory/agent-code#Context-block tally]])

## Ingestion Stats

- 41 GitHub repos → 7 Qdrant collections
- Server: all 12 folders indexed
- QCoDeS: 75 DBs, 75,477 runs
- RAG eval: 17/20 queries relevant (85%)
- BM25 hybrid search planned: [[PHASE2_BACKLOG#B1 — BM25 hybrid search (L2)]]
