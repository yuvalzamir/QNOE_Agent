"""Nightly maintenance runner for the QNOE lab agent.

Each task is a plain function registered in TASKS (bottom of this file).
Tasks run in order; a failure is logged but does not stop remaining tasks.

To add a new nightly task:
  1. Write  def task_<name>() -> None  — raise on failure, log progress via logger
  2. Append it to TASKS

Usage:
  python -m agent.indexing.nightly_run            # run all tasks
  python -m agent.indexing.nightly_run --dry-run  # print plan without executing
  python -m agent.indexing.nightly_run --task qdrant_snapshot  # run one task
"""
import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

from agent.ingest.run_ingest import ingest_directory
from agent.ingest.qcodes_scanner import scan_roots as scan_qcodes, scan_specific_dbs

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (all overridable via environment variables)
# ---------------------------------------------------------------------------

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
REPOS_DIR = Path(os.environ.get("REPOS_DIR", "/opt/qnoe-agent/repos"))
COLLECTIONS_CONFIG = Path(os.environ.get(
    "COLLECTIONS_CONFIG", "/opt/qnoe-agent/config/repo_collections.yaml"
))
SERVER_ROOT = Path(os.environ.get("SERVER_ROOT", "/ICFO/groups/NOE"))
SNAPSHOT_RETENTION_DAYS = int(os.environ.get("SNAPSHOT_RETENTION_DAYS", "7"))
# AGENT_DATA_DIR must match where ingest workers write their manifest DB.
# The server ingestion job uses /home/yzamir/qnoe_server_data; repo ingestion uses
# /opt/qnoe-agent/memory. Pass explicitly so the two jobs never share a manifest file.
AGENT_DATA_DIR = Path(os.environ.get("AGENT_DATA_DIR", "/opt/qnoe-agent/memory"))
SERVER_DATA_DIR = Path(os.environ.get("SERVER_DATA_DIR", "/home/yzamir/qnoe_server_data"))

# Per-task wall-clock limits (seconds). Exceeded tasks are logged and skipped;
# remaining tasks still run. signal.alarm only works in the main thread.
TASK_TIMEOUTS = {
    "task_qdrant_snapshot":        5 * 60,   #  5 min
    "task_index_repos":           3 * 3600,  #  3 h
    "task_sync_sharepoint":       5 * 3600,  #  5 h
    "task_process_change_queue":  1 * 3600,  #  1 h
    "task_orphan_cleanup":        10 * 60,   # 10 min
    "task_context_blocks":        5 * 60,    #  5 min (pure file reads)
    "task_sp_coverage":           90 * 60,   # 90 min (full Graph listing ~45 min)
    "task_server_sweep":          3 * 3600,  #  3 h (un-timed /mnt/noe find ~40-60 min + new files)
    "task_server_coverage":       15 * 60,   # 15 min (reads the find-cache the sweep refreshed)
}
_DEFAULT_TASK_TIMEOUT = 3600  # 1 h for any unlisted task


def _alarm_handler(signum, frame):
    raise TimeoutError("task exceeded wall-clock limit")


LOGS_DIR = Path(os.environ.get("AGENT_LOGS_DIR", "/opt/qnoe-agent/logs"))
REPORT_JSON = LOGS_DIR / "nightly_report.json"


class _CapturingHandler(logging.Handler):
    """Captures all log records during a task for the nightly report."""
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _parse_ingest_stats(records: list[logging.LogRecord]) -> dict:
    """Extract indexing stats and failed files from captured log records."""
    files_indexed = chunks_added = files_failed = 0
    new_files = updated_files = 0
    failed_files: list[str] = []
    for r in records:
        msg = r.getMessage()
        # New format: "Done. Indexed N chunks from M files (A new, B updated, C skipped unchanged)."
        m = re.search(r"Indexed (\d+) chunks from (\d+) files \((\d+) new, (\d+) updated", msg)
        if m:
            chunks_added += int(m.group(1))
            files_indexed += int(m.group(2))
            new_files += int(m.group(3))
            updated_files += int(m.group(4))
        elif re.search(r"Indexed (\d+) chunks from (\d+) files", msg):
            # Legacy format without new/updated breakdown
            m2 = re.search(r"Indexed (\d+) chunks from (\d+) files", msg)
            if m2:
                chunks_added += int(m2.group(1))
                files_indexed += int(m2.group(2))
        if r.levelno >= logging.WARNING:
            # "Could not open PPTX /path/to/file: ..."
            m3 = re.search(r"(?:Could not open|failed for|error.*?)\s+(/\S+)", msg, re.I)
            if m3:
                failed_files.append(m3.group(1))
                files_failed += 1
    return {
        "files_indexed": files_indexed,
        "new_files": new_files,
        "updated_files": updated_files,
        "chunks_added": chunks_added,
        "files_failed": files_failed,
        "failed_files": failed_files,
    }


