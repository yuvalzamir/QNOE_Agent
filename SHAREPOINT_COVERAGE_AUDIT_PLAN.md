# SharePoint Coverage Audit — Plan

*Created 2026-07-16. Owner: (another agent). Author of this spec was focused on the server re-ingest sprint and did NOT implement this.*

## Why

The server document corpus was silently ~2/3 unindexed for months ([[memory/mistakes#M58]]). The root lesson was **not** the two CIFS bugs that caused it — it was that **no reconciliation existed** between *what's present* and *what's indexed*. `scripts/coverage_audit.py` fixed that for the CIFS server (per-folder present-vs-indexed, flags <80%).

**SharePoint has the identical blind spot.** Its manifest (`memory/sharepoint.db`) is an **etag dedup** table, not a coverage check. Nothing today tells us "files enumerable via Graph" vs "files indexed." This plan builds that reconciliation.

Note: the two *server* failure modes (M7 `find` timeout, SMB ACL on the `/ICFO` mount) do **not** apply to SharePoint — it ingests via the Microsoft Graph API (`agent/ingest/sharepoint_sync.py`), no `find`, no SMB mount. But SharePoint has its **own** analogous silent-gap classes (below).

## Gap classes to detect (SharePoint-specific)

1. **Un-enumerated sites / libraries (the SP analog of the ACL gap) — HIGHEST PRIORITY.**
   - Config (`config/sharepoint.yaml`) ingests only two sites: `noe-group` (all) and `twisted-materials` (scoped to **QTOM + SpectroMag + THz laser only**).
   - Any other SharePoint site in the tenant, or any document library the app credential was never granted, is **never enumerated → silently absent**. This is the biggest unknown.
2. **Docling timeout / oversize skips.** The full-sync uses Docling **kill-on-timeout** and `max_file_mb:300` (+ `DOCLING_MAX_FILE_BYTES`). Every file that timed out or exceeded the cap was dropped **with no error**.
3. **OOM-interrupted syncs.** Full-sync history includes several 24-worker OOM crashes. etag-dedup makes resume *possible*, but a crashed sync never cleanly re-run to completion leaves holes.
4. **Extension filter drift.** `.txt` was removed from the extension list; anything outside the configured extensions is intentionally skipped — the audit must apply the **same** filter so an intentionally-excluded file is NOT counted as a gap (apples-to-apples).

## What to build

`scripts/sharepoint_coverage_audit.py` — model it on `scripts/coverage_audit.py` (per-container present-vs-indexed, `--json` / `--line`, threshold flag). It is **read-only** (Graph GETs only, no Docling, no embedding) → light, no box contention, **safe to run during the server sprint**.

### Behavior

1. **Auth:** reuse the token/auth logic from `agent/ingest/sharepoint_sync.py` (creds `secrets/sharepoint.env`, 640/qnoe-ai; token refresh 45 min). **Must run as `qnoe-ai`** (owns the secret). `yzamir` cannot `sudo -u qnoe-ai` non-interactively — the executing agent deploys to `/opt/qnoe-agent/scripts/` and the human runs it, or it runs via a `qnoe-ai` cron/systemd.
2. **Enumerate PRESENT (per configured site → each document library → recurse all files)** via Graph `drive/items` children. Apply the **same extension + `max_file_mb` filters** the sync uses, so the "present" set = "should-be-indexed" set.
3. **Enumerate INDEXED:** count from `memory/sharepoint.db` (authoritative sync manifest) and/or Qdrant `group-wide` points filtered `repo="<site>"`. Cross-check both — a divergence between sharepoint.db and Qdrant is itself a finding (manifest says indexed but points absent = a purge/half-write).
4. **Diff → per-library `present` vs `indexed`, flag `< threshold` (default 80%).** List a sample of missing file paths per flagged library.
5. **Tenant-wide site discovery (gap class 1):** call Graph `/sites?search=*` (or `/sites/getAllSites` if permitted) to list **all sites the app credential can see**, and diff against the two configured sites → report **sites present in the tenant but NOT in our config**.
6. **Permission-denied report:** any site/library that returns 403/401 during enumeration → list explicitly as "credential cannot see" (the un-granted-access gap).

### Output

- `--json` and `--line` (match `coverage_audit.py` conventions).
- Three sections: (a) per-library coverage % with flagged (<threshold) rows + sample missing files; (b) tenant sites not in config; (c) sites/libraries the credential is denied.

## Acceptance criteria

- Every **configured** document library reports ≥ threshold coverage, OR the shortfall is explained (all misses are policy-excluded extensions/oversize/known-private).
- Section (b) is reviewed: each tenant site not in config is a deliberate exclusion, not an oversight.
- Section (c) is empty, or every denied library is intentionally out of scope.

## Follow-ups (after the audit reveals the number)

- If gaps found: re-run `sharepoint_sync.py --full [--site X]` for the affected sites (etag-dedup skips already-indexed, so it's incremental), OR add missing sites/libraries to `config/sharepoint.yaml` and full-sync them.
- **Wire the audit into the nightly report** as a standing check (same pattern requested for `coverage_audit.py` — see [[TODO]]), so this silent-gap class is caught automatically going forward.

## Reference facts (SharePoint pipeline, from vault `memory/infrastructure.md` §SharePoint + MEMORY.md)

- Ingestion: `agent/ingest/sharepoint_sync.py`. Config `config/sharepoint.yaml`; creds `secrets/sharepoint.env` (640/qnoe-ai). Manifest `memory/sharepoint.db` (etag dedup). Token refresh 45 min (expire 60).
- Stored in Qdrant `group-wide` collection with payload `repo="<site>"` (e.g. `repo="noe-group"`, `repo="twisted-materials"`). Purge = `POST /collections/group-wide/points/delete` filter `repo="<site>"`.
- Existing CLI: `--validate` · `--full [--site X] [--keep-cache]`. Full-sync internals (all in `sharepoint_sync.py`): JSONL streaming cache (avoids 80–90 GB OOM), `psutil` submission guard `MIN_FREE_GB=20`, workers ≤20 (24+ OOMs), Docling kill-on-timeout, `_SharedToken` 45-min refresh, `ProcessPoolExecutor(1)` per file, `.txt` removed from extensions, `max_file_mb:300`.
- Model the new script on `scripts/coverage_audit.py` (present-vs-indexed reconciliation, `--json`/`--line`, `<threshold` flag).
