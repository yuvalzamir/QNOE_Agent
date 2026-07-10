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
        all_stats[site["name"]] = site_stats
        logger.info("SP nightly done for %s: %s", site["name"], site_stats)

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
    if doc_entries:
        doc_paths = [Path(e["file_path"]) for e in doc_entries if Path(e["file_path"]).exists()]
        if doc_paths:
            logger.info("Ingesting %d doc files from change queue", len(doc_paths))
            ingest_directory(
                team="group-wide",
                repo_path=Path("/"),
                repo_name="server-watcher",
                force=True,
                dry_run=False,
                file_list=doc_paths,
                manifest_db=str(SERVER_DATA_DIR / "episodic.db"),
            )
        processed_ids.extend(e["id"] for e in doc_entries)

    # Process .db files
    if db_entries:
        db_paths = [Path(e["file_path"]) for e in db_entries if Path(e["file_path"]).exists()]
        if db_paths:
            logger.info("Scanning %d QCoDeS databases from change queue", len(db_paths))
            asyncio.run(scan_specific_dbs(db_paths))
        processed_ids.extend(e["id"] for e in db_entries)

    # Mark deleted entries as processed (orphan_cleanup handles Qdrant removal)
    processed_ids.extend(e["id"] for e in deleted_entries)

    mark_processed(conn, processed_ids)
    conn.close()
    logger.info("Change queue: processed %d entries", len(processed_ids))
    return {
        "total": len(processed_ids),
        "docs": len(doc_entries),
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


TASKS: list = [
    task_qdrant_snapshot,
    task_index_repos,
    task_sync_sharepoint,
    task_process_change_queue,
    task_orphan_cleanup,
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
            if isinstance(s, dict):
                new = s.get('new', 0)
                upd = s.get('updated', 0)
                detail = f"{new}↑ {upd}✎" if (new or upd) else f"{s.get('processed', 0)}✓"
                parts.append(f"{site}: {detail} del={s.get('deleted', 0)} err={s.get('errors', 0)}")
        return " | ".join(parts)
    if task_name == "task_scan_qcodes":
        return (f"{stats.get('dbs_found', 0)} DBs, "
                f"+{stats.get('new_runs', 0)} runs")
    if task_name == "task_process_change_queue":
        return f"{stats.get('total', 0)} entries ({stats.get('docs', 0)} docs, {stats.get('dbs', 0)} DBs)"
    if task_name == "task_orphan_cleanup":
        deleted = (stats.get("repo", {}) or {}).get("deleted", 0)
        deleted += (stats.get("server", {}) or {}).get("deleted", 0)
        return f"{deleted} orphans deleted"
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