SERVER_FOLDERS = [
    "Lab_Instruments", "Manuscripts", "Meetings", "Notebook", "Notebooks",
    "Papers & Books", "Posters", "Presentation", "Presentations", "Projects",
    "Spectromag", "Theses & reports",
]


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def task_qdrant_snapshot() -> dict:
    """Snapshot all Qdrant collections; prune snapshots older than SNAPSHOT_RETENTION_DAYS."""
    resp = requests.get(f"{QDRANT_URL}/collections", timeout=10)
    resp.raise_for_status()
    collections = [c["name"] for c in resp.json()["result"]["collections"]]
    logger.info("Snapshotting %d collections: %s", len(collections), collections)

    created_count = 0
    for col in collections:
        r = requests.post(f"{QDRANT_URL}/collections/{col}/snapshots", timeout=60)
        r.raise_for_status()
        logger.info("  created snapshot: %s", col)
        created_count += 1

    # Prune snapshots older than retention window
    pruned_count = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=SNAPSHOT_RETENTION_DAYS)
    for col in collections:
        r = requests.get(f"{QDRANT_URL}/collections/{col}/snapshots", timeout=10)
        r.raise_for_status()
        for snap in r.json()["result"]:
            raw = snap["creation_time"]
            created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created < cutoff:
                name = snap["name"]
                requests.delete(
                    f"{QDRANT_URL}/collections/{col}/snapshots/{name}", timeout=10
                ).raise_for_status()
                logger.info("  pruned old snapshot: %s / %s", col, name)
                pruned_count += 1
    return {"collections": len(collections), "snapshots_created": created_count, "snapshots_pruned": pruned_count}


def task_index_repos() -> dict:
    """Incremental re-index of all cloned GitHub repos (skips unchanged files)."""
    if not REPOS_DIR.exists():
        raise FileNotFoundError(f"Repos directory not found: {REPOS_DIR}")
    if not COLLECTIONS_CONFIG.exists():
        raise FileNotFoundError(f"Collections config not found: {COLLECTIONS_CONFIG}")

    with open(COLLECTIONS_CONFIG) as f:
        config = yaml.safe_load(f)

    excluded = set(config.get("exclude", []))
    repos = sorted(d for d in REPOS_DIR.iterdir() if d.is_dir() and not d.name.startswith("."))
    repos = [r for r in repos if r.name not in excluded]
    logger.info("Re-indexing %d repos", len(repos))

    # Stats are extracted from log capture in run(); return repo count for reference.
    for repo in repos:
        collection = _resolve_collection(repo.name, config)
        logger.info("  %s -> %s", repo.name, collection)
        ingest_directory(
            team=collection, repo_path=repo, repo_name=repo.name, force=False, dry_run=False,
            manifest_db=str(AGENT_DATA_DIR / "episodic.db"),
        )
    return {"repos_scanned": len(repos)}


def task_index_server() -> None:
    """Incremental re-index of NOE server documents (skips unchanged files)."""
    if not SERVER_ROOT.exists():
        raise FileNotFoundError(f"Server not mounted: {SERVER_ROOT}")
    logger.info("Re-indexing server docs (%d folders)", len(SERVER_FOLDERS))

    for folder_name in SERVER_FOLDERS:
        folder = SERVER_ROOT / folder_name
        if not folder.exists():
            logger.warning("  folder not found, skipping: %s", folder)
            continue
        logger.info("  %s", folder_name)
        ingest_directory(
            team="group-wide", repo_path=folder, repo_name=folder_name, force=False, dry_run=False,
            manifest_db=str(SERVER_DATA_DIR / "episodic.db"),
        )


# ---------------------------------------------------------------------------
# Task registry — append here to add new nightly tasks
# ---------------------------------------------------------------------------

def task_sync_sharepoint() -> None:
    """Incremental SharePoint sync via delta API. Falls back to full sync if no baseline."""
    try:
        from agent.ingest.sharepoint_sync import (
            load_sharepoint_config,
            delta_sync,
            record_sp_activity,
            summarize_sp_activity,
        )
        from agent.ingest.sharepoint_client import authenticate
    except ImportError as exc:
        logger.warning("SharePoint sync skipped — missing dependency: %s", exc)
        return

    sp_config_path = os.environ.get(
        "SHAREPOINT_CONFIG", "/opt/qnoe-agent/config/sharepoint.yaml"
    )
    if not Path(sp_config_path).exists():
        logger.info("SharePoint sync skipped — %s not found", sp_config_path)
        return

    # Load credentials from secrets file if not already in environment
    sp_env_file = Path("/opt/qnoe-agent/secrets/sharepoint.env")
    if sp_env_file.exists():
        for line in sp_env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    cfg = load_sharepoint_config(sp_config_path)
    token = authenticate(cfg["auth"])

    all_stats: dict = {}
    for site in cfg.get("sites", []):
        logger.info("SP delta sync (nightly): %s", site["name"])
        site_stats = delta_sync(site, cfg, token)
        record_sp_activity("nightly", site["name"], site_stats)
        all_stats[site["name"]] = site_stats
        logger.info("SP nightly done for %s: %s", site["name"], site_stats)

    # The 30-min watcher poller consumes the delta token as it runs, so the
    # nightly delta above almost always shows ~0. Surface what the poller
    # actually ingested over the last 24h so its work — and any silently
    # skipped/failed files — appears in the report.
    all_stats["poller_activity_24h"] = summarize_sp_activity(24)

    # Safety net for the find_file tool: delta_sync writes web_url only for
    # changed files, so backfill any manifest rows still missing it (from a
    # crash, re-index, or the pre-web_url era). Cheap — only scans NULL rows.
    try:
        from agent.indexing.backfill_sp_weburl import backfill
        bf_stats = backfill()
        all_stats["web_url_backfill"] = bf_stats
        logger.info("SP web_url backfill (nightly): %s", bf_stats)
    except Exception as exc:
        logger.warning("SP web_url backfill skipped: %s", exc)

    return all_stats


