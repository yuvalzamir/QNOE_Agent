# Full Server Ingest Plan — recover the `/ICFO`-denied folders via `/mnt/noe`

*Prepared 2026-07-16. Status: **STAGED, awaiting final GO to run.** Everything below is written + deployed + dry-run-validated; nothing has been executed against the live corpus.*

## Problem
The NOE share is mounted twice with different SMB creds:
- `/ICFO/groups/NOE` → cred **`yzamir`** — ACL-denied **645+ folders** (whole `Fabrication`, `Data Backup`, per-person dirs, …). **This is what the nightly scans**, so those folders are silently unindexed.
- `/mnt/noe` → cred **`sberlanga`** (uid=qnoe-ai) — can read all of them. `yzamir` can read `/mnt/noe` (mode 0755), so the credential — not the local user — is the lever.

Diagnostic to re-check anytime: `bash scripts/coverage_gap.sh` (lists denied-on-`/ICFO`, readable-on-`/mnt/noe`).

## Approach
Read via `/mnt/noe`, **store canonical `/ICFO/groups/NOE` paths** (so already-indexed files dedupe by hash — no duplicate points — and `find_file` stays on `/ICFO` paths). Parallelized, resumable, run with vLLM stopped.

## Scope (allowlist in `ingest_server.SERVER_FOLDERS`)
**INCLUDE (20):** Lab_Instruments, Manuscripts, Matlab scripts, Meetings, Notebook, Notebooks, Papers & Books, Posters, Presentation, Presentations, Projects, Python scripts, QCoDeS, QTLab, Samples, Scripts, Setups, Spectromag, Teaching, Theses & reports.
**EXCLUDE (per your decision):** `Fabrication`, `Personal`; plus junk/archive `Data Backup`, `ai_agent`, `Pictures`, `Rendering Files`, `National Instruments Downloads`, `.obsidian`, `Obsidian`, `.TemporaryItems`. Per-file junk (`venv`, `__pycache__`, `.ipynb_checkpoints`, `Personal/Sergi/QTM - Copy`) pruned by `watcher.yaml`.
**OPEN DECISION — `.txt`:** the server has thousands of raw-measurement `.txt` (1 chunk each, e.g. "PH freqsweep…"). Default here = **include** (don't silently drop; some are readmes). To skip: set `EXCLUDE_EXTENSIONS=.txt`. Your call before we run.

## What was changed (staged in the repo + deployed to DGX)
1. **`agent/ingest/run_ingest.py`**
   - `_store_key(path)` + `INGEST_READ_ROOT`/`INGEST_STORE_ROOT` env → normalize manifest key + Qdrant `source` payload (`/mnt/noe`→`/ICFO`). No-op when unset (existing callers unaffected).
   - `_find_files`: **removed the 300s find timeout** (M7). Runs to completion.
   - `_get_manifest_conn`: `PRAGMA busy_timeout=120000` for concurrent workers. **No WAL** (cross-UID WAL side-files break the sandboxed find_file reader — M52).
   - `ingest_directory(..., list_force=True)`: parallel runner passes `list_force=False` so a file-list run keeps hash-dedup (resumable), instead of force-reindexing.
2. **`agent/ingest/ingest_server.py`** — expanded `SERVER_FOLDERS` (the 8 new folders) + documented exclusions.
3. **`agent/ingest/parallel_server_ingest.py`** (new) — cached find-manifest + `ProcessPoolExecutor` workers.
4. **`scripts/run_full_server_ingest.sh`** (new) — launcher that sets all env.

## Run procedure (on your GO)
1. **Free the box:** `sudo systemctl stop vllm.service` (frees ~115 GB RAM + CPU for embedding/Docling). *(gateway can't answer without the LLM anyway — expected downtime.)*
2. **Launch (as yzamir):** `bash /opt/qnoe-agent/scripts/run_full_server_ingest.sh 12` → runs in `screen`/`nohup`, logs to `/home/yzamir/full_ingest.log`.
   - First does the **un-timed CIFS find** (may take a while) → caches the file list to `/home/yzamir/qnoe_server_data/full_scan_filelist.txt`.
   - Then 12 worker processes ingest in parallel; per-file sha256 dedup skips already-indexed content.
3. **Monitor:** `tail -f /home/yzamir/full_ingest.log`; `curl :6333/collections/group-wide` for point growth.
4. **When done:** `sudo systemctl start vllm.service` (restore inference).

## Resumability / crash recovery
- **Interrupted / crashed?** Just re-run the launcher. The **find-cache** skips the slow find; the **sha256 manifest** skips every file already indexed — so a re-run only does the remainder. (`--refresh-find` forces a fresh find; use only if folders changed.)
- A worker segfault (Docling) drops that shard's remainder; the re-run mops it up.

## After the first full run
- **Nightly currency:** point the nightly at `/mnt/noe` (set `SERVER_ROOT=/mnt/noe` + the `INGEST_*` roots in the cron) so new/edited files in these folders stay indexed. (Follow-up; the nightly is incremental so single-process is fine.)
- **CavityQED fix-up:** the earlier CavityQED ingest stored `/mnt/noe` paths; a `--refresh-find` full run re-keys them to `/ICFO` (the old `/mnt/noe` manifest rows + points should then be purged — small cleanup, tracked separately).

## Rollback
Points land in `group-wide` only (additive). To undo: `POST /collections/group-wide/points/delete` filtered on the new `repo="server/<folder>"` tags, and delete the matching `index_manifest` rows. No existing content is modified (dedup skips unchanged).

## Validation done before GO
- `run_ingest` / `parallel_server_ingest` import + syntax OK.
- **Dry-run** (`--dry-run`) prints per-folder file counts + read→store path mapping, writes nothing. (Results in the message that accompanies this plan.)

**→ Awaiting your final GO to stop vLLM and run.**