def task_scan_qcodes() -> dict:
    """Scan for QCoDeS databases and update registry + qcodes-runs collection."""
    roots = []
    if REPOS_DIR.exists():
        roots.append(REPOS_DIR)
    # Server mount guard — same as task_orphan_cleanup
    mount_marker = SERVER_ROOT / "Group_Manual.txt"
    if mount_marker.exists():
        roots.append(SERVER_ROOT)
    else:
        logger.warning("Server mount not available — scanning repos only")
    if not roots:
        raise FileNotFoundError("No scan roots available")
    logger.info("Scanning %d roots for QCoDeS databases", len(roots))
    stats = asyncio.run(scan_qcodes(roots))
    logger.info(
        "QCoDeS scan: %d DBs found, %d skipped, %d new runs, %d cards upserted",
        stats["dbs_found"], stats["dbs_skipped"],
        stats["new_runs"], stats["cards_upserted"],
    )
    return stats


def task_process_change_queue() -> None:
    """Process stable entries from the watcher's change_queue.

    Handles doc files via ingest_directory (single-file mode) and .db files
    via scan_specific_dbs. Marks entries as processed when done.
    """
    import sqlite3 as _sqlite3
    from agent.watcher.file_cache import get_pending_queue, mark_processed, init_schema

    watcher_db = os.environ.get("WATCHER_DB", "/opt/qnoe-agent/memory/watcher.db")
    if not Path(watcher_db).exists():
        logger.info("Watcher DB not found (%s) — skipping", watcher_db)
        return

    conn = _sqlite3.connect(watcher_db)
    conn.execute("PRAGMA journal_mode=WAL")
    init_schema(conn)

    pending = get_pending_queue(conn, only_stable=True)
    if not pending:
        logger.info("Change queue: no stable entries to process")
        conn.close()
        return

    logger.info("Change queue: %d stable entries to process", len(pending))

    doc_exts = {".pdf", ".docx", ".pptx", ".md", ".txt", ".rst", ".py", ".ipynb"}
    db_exts = {".db"}

    # Split into docs vs databases
    doc_entries = [e for e in pending if e["ext"] in doc_exts and e["change_type"] != "deleted"]
    db_entries = [e for e in pending if e["ext"] in db_exts and e["change_type"] != "deleted"]
    deleted_entries = [e for e in pending if e["change_type"] == "deleted"]

    # Process doc files
    processed_ids: list[int] = []
    done_docs = 0
    if doc_entries:
        # Skip files that aren't readable (CIFS ACL — some lab files are
        # restricted on the Windows share). One unreadable file must NOT abort
        # the whole change-queue task; it stays pending and is re-checked next
        # run. NB: on CIFS neither Path.exists() nor os.access() is reliable —
        # Path.exists() RAISES PermissionError when the parent dir denies
        # traversal (py3.12 propagates EACCES from stat), and os.access() reads
        # mode bits the SMB server may not honour on open. The only reliable
        # test is to actually open the file.
        # CHUNKED + DURABLE + HASH-DEDUPED (2026-07-21, after the first
        # groundhog-day night): the old shape did ONE ingest_directory call
        # with force=True over everything and marked processed only at the
        # END — so when the 1h task alarm fired mid-ingest, a full hour of
        # re-embedding was done and ZERO entries were dequeued; the next night
        # redid the same files forever. Made worse by a watcher restart
        # enqueuing ~110K spurious change events (files unchanged on disk).
        # Now: chunks of CHANGE_QUEUE_CHUNK entries, mark_processed after each
        # chunk (durable progress), list_force=False so unchanged files are
        # skipped by sha256 instead of re-embedded (the hash is the arbiter of
        # "really changed", not the watcher event), and a soft time budget
        # that exits cleanly before the task alarm.
        chunk_n = int(os.environ.get("CHANGE_QUEUE_CHUNK", "300"))
        budget_s = int(os.environ.get("CHANGE_QUEUE_BUDGET_S", "2700"))
        t0 = time.time()
        for ci in range(0, len(doc_entries), chunk_n):
            if time.time() - t0 > budget_s:
                logger.warning(
                    "Change queue: time budget (%ds) reached after %d/%d doc "
                    "entries — the rest stay queued for the next run",
                    budget_s, done_docs, len(doc_entries))
                break
            chunk = doc_entries[ci:ci + chunk_n]
            doc_paths = []
            unreadable = []
            for e in chunk:
                fp = e["file_path"]
                try:
                    with open(fp, "rb") as fh:
                        fh.read(1)
                except FileNotFoundError:
                    continue                 # missing/deleted — skip silently
                except OSError:
                    unreadable.append(fp)    # permission denied, stale handle, etc.
                    continue
                doc_paths.append(Path(fp))
            if unreadable:
                logger.warning(
                    "Change queue: skipping %d unreadable file(s) (permission denied): %s",
                    len(unreadable), unreadable[:3],
                )
            if doc_paths:
                ingest_directory(
                    team="group-wide",
                    repo_path=Path("/"),
                    repo_name="server-watcher",
                    force=False,
                    dry_run=False,
                    file_list=doc_paths,
                    list_force=False,
                    manifest_db=str(SERVER_DATA_DIR / "episodic.db"),
                )
            chunk_ids = [e["id"] for e in chunk]
            mark_processed(conn, chunk_ids)
            done_docs += len(chunk)
            if (ci // chunk_n) % 10 == 0:
                logger.info("Change queue: %d/%d doc entries done", done_docs, len(doc_entries))

    # Process .db files (same CIFS-safe readability test as docs above)
    if db_entries:
        db_paths = []
        db_unreadable = []
        for e in db_entries:
            fp = e["file_path"]
            try:
                with open(fp, "rb") as fh:
                    fh.read(1)
            except FileNotFoundError:
                continue
            except OSError:
                db_unreadable.append(fp)
                continue
            db_paths.append(Path(fp))
        if db_unreadable:
            logger.warning(
                "Change queue: skipping %d unreadable .db file(s) (permission denied): %s",
                len(db_unreadable), db_unreadable[:3],
            )
        if db_paths:
            logger.info("Scanning %d QCoDeS databases from change queue", len(db_paths))
            asyncio.run(scan_specific_dbs(db_paths))
        processed_ids.extend(e["id"] for e in db_entries)

    # Mark deleted entries as processed (orphan_cleanup handles Qdrant removal)
    processed_ids.extend(e["id"] for e in deleted_entries)

    mark_processed(conn, processed_ids)
    conn.close()
    logger.info("Change queue: processed %d entries (%d docs chunked-durable)",
                len(processed_ids) + done_docs, done_docs)
    return {
        "total": len(processed_ids) + done_docs,
        "docs": done_docs,
        "docs_pending": max(len(doc_entries) - done_docs, 0),
        "dbs": len(db_entries),
        "deleted": len(deleted_entries),
    }


def task_orphan_cleanup() -> dict:
    """Remove Qdrant chunks for files missing from disk for 7+ days."""
    from agent.ingest.run_ingest import sweep_orphans

    # Repo manifest (local disk — always available)
    repo_db = str(AGENT_DATA_DIR / "episodic.db")
    repo_stats = sweep_orphans(repo_db, QDRANT_URL)
    logger.info("Repo orphan sweep: %s", repo_stats)

    result = {"repo": repo_stats}
    # Server manifest — only if mount is live
    mount_marker = SERVER_ROOT / "Group_Manual.txt"
    if mount_marker.exists():
        server_db = str(SERVER_DATA_DIR / "episodic.db")
        server_stats = sweep_orphans(server_db, QDRANT_URL)
        logger.info("Server orphan sweep: %s", server_stats)
        result["server"] = server_stats
    else:
        logger.warning("Server mount not available — skipping server orphan sweep")
    return result


def task_context_blocks() -> dict:
    """Surface threat-scanner context drops (mistakes M53 lineage).

    Read-only consumer of the three files the hourly qnoe-context-tally job
    (scripts/context_block_tally.py, runs as qnoe-ai) writes into LOGS_DIR:
    context_blocks.jsonl (block events parsed from the profile agent.logs),
    soul_health.json (fresh static scan) and context_block_tally.state.json.
    Raises only when BOTH data sources are missing — that means the monitor
    itself is down, which must show as a FAILED task, never as "clean".
    """
    events_path = LOGS_DIR / "context_blocks.jsonl"
    health_path = LOGS_DIR / "soul_health.json"
    state_path = LOGS_DIR / "context_block_tally.state.json"
    window_hours = 24
    stale_after_hours = 3
    now = datetime.now()

    stats: dict = {"window_hours": window_hours, "events": 0, "anomalies": 0,
                   "by_target": {}, "kinds": {}, "static_scan": {},
                   "tally_last_run": None, "tally_stale": True}

    # Tally freshness — a dead timer must never read as "no blocks".
    try:
        state = json.loads(state_path.read_text())
        last_run = state.get("last_run")
        stats["tally_last_run"] = last_run
        if last_run:
            age_h = (now - datetime.fromisoformat(last_run)).total_seconds() / 3600
            stats["tally_stale"] = age_h > stale_after_hours
    except (OSError, ValueError):
        pass
    if stats["tally_stale"]:
        logger.warning("context-block tally is stale or missing (last_run=%s) — "
                       "check qnoe-context-tally.timer", stats["tally_last_run"])

    # Block events, last 24h (ISO strings compare lexicographically).
    events_ok = False
    cutoff = (now - timedelta(hours=window_hours)).isoformat(timespec="seconds")
    try:
        with open(events_path, encoding="utf-8") as fh:
            events_ok = True
            for line in fh:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue  # tolerate a partial trailing line
                if (ev.get("ts") or ev.get("ingested_at") or "") < cutoff:
                    continue
                kind = ev.get("kind", "unknown")
                stats["kinds"][kind] = stats["kinds"].get(kind, 0) + 1
                if kind == "anomaly":
                    stats["anomalies"] += 1
                    continue
                stats["events"] += 1
                target = f"{ev.get('profile', '?')}/{ev.get('file', '?')}"
                per_pattern = stats["by_target"].setdefault(target, {})
                for p in ev.get("patterns") or ["?"]:
                    per_pattern[p] = per_pattern.get(p, 0) + 1
    except OSError as e:
        logger.warning("context_blocks.jsonl unreadable: %s", e)

    # Static scan state (written fresh each tally run; startup also writes it).
    health_ok = False
    try:
        health = json.loads(health_path.read_text())
        health_ok = True
        age_h = None
        gen = health.get("generated_at")
        try:
            age_h = round((now - (datetime.fromisoformat(gen) if gen else
                                  datetime.fromtimestamp(health_path.stat().st_mtime))
                           ).total_seconds() / 3600, 1)
        except (OSError, ValueError):
            pass
        stats["static_scan"] = {"summary": health.get("summary", "?"),
                                "blocked": len(health.get("blocked", [])),
                                "scanned": health.get("scanned", 0),
                                "age_hours": age_h}
    except (OSError, ValueError) as e:
        stats["static_scan"] = {"error": str(e)}
        logger.warning("soul_health.json unreadable: %s", e)

    if not events_ok and not health_ok:
        raise RuntimeError(
            "context-block monitor down: neither context_blocks.jsonl nor "
            "soul_health.json is readable — check qnoe-context-tally.timer")
    if stats["anomalies"]:
        logger.warning("%d unparsed block-like log line(s) — Hermes may have "
                       "changed the warning format; inspect kind=anomaly events "
                       "in context_blocks.jsonl", stats["anomalies"])
    return stats


def task_sp_coverage() -> dict:
    """SharePoint present-vs-indexed coverage audit (standing check, M58 lineage).

    Runs scripts/sharepoint_coverage_audit.py in a subprocess: a read-only
    Graph listing (~45 min for noe-group) that reconciles what's live on
    SharePoint against sp_manifest, per top-level folder. Surfaces coverage
    gaps, credential-denied libraries, unconfigured tenant sites and
    manifest/Qdrant divergence — the silent-gap class that left ~11.7K files
    unindexed until 2026-07-16. Also refreshes logs/sp_live_items.jsonl, the
    live-item inventory consumed by scripts/sp_orphan_sweep.py.

    Audit exit code 1 means "findings" (gaps reported as warnings, task OK);
    a crash or unparseable output raises → task FAILURE, never silent.
    Set SP_AUDIT_EXTRA_ARGS (e.g. "--site twisted-materials") to narrow a run.
    """
    import subprocess

    sp_env_file = Path("/opt/qnoe-agent/secrets/sharepoint.env")
    if sp_env_file.exists():
        for line in sp_env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    cmd = [sys.executable, "/opt/qnoe-agent/scripts/sharepoint_coverage_audit.py",
           "--json", "--dump-live", str(LOGS_DIR / "sp_live_items.jsonl")]
    cmd += os.environ.get("SP_AUDIT_EXTRA_ARGS", "").split()
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=85 * 60,
        env={**os.environ, "PYTHONPATH": "/opt/qnoe-agent"},
    )
    if not proc.stdout.strip():
        raise RuntimeError(
            f"coverage audit produced no output (rc={proc.returncode}): "
            f"{proc.stderr[-500:]}")
    res = json.loads(proc.stdout)

    summary = res.get("summary", "?")
    gapped = [f for s in res.get("sites", []) for f in s.get("folders", [])
              if f.get("gap")]
    if gapped or res.get("denied"):
        logger.warning("SP coverage: %s", summary)
    else:
        logger.info("SP coverage: %s", summary)
    return {
        "summary": summary,
        "gapped_folders": len(gapped),
        "denied": len(res.get("denied", [])),
        "tenant_unconfigured": len((res.get("tenant") or {}).get("unconfigured_sites", [])),
        "sites": {s["site"]: {"present": s.get("present_total"),
                              "indexed": s.get("indexed_total"),
                              "orphans": s.get("orphans")}
                  for s in res.get("sites", [])},
    }


def task_server_sweep() -> dict:
    """Nightly new-file sweep of the whole server via the broad /mnt/noe mount
    (M58 standing fix, gap class 1: ACL-denied folders).

    The watcher (server_root /ICFO, yzamir SMB cred) catches changes in
    yzamir-visible folders near-real-time — but 645+ ACL-denied folders are
    invisible to it, which is how ~2/3 of the corpus went stale for months
    (M58). This task re-runs the un-timed find via /mnt/noe (sberlanga cred,
    broad read), refreshes the find-cache that task_server_coverage reads,
    and ingests ONLY files with no manifest row (--new-only: one sqlite scan;
    no CIFS content reads for the ~48K already-indexed files — the default
    full-hash incremental would re-read the entire corpus nightly).
    Stored paths are canonical /ICFO (INGEST_READ_ROOT -> INGEST_STORE_ROOT).

    Deliberate scope limits:
      * Notebook + Personal excluded — Notebook stays /ICFO-scoped (private
        per-person notebooks, privacy decision 2026-07-16); Personal is not
        ingested at all.
      * MODIFICATIONS in ACL-denied folders are not caught (append-mostly
        corpus; the watcher covers visible folders). task_server_coverage
        flags any systematic drift this leaves.
    """
    import subprocess

    mount_marker = Path(os.environ.get("MNT_NOE_ROOT", "/mnt/noe")) / "Group_Manual.txt"
    if not mount_marker.exists():
        raise FileNotFoundError(f"/mnt/noe not mounted (marker missing: {mount_marker})")

    env = {
        **os.environ,
        "PYTHONPATH": "/opt/qnoe-agent",
        "SERVER_ROOT": os.environ.get("MNT_NOE_ROOT", "/mnt/noe"),
        "INGEST_READ_ROOT": os.environ.get("MNT_NOE_ROOT", "/mnt/noe"),
        "INGEST_STORE_ROOT": "/ICFO/groups/NOE",
        "AGENT_DATA_DIR": str(SERVER_DATA_DIR),
        "EXCLUDE_FOLDERS": os.environ.get("SWEEP_EXCLUDE_FOLDERS", "Notebook,Personal"),
        # Mirror the sprint launcher's extension exclusion (user decision
        # 2026-07-16: raw-measurement .txt not ingested). Its absence on night
        # #1 made the fresh find enumerate ~30K .txt files: present-counts
        # inflated 4-10x (7 false <80% flags) and --new-only queued them all
        # (the 170-min sweep timeout).
        "EXCLUDE_EXTENSIONS": os.environ.get("SWEEP_EXCLUDE_EXTENSIONS", ".txt"),
        "INGEST_SKIP_IF_INDEXED": "1",   # belt on top of --new-only (resume safety)
        "OMP_NUM_THREADS": "2",          # M58 lesson: thread oversubscription, not
                                         # worker count, was the sprint bottleneck
    }
    cmd = [sys.executable, "-m", "agent.ingest.parallel_server_ingest",
           "--refresh-find", "--new-only",
           "--workers", os.environ.get("SWEEP_WORKERS", "2"),
           "--min-free-gb", os.environ.get("SWEEP_MIN_FREE_GB", "20")]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=170 * 60, env=env)
    out = (proc.stdout or "") + (proc.stderr or "")
    tail = "\n".join(out.strip().splitlines()[-15:])
    if proc.returncode != 0:
        raise RuntimeError(f"server sweep rc={proc.returncode}:\n{tail}")

    stats = {"rc": proc.returncode}
    m = re.search(r"--new-only: (\d+) of (\d+) files have no manifest row", out)
    if m:
        stats["new_files"] = int(m.group(1))
        stats["present_files"] = int(m.group(2))
    m = re.search(r"progress: (\d+)/(\d+) batches \((\d+) failed\)(?!.*progress:)",
                  out, re.DOTALL)
    if m:
        stats["batches"] = f"{m.group(1)}/{m.group(2)}"
        stats["batches_failed"] = int(m.group(3))
        if int(m.group(3)):
            logger.warning("server sweep: %s failed batch(es) — resumable, "
                           "will retry next night", m.group(3))
    logger.info("Server sweep: %s", stats)
    return stats


def task_server_coverage() -> dict:
    """Server present-vs-indexed coverage audit (standing check, M58 lineage).

    Runs scripts/coverage_audit.py against the find-cache task_server_sweep
    just refreshed: PRESENT per top-level folder (via /mnt/noe) vs INDEXED
    manifest rows (canonical /ICFO paths); warns on any folder below
    MIN_COVERAGE (default 80%) — the reconciliation whose absence let M58
    hide for months. Runs even if the sweep failed (stale cache is still a
    meaningful check; the sweep's own failure is surfaced separately).
    """
    import subprocess

    cmd = [sys.executable, "/opt/qnoe-agent/scripts/coverage_audit.py", "--json"]
    cmd += os.environ.get("SERVER_COVERAGE_ARGS", "").split()
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=14 * 60,
        env={**os.environ, "PYTHONPATH": "/opt/qnoe-agent",
             "SERVER_ROOT": os.environ.get("MNT_NOE_ROOT", "/mnt/noe"),
             "AGENT_DATA_DIR": str(SERVER_DATA_DIR)},
    )
    if not proc.stdout.strip():
        raise RuntimeError(
            f"coverage audit produced no output (rc={proc.returncode}): "
            f"{proc.stderr[-500:]}")
    res = json.loads(proc.stdout)
    summary = res.get("summary", "?")
    if res.get("gapped"):
        logger.warning("Server coverage: %s", summary)
    else:
        logger.info("Server coverage: %s", summary)
    return {
        "summary": summary,
        "gapped_folders": len(res.get("gapped", [])),
        "total": f"{res.get('total_indexed')}/{res.get('total_present')}",
        "present_source": res.get("present_source", "?"),
    }


TASKS: list = [
    task_qdrant_snapshot,
    task_index_repos,
    task_sync_sharepoint,
    task_process_change_queue,
    task_orphan_cleanup,
    task_context_blocks,
    task_sp_coverage,
    task_server_sweep,
    task_server_coverage,
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _resolve_collection(repo_name: str, config: dict) -> str:
    repo_lower = repo_name.lower()
    for collection, patterns in config.get("collections", {}).items():
        for pattern in patterns:
            if pattern.lower() in repo_lower:
                return collection
    return config.get("default", "group-wide")


def run(dry_run: bool = False, only_task: str | None = None) -> int:
    """Run registered tasks in order. Returns number of failures."""
    tasks = TASKS
    if only_task:
        needle = only_task if only_task.startswith("task_") else f"task_{only_task}"
        tasks = [t for t in TASKS if t.__name__ == needle]
        if not tasks:
            logger.error(
                "Unknown task: %s. Available: %s",
                only_task,
                [t.__name__ for t in TASKS],
            )
            return 1

    run_start = datetime.now(timezone.utc)
    logger.info("=" * 60)
    logger.info("Nightly run start — %s UTC", run_start.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("Tasks queued: %s", [t.__name__ for t in tasks])

    if dry_run:
        logger.info("[dry-run] No tasks executed.")
        return 0

    in_main = threading.current_thread() is threading.main_thread()
    if in_main:
        old_handler = signal.signal(signal.SIGALRM, _alarm_handler)

    root_logger = logging.getLogger()
    failures = 0
    task_results: list[dict] = []

    for task in tasks:
        logger.info("-" * 40)
        logger.info("START  %s", task.__name__)
        t0 = time.monotonic()
        limit = TASK_TIMEOUTS.get(task.__name__, _DEFAULT_TASK_TIMEOUT)

        # Attach capturing handler to root logger for this task
        capture = _CapturingHandler()
        root_logger.addHandler(capture)
        if in_main:
            signal.alarm(limit)

        status = "ok"
        task_stats: dict | None = None
        error_msg: str | None = None
        try:
            task_stats = task()
            logger.info("OK     %s  (%.1fs)", task.__name__, time.monotonic() - t0)
        except TimeoutError:
            elapsed = time.monotonic() - t0
            error_msg = f"exceeded {limit}s wall-clock limit"
            logger.error("TIMEOUT %s  (%.1fs): %s", task.__name__, elapsed, error_msg)
            status = "timeout"
            failures += 1
        except Exception as exc:
            import traceback as _tb
            error_msg = f"{exc}\n{_tb.format_exc()}"
            logger.error(
                "FAIL   %s  (%.1fs): %s",
                task.__name__, time.monotonic() - t0, exc,
                exc_info=True,
            )
            status = "fail"
            failures += 1
        finally:
            if in_main:
                signal.alarm(0)
            root_logger.removeHandler(capture)

        duration = time.monotonic() - t0

        # Augment stats from captured log records for ingest tasks
        warnings_errors = [
            r for r in capture.records if r.levelno >= logging.WARNING
        ]
        ingest_stats = _parse_ingest_stats(capture.records)
        if ingest_stats["files_indexed"] > 0 or ingest_stats["files_failed"] > 0:
            task_stats = {**(task_stats or {}), **ingest_stats}

        task_results.append({
            "name": task.__name__,
            "status": status,
            "duration_s": round(duration, 1),
            "stats": task_stats,
            "error": error_msg,
            "warnings": [r.getMessage() for r in warnings_errors if r.levelno == logging.WARNING],
            "errors": [r.getMessage() for r in warnings_errors if r.levelno >= logging.ERROR],
            "failed_files": ingest_stats.get("failed_files", []),
        })

    if in_main:
        signal.signal(signal.SIGALRM, old_handler)

    run_end = datetime.now(timezone.utc)
    logger.info("=" * 60)
    logger.info(
        "Nightly run done — %d/%d tasks succeeded.", len(tasks) - failures, len(tasks)
    )

    # Write machine-readable report
    report = {
        "run_date": run_start.strftime("%Y-%m-%d"),
        "run_start": run_start.isoformat(),
        "run_end": run_end.isoformat(),
        "tasks_ok": len(tasks) - failures,
        "tasks_total": len(tasks),
        "tasks": task_results,
    }
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        REPORT_JSON.write_text(json.dumps(report, indent=2))
        _write_txt_report(report)
    except Exception as exc:
        logger.warning("Could not write nightly report: %s", exc)

    return failures


def _fmt_duration(s: float) -> str:
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{int(s)//60}m {int(s)%60}s"
    return f"{s/3600:.1f}h"


def _write_txt_report(report: dict) -> None:
    """Write a human-readable detailed report to a dated file."""
    date_str = report["run_date"]
    txt_path = LOGS_DIR / f"nightly_report_{date_str}.txt"
    lines: list[str] = []

    lines.append("=" * 64)
    lines.append(f"QNOE Agent Nightly Report — {date_str}")
    lines.append(f"Start : {report['run_start']}")
    lines.append(f"End   : {report['run_end']}")
    lines.append(f"Result: {report['tasks_ok']}/{report['tasks_total']} tasks succeeded")
    lines.append("=" * 64)
    lines.append("")

    lines.append("TASK SUMMARY")
    lines.append("-" * 40)
    icon = {"ok": "OK  ", "fail": "FAIL", "timeout": "TIME"}
    for t in report["tasks"]:
        name = t["name"].replace("task_", "")
        dur = _fmt_duration(t["duration_s"])
        stats = t.get("stats") or {}
        stat_str = _summarise_stats(t["name"], stats)
        lines.append(f"  [{icon.get(t['status'], '??? ')}] {name:<28} {dur:>6}  {stat_str}")
    lines.append("")

    # Failed files section
    all_failed: list[tuple[str, list[str]]] = [
        (t["name"], t["failed_files"])
        for t in report["tasks"]
        if t.get("failed_files")
    ]
    if all_failed:
        lines.append("FAILED FILES")
        lines.append("-" * 40)
        for task_name, files in all_failed:
            lines.append(f"  {task_name}:")
            for f in files:
                lines.append(f"    {f}")
        lines.append("")

    # Warnings and errors per task
    has_issues = any(t["warnings"] or t["errors"] for t in report["tasks"])
    if has_issues:
        lines.append("WARNINGS & ERRORS")
        lines.append("-" * 40)
        for t in report["tasks"]:
            msgs = t["warnings"] + t["errors"]
            if msgs:
                lines.append(f"  {t['name']}:")
                for m in msgs:
                    lines.append(f"    {m}")
        lines.append("")

    # Full error tracebacks for failed/timed-out tasks
    failed_tasks = [t for t in report["tasks"] if t["status"] != "ok" and t.get("error")]
    if failed_tasks:
        lines.append("FULL ERROR DETAILS")
        lines.append("-" * 40)
        for t in failed_tasks:
            lines.append(f"  [{t['status'].upper()}] {t['name']}:")
            for line in (t["error"] or "").splitlines():
                lines.append(f"    {line}")
            lines.append("")

    lines.append("=" * 64)
    txt_path.write_text("\n".join(lines))
    logger.info("Nightly report written: %s", txt_path)


def _summarise_stats(task_name: str, stats: dict) -> str:
    if not stats:
        return ""
    if task_name == "task_qdrant_snapshot":
        return f"created {stats.get('snapshots_created', 0)}, pruned {stats.get('snapshots_pruned', 0)}"
    if task_name == "task_index_repos":
        new = stats.get('new_files', 0)
        upd = stats.get('updated_files', 0)
        breakdown = f"{new} new, {upd} updated" if (new or upd) else f"{stats.get('files_indexed', 0)} files"
        return (f"indexed {breakdown} "
                f"(+{stats.get('chunks_added', 0)} chunks), "
                f"{stats.get('files_failed', 0)} failed")
    if task_name == "task_sync_sharepoint":
        parts = []
        for site, s in stats.items():
            if site == "poller_activity_24h":
                continue
            if isinstance(s, dict):
                new = s.get('new', 0)
                upd = s.get('updated', 0)
                detail = f"{new}↑ {upd}✎" if (new or upd) else f"{s.get('processed', 0)}✓"
                parts.append(f"{site}: {detail} del={s.get('deleted', 0)} err={s.get('errors', 0)}")
        summary = "nightly delta: " + (" | ".join(parts) if parts else "none")
        # Surface the 30-min poller's actual 24h ingestion — the real work,
        # invisible to the nightly delta above (token already consumed).
        pa = stats.get("poller_activity_24h") or {}
        by_site = pa.get("by_site") or {}
        hrs = pa.get("window_hours", 24)
        if by_site:
            pseg = []
            for site, a in by_site.items():
                seg = (f"{site}: {a.get('new', 0)}↑ {a.get('updated', 0)}✎ "
                       f"del={a.get('deleted', 0)} skip={a.get('skipped', 0)} "
                       f"err={a.get('errors', 0)}")
                dropped = (a.get('skipped_files') or []) + (a.get('failed_files') or [])
                if dropped:
                    shown = ", ".join(dropped[:5])
                    if len(dropped) > 5:
                        shown += f", +{len(dropped) - 5} more"
                    seg += f" [dropped: {shown}]"
                pseg.append(seg)
            summary += f"\n           poller {hrs}h: " + " | ".join(pseg)
        else:
            summary += f"\n           poller {hrs}h: no activity"
        return summary
    if task_name == "task_scan_qcodes":
        return (f"{stats.get('dbs_found', 0)} DBs, "
                f"+{stats.get('new_runs', 0)} runs")
    if task_name == "task_process_change_queue":
        return f"{stats.get('total', 0)} entries ({stats.get('docs', 0)} docs, {stats.get('dbs', 0)} DBs)"
    if task_name == "task_orphan_cleanup":
        deleted = (stats.get("repo", {}) or {}).get("deleted", 0)
        deleted += (stats.get("server", {}) or {}).get("deleted", 0)
        return f"{deleted} orphans deleted"
    if task_name == "task_context_blocks":
        hrs = stats.get("window_hours", 24)
        targets = stats.get("by_target") or {}
        if targets:
            segs = [f"{t} ({', '.join(f'{p} ×{c}' for p, c in pats.items())})"
                    for t, pats in list(targets.items())[:5]]
            if len(targets) > 5:
                segs.append(f"+{len(targets) - 5} more")
            blocks = f"blocks {hrs}h: {stats.get('events', 0)} — " + "; ".join(segs)
        else:
            blocks = f"blocks {hrs}h: none"
        ss = stats.get("static_scan") or {}
        if "error" in ss:
            static = f"static scan UNREADABLE ({ss['error'][:60]})"
        else:
            state = f"⚠️ {ss.get('blocked', '?')} BLOCKED" if ss.get("blocked") else "CLEAN"
            static = f"static: {state} ({ss.get('scanned', '?')} files, {ss.get('age_hours', '?')}h old)"
        out = f"{blocks} | {static}"
        if stats.get("tally_stale"):
            out += " | ⚠️ TALLY STALE — check qnoe-context-tally.timer"
        if stats.get("anomalies"):
            out += f" | ⚠️ {stats['anomalies']} unparsed block-line(s)"
        return out
    return str(stats)[:80]


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="QNOE nightly maintenance runner")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without executing")
    parser.add_argument("--task", default=None, metavar="NAME", help="Run only this task")
    args = parser.parse_args()

    sys.exit(min(run(dry_run=args.dry_run, only_task=args.task), 1))


if __name__ == "__main__":
    main()
