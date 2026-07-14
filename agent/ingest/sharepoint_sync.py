"""SharePoint document library sync — full and incremental (delta).

No files are persisted locally. Each item is streamed to a temp path,
chunked, embedded, upserted into Qdrant, and the temp file is deleted
immediately — even on failure (via try/finally).

Usage:
  python -m agent.ingest.sharepoint_sync                    # delta sync all sites
  python -m agent.ingest.sharepoint_sync --full             # full sync all sites
  python -m agent.ingest.sharepoint_sync --full --site qnoe-main
  python -m agent.ingest.sharepoint_sync --validate         # auth + list drives only
"""
import argparse
import json
import logging
import multiprocessing
import os
import sqlite3
import threading
import time
from concurrent.futures import (
    ProcessPoolExecutor as _PPE,
    ThreadPoolExecutor as _TPE,
    TimeoutError as _ChunkTimeout,
    BrokenExecutor as _BrokenExecutor,
    as_completed,
)
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil
import yaml
from qdrant_client import QdrantClient

from .sharepoint_client import (
    authenticate,
    download_to_temp,
    get_delta,
    get_drive_id,
    get_site_id,
    list_drive_items,
)
from .splitter import chunk_file
from .embed import embed_documents, embed_sparse
from .run_ingest import _ensure_collection, _upsert_chunks, QDRANT_URL

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".py", ".ipynb", ".md", ".rst", ".pdf", ".pptx", ".docx"}

# Skip files inside dependency/cache directories — these are never lab content
EXCLUDE_PATH_SUBSTRINGS = {".env/", "/venv/", "site-packages/", "node_modules/", "__pycache__/"}

# Max seconds to spend chunking a single file (covers Docling PDF/DOCX/PPTX processing).
FILE_CHUNK_TIMEOUT = int(os.environ.get("SP_FILE_CHUNK_TIMEOUT", "300"))

# Parallelism settings
THREAD_WORKERS = int(os.environ.get("SP_THREAD_WORKERS", "20"))  # concurrent download/embed threads

# Memory guard: never submit new work if available RAM drops below this threshold.
# Docling subprocesses fork from the parent and can spike 4-5 GB each on large PDFs.
MIN_FREE_GB = float(os.environ.get("SP_MIN_FREE_GB", "20"))


def _memory_ok() -> bool:
    """Return True if available system RAM is above the safety floor."""
    return psutil.virtual_memory().available / (1024 ** 3) >= MIN_FREE_GB


# Chunking runs in a forkserver-based subprocess, never a plain fork of the main
# process. By mid-batch the main process has loaded torch/onnxruntime (threads +
# CUDA) via embed_documents; forking THAT state segfaults the worker mid-parse
# (_BrokenExecutor), silently dropping the file (M47). forkserver forks each
# worker from a clean, single-threaded server process instead — fork-speed
# (unlike spawn, which re-imports the whole module tree per file and would
# cripple full_sync's 76K files) but without inheriting the unsafe torch state.
# The server is pre-started from a clean pre-torch state via _ensure_chunk_server.
_CHUNK_CTX = multiprocessing.get_context("forkserver")
_CHUNK_CTX.set_forkserver_preload(["agent.ingest.splitter"])
_chunk_server_started = False
_chunk_server_lock = threading.Lock()


def _noop_task() -> bool:
    return True


def _ensure_chunk_server() -> None:
    """Start the forkserver server once, from a clean (pre-torch) state.

    Called at the top of a sync run — before any embedding loads torch into the
    main process — so the server (and every worker forked from it) never inherits
    fork-unsafe torch/CUDA thread state. Idempotent and thread-safe.
    """
    global _chunk_server_started
    if _chunk_server_started:
        return
    with _chunk_server_lock:
        if _chunk_server_started:
            return
        try:
            ex = _PPE(max_workers=1, mp_context=_CHUNK_CTX)
            ex.submit(_noop_task).result(timeout=120)
            ex.shutdown(wait=False)
            _chunk_server_started = True
            logger.info("SP chunk forkserver started (clean, pre-torch)")
        except Exception as exc:
            logger.warning("Could not pre-start chunk forkserver: %s", exc)

# Listing cache: saves the full item list to disk so restarts skip the listing phase
LISTING_CACHE_DIR = Path(os.environ.get("SP_LISTING_CACHE_DIR", "/tmp/qnoe-sp-listing-cache/"))
LISTING_CACHE_MAX_AGE_H = int(os.environ.get("SP_LISTING_CACHE_MAX_AGE_H", "999999"))  # never expires by default

def _chunk_file_safe(dest: Path, site_name: str) -> list:
    """Run chunk_file in a fresh isolated subprocess.

    One crash/timeout only affects this one file — no cascade to other threads.
    Explicitly kills the worker process on timeout so shutdown(wait=True) never hangs.
    Uses a forkserver context so the worker never inherits the parent's
    torch/onnxruntime state (which crashes forked workers on the 2nd+ file — M47).
    """
    ex = _PPE(max_workers=1, mp_context=_CHUNK_CTX)
    try:
        fut = ex.submit(chunk_file, dest, site_name)
        try:
            return fut.result(timeout=FILE_CHUNK_TIMEOUT)
        except (_ChunkTimeout, Exception):
            # Kill worker processes immediately — avoids shutdown(wait=True) blocking forever
            for proc in getattr(ex, "_processes", {}).values():
                try:
                    proc.kill()
                except Exception:
                    pass
            raise
    finally:
        ex.shutdown(wait=False)

class _SharedToken:
    """Thread-safe token holder that auto-refreshes before expiry."""

    def __init__(self, token: str, auth_cfg: dict) -> None:
        self._token = token
        self._auth_cfg = auth_cfg
        self._ts = time.monotonic()
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            if time.monotonic() - self._ts >= TOKEN_REFRESH_SECONDS:
                try:
                    self._token = authenticate(self._auth_cfg)
                    self._ts = time.monotonic()
                    logger.info("SP token refreshed (worker thread)")
                except Exception as exc:
                    logger.warning("SP token refresh failed, using old token: %s", exc)
            return self._token


SP_CONFIG_PATH = os.environ.get(
    "SHAREPOINT_CONFIG", "/opt/qnoe-agent/config/sharepoint.yaml"
)
SP_MANIFEST_DB = os.environ.get(
    "SP_MANIFEST_DB", "/opt/qnoe-agent/memory/sharepoint.db"
)
WATCHER_DB = os.environ.get("WATCHER_DB", "/opt/qnoe-agent/memory/watcher.db")


# ---------------------------------------------------------------------------
# Listing cache (avoids re-listing 900K items on restart)
# ---------------------------------------------------------------------------

def _listing_cache_path(drive_id: str) -> Path:
    safe = drive_id.replace("!", "_").replace("/", "_")[:50]
    return LISTING_CACHE_DIR / f"{safe}.jsonl"


def _check_listing_cache(drive_id: str) -> "tuple[str, float] | None":
    """Return (delta_link, saved_at) if a valid cache exists, else None.

    Does NOT load items — they are streamed on demand via _stream_listing_cache.
    """
    p = _listing_cache_path(drive_id)
    if not p.exists():
        return None
    try:
        with open(p) as f:
            meta = json.loads(f.readline())
        age_h = (time.time() - meta["saved_at"]) / 3600
        if age_h > LISTING_CACHE_MAX_AGE_H:
            logger.info("Listing cache expired (%.1fh old), re-listing", age_h)
            return None
        logger.info("Listing cache found (%.1fh old) — streaming items", age_h)
        return meta["delta_link"], meta["saved_at"]
    except Exception as exc:
        logger.warning("Could not read listing cache metadata: %s", exc)
        return None


def _save_listing_cache(drive_id: str, items: list, delta_link: str) -> None:
    """Write JSONL cache: metadata on line 1, one item per subsequent line."""
    try:
        LISTING_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _listing_cache_path(drive_id)
        with open(p, "w") as f:
            f.write(json.dumps({"_meta": True, "delta_link": delta_link, "saved_at": time.time()}) + "\n")
            for item in items:
                f.write(json.dumps(item) + "\n")
        logger.info("Listing cache saved: %d items", len(items))
    except Exception as exc:
        logger.warning("Could not save listing cache: %s", exc)


def _stream_listing_cache(drive_id: str, skip_files: int = 0):
    """Generator: stream file items from JSONL cache one at a time.

    Applies file/deleted filter and skip_files offset without loading all items.
    """
    p = _listing_cache_path(drive_id)
    skipped = 0
    with open(p) as f:
        f.readline()  # skip metadata line
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "file" not in item or "deleted" in item:
                continue
            if skipped < skip_files:
                skipped += 1
                continue
            yield item


def _clear_listing_cache(drive_id: str) -> None:
    _listing_cache_path(drive_id).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_sharepoint_config(path: str | None = None) -> dict:
    with open(path or SP_CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# SharePoint manifest (etag-based deduplication, separate from repo manifest)
# ---------------------------------------------------------------------------

def _get_sp_manifest_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(SP_MANIFEST_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sp_manifest (
            id           INTEGER PRIMARY KEY,
            item_id      TEXT NOT NULL UNIQUE,
            item_path    TEXT NOT NULL,
            site_name    TEXT NOT NULL,
            drive_id     TEXT NOT NULL,
            etag         TEXT NOT NULL,
            collection   TEXT NOT NULL,
            point_ids    TEXT NOT NULL,
            web_url      TEXT,
            indexed_at   TEXT NOT NULL
        )
    """)
    # Migration: add web_url to manifests created before the find_file tool.
    # (CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so the
    # column must be added explicitly. Backfill existing rows separately via
    # agent.indexing.backfill_sp_weburl.)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sp_manifest)")}
    if "web_url" not in cols:
        conn.execute("ALTER TABLE sp_manifest ADD COLUMN web_url TEXT")
    # Activity log: one row per sync run (poller or nightly), so the nightly
    # report can surface work done by the always-on SharePoint poller, which
    # otherwise ingests directly and leaves no trace the report can read.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sp_activity (
            id            INTEGER PRIMARY KEY,
            ts            TEXT NOT NULL,
            source        TEXT NOT NULL,
            site          TEXT NOT NULL,
            processed     INTEGER DEFAULT 0,
            new           INTEGER DEFAULT 0,
            updated       INTEGER DEFAULT 0,
            skipped       INTEGER DEFAULT 0,
            deleted       INTEGER DEFAULT 0,
            errors        INTEGER DEFAULT 0,
            skipped_files TEXT,
            failed_files  TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sp_activity_ts ON sp_activity(ts)")
    # Retry queue: items whose processing failed during a delta pass. The delta
    # token advances regardless (so we don't reprocess the whole change set every
    # cycle), which means a one-time failure would otherwise be lost forever.
    # Failed items are parked here and re-attempted on later syncs until they
    # succeed or exhaust max attempts. See memory/mistakes.md M47.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sp_retry_queue (
            item_id      TEXT PRIMARY KEY,
            drive_id     TEXT NOT NULL,
            site_name    TEXT NOT NULL,
            name         TEXT,
            attempts     INTEGER NOT NULL DEFAULT 0,
            first_failed TEXT NOT NULL,
            last_attempt TEXT NOT NULL,
            last_error   TEXT
        )
    """)
    conn.commit()
    return conn


# Max times a failed item is re-attempted before it is given up on (and left in
# the queue flagged as exhausted, so the report can surface it).
SP_RETRY_MAX_ATTEMPTS = int(os.environ.get("SP_RETRY_MAX_ATTEMPTS", "5"))


def _enqueue_retry(sp_conn: sqlite3.Connection, item: dict, drive_id: str,
                   site_name: str, error: str) -> None:
    """Record (or bump the attempt count of) a failed item in the retry queue."""
    now = datetime.now(timezone.utc).isoformat()
    item_id = item.get("id")
    if not item_id:
        return
    sp_conn.execute(
        """INSERT INTO sp_retry_queue
               (item_id, drive_id, site_name, name, attempts, first_failed, last_attempt, last_error)
           VALUES (?, ?, ?, ?, 1, ?, ?, ?)
           ON CONFLICT(item_id) DO UPDATE SET
               attempts     = attempts + 1,
               last_attempt = excluded.last_attempt,
               last_error   = excluded.last_error,
               name         = excluded.name""",
        (item_id, drive_id, site_name, item.get("name", ""), now, now, (error or "")[:500]),
    )
    sp_conn.commit()


def _dequeue_retry(sp_conn: sqlite3.Connection, item_id: str) -> None:
    """Remove an item from the retry queue (called after it finally succeeds)."""
    sp_conn.execute("DELETE FROM sp_retry_queue WHERE item_id = ?", (item_id,))
    sp_conn.commit()


def _process_retry_queue(
    site_cfg: dict, cfg: dict, drive_id: str, temp_dir: Path,
    token, client: QdrantClient, sp_conn: sqlite3.Connection, stats: dict,
) -> None:
    """Re-attempt items parked in the retry queue for this drive.

    Re-fetches each item's current metadata from Graph (the parked row only
    stores ids) and runs it back through _process_item. Successes are dequeued;
    still-failing items keep their bumped attempt count. Items past
    SP_RETRY_MAX_ATTEMPTS are skipped (left in the queue, flagged exhausted).
    """
    from .sharepoint_client import GRAPH_BASE, _get as _graph_get
    rows = sp_conn.execute(
        """SELECT item_id, name, attempts FROM sp_retry_queue
           WHERE drive_id = ? AND attempts < ?""",
        (drive_id, SP_RETRY_MAX_ATTEMPTS),
    ).fetchall()
    if not rows:
        return
    logger.info("SP retry: %d parked item(s) for %s/%s",
                len(rows), site_cfg["name"], drive_id)
    for item_id, name, attempts in rows:
        tok = token.get() if isinstance(token, _SharedToken) else token
        try:
            item = _graph_get(f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}", tok)
        except Exception as exc:
            # 404 → the item was deleted upstream; stop retrying it.
            if "404" in str(exc):
                _dequeue_retry(sp_conn, item_id)
                logger.info("SP retry: %s gone upstream (404) — dropped from queue", name)
            else:
                _enqueue_retry(sp_conn, {"id": item_id, "name": name}, drive_id,
                               site_cfg["name"], f"refetch failed: {exc}")
            continue
        try:
            ok = _process_item(item, site_cfg, drive_id, temp_dir, token, client, sp_conn)
        except Exception as exc:
            _enqueue_retry(sp_conn, item, drive_id, site_cfg["name"], str(exc))
            continue
        if ok:
            _dequeue_retry(sp_conn, item_id)
            stats["processed"] += 1
            stats.setdefault("retried_ok", 0)
            stats["retried_ok"] += 1
            logger.info("SP retry: recovered %s (was attempt %d)", name, attempts)
        else:
            _enqueue_retry(sp_conn, item, drive_id, site_cfg["name"], "still failing")


def record_sp_activity(source: str, site: str, stats: dict) -> None:
    """Persist one sync run's stats to the sp_activity log.

    `source` is "poller" (30-min watcher poller) or "nightly". This is what
    makes the poller's continuous ingestion visible to the daily report — the
    poller consumes the Graph delta token as it runs, so by report time its own
    re-run of delta_sync sees nothing. The report reads this log instead.
    """
    conn = _get_sp_manifest_conn()
    try:
        now = datetime.now(timezone.utc)
        conn.execute(
            """INSERT INTO sp_activity
               (ts, source, site, processed, new, updated, skipped, deleted,
                errors, skipped_files, failed_files)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now.isoformat(), source, site,
                int(stats.get("processed", 0)), int(stats.get("new", 0)),
                int(stats.get("updated", 0)), int(stats.get("skipped", 0)),
                int(stats.get("deleted", 0)), int(stats.get("errors", 0)),
                json.dumps(stats.get("skipped_files", [])),
                json.dumps(stats.get("failed_files", [])),
            ),
        )
        # Keep the table small — 30 days of history is plenty for the report.
        cutoff = (now - timedelta(days=30)).isoformat()
        conn.execute("DELETE FROM sp_activity WHERE ts < ?", (cutoff,))
        conn.commit()
    except Exception as exc:
        logger.warning("Could not record SP activity for %s: %s", site, exc)
    finally:
        conn.close()


def summarize_sp_activity(hours: int = 24) -> dict:
    """Aggregate recorded SharePoint sync activity within the last `hours`.

    Returns {"window_hours", "by_site": {site: {processed, new, updated,
    skipped, deleted, errors, sources, skipped_files, failed_files}}}.
    """
    conn = _get_sp_manifest_conn()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = conn.execute(
            """SELECT site, source, processed, new, updated, skipped, deleted,
                      errors, skipped_files, failed_files
               FROM sp_activity WHERE ts >= ?""",
            (cutoff,),
        ).fetchall()
    except Exception as exc:
        logger.warning("Could not summarize SP activity: %s", exc)
        rows = []
    finally:
        conn.close()

    by_site: dict = {}
    for (site, source, processed, new, updated, skipped, deleted, errors,
         skipped_json, failed_json) in rows:
        agg = by_site.setdefault(site, {
            "processed": 0, "new": 0, "updated": 0, "skipped": 0,
            "deleted": 0, "errors": 0, "sources": set(),
            "skipped_files": [], "failed_files": [],
        })
        agg["processed"] += processed or 0
        agg["new"] += new or 0
        agg["updated"] += updated or 0
        agg["skipped"] += skipped or 0
        agg["deleted"] += deleted or 0
        agg["errors"] += errors or 0
        agg["sources"].add(source)
        try:
            agg["skipped_files"].extend(json.loads(skipped_json or "[]"))
            agg["failed_files"].extend(json.loads(failed_json or "[]"))
        except (TypeError, ValueError):
            pass
    # Sets are not JSON-serializable — the report writes this dict to JSON.
    for agg in by_site.values():
        agg["sources"] = sorted(agg["sources"])
    return {"window_hours": hours, "by_site": by_site}


def _is_unchanged(sp_conn: sqlite3.Connection, item_id: str, etag: str) -> bool:
    row = sp_conn.execute(
        "SELECT etag FROM sp_manifest WHERE item_id = ?", (item_id,)
    ).fetchone()
    return row is not None and row[0] == etag


def _record_item(
    sp_conn: sqlite3.Connection,
    item_id: str,
    item_path: str,
    site_name: str,
    drive_id: str,
    etag: str,
    collection: str,
    point_ids: list[str],
    web_url: str = "",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    sp_conn.execute(
        """INSERT OR REPLACE INTO sp_manifest
           (item_id, item_path, site_name, drive_id, etag, collection, point_ids, web_url, indexed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (item_id, item_path, site_name, drive_id, etag, collection,
         json.dumps(point_ids), web_url, now),
    )
    sp_conn.commit()


def _delete_old_chunks(
    client: QdrantClient, sp_conn: sqlite3.Connection, item_id: str
) -> None:
    row = sp_conn.execute(
        "SELECT collection, point_ids FROM sp_manifest WHERE item_id = ?", (item_id,)
    ).fetchone()
    if not row:
        return
    collection, point_ids_json = row
    point_ids = json.loads(point_ids_json) if point_ids_json else []
    if point_ids:
        try:
            client.delete(collection_name=collection, points_selector=point_ids)
        except Exception as exc:
            logger.warning(
                "Could not delete old SP chunks for item %s: %s", item_id, exc
            )


def _delete_item(
    sp_conn: sqlite3.Connection, client: QdrantClient, item_id: str
) -> None:
    """Remove a deleted SharePoint item from Qdrant and manifest."""
    row = sp_conn.execute(
        "SELECT collection, point_ids FROM sp_manifest WHERE item_id = ?", (item_id,)
    ).fetchone()
    if not row:
        return
    collection, point_ids_json = row
    point_ids = json.loads(point_ids_json) if point_ids_json else []
    if point_ids:
        try:
            client.delete(collection_name=collection, points_selector=point_ids)
        except Exception as exc:
            logger.warning(
                "Could not delete SP Qdrant chunks for item %s: %s", item_id, exc
            )
    sp_conn.execute("DELETE FROM sp_manifest WHERE item_id = ?", (item_id,))
    sp_conn.commit()


# ---------------------------------------------------------------------------
# Delta link persistence (stored in watcher DB)
# ---------------------------------------------------------------------------

def _get_watcher_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(WATCHER_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sharepoint_delta (
            drive_id   TEXT PRIMARY KEY,
            delta_link TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _save_delta_link(drive_id: str, delta_link: str) -> None:
    conn = _get_watcher_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO sharepoint_delta (drive_id, delta_link, updated_at)"
        " VALUES (?, ?, ?)",
        (drive_id, delta_link, now),
    )
    conn.commit()
    conn.close()


def _get_stored_delta_link(drive_id: str) -> str | None:
    conn = _get_watcher_conn()
    row = conn.execute(
        "SELECT delta_link FROM sharepoint_delta WHERE drive_id = ?", (drive_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Item path helpers
# ---------------------------------------------------------------------------

def _item_path(item: dict) -> str:
    """Extract relative path from item's parentReference.path + name.

    Graph API path format: /drives/{drive_id}/root:/Folder/Subfolder
    Returns:  Folder/Subfolder/filename.pdf
    """
    parent_path = item.get("parentReference", {}).get("path", "")
    if "root:" in parent_path:
        parent_path = parent_path.split("root:", 1)[1].lstrip("/")
    return f"{parent_path}/{item['name']}".lstrip("/") if parent_path else item["name"]


# ---------------------------------------------------------------------------
# Single-item processing
# ---------------------------------------------------------------------------

def _process_item(
    item: dict,
    site_cfg: dict,
    drive_id: str,
    temp_dir: Path,
    token: "str | _SharedToken",
    client: QdrantClient,
    sp_conn: "sqlite3.Connection | None" = None,
) -> bool:
    """Download → chunk → embed → upsert → delete temp. Returns True if indexed.

    If sp_conn is None, opens its own connection (safe to call from threads).
    token may be a plain str or a _SharedToken (used by parallel full_sync).
    """
    own_conn = sp_conn is None
    if own_conn:
        sp_conn = _get_sp_manifest_conn()
    tok = token.get() if isinstance(token, _SharedToken) else token
    name = item.get("name", "")
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return False

    size = item.get("size", 0)
    max_bytes = site_cfg.get("max_file_mb", 50) * 1024 * 1024
    if size > max_bytes:
        logger.warning(
            "Skipping oversized SP file (%d MB): %s", size // (1024 * 1024), name
        )
        return False

    rel_path = _item_path(item)
    for excl in site_cfg.get("exclude_folders", []):
        if rel_path.startswith(excl.lstrip("/")):
            return False
    if any(p in rel_path for p in EXCLUDE_PATH_SUBSTRINGS):
        return False

    item_id = item["id"]
    etag = item.get("eTag", "")
    if _is_unchanged(sp_conn, item_id, etag):
        return False

    dest = temp_dir / site_cfg["name"] / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        download_to_temp(drive_id, item_id, dest, tok)
        try:
            chunks = _chunk_file_safe(dest, site_cfg["name"])
        except (_ChunkTimeout, _BrokenExecutor):
            # Retryable failure (transient crash/timeout), NOT a permanent skip
            # — propagate so the caller parks it in the retry queue. See M47.
            logger.error("SP chunk_file timed out or worker crashed: %s", name)
            raise
        if not chunks:
            return False  # parsed but produced no text — permanent skip, no retry

        # Override source to SharePoint web URL (temp path is meaningless)
        web_url = item.get("webUrl", rel_path)
        for chunk in chunks:
            chunk["source"] = web_url
            chunk["repo"] = site_cfg["name"]

        texts = [c["text"] for c in chunks]
        vectors = embed_documents(texts)
        sparse_vecs = embed_sparse(texts)

        collection = site_cfg["collection"]
        _ensure_collection(client, collection)
        _delete_old_chunks(client, sp_conn, item_id)
        point_ids = _upsert_chunks(client, collection, chunks, vectors, sparse_vecs)
        _record_item(
            sp_conn, item_id, rel_path, site_cfg["name"],
            drive_id, etag, collection, point_ids, web_url,
        )
        logger.info("SP indexed: %s → %d chunks", rel_path, len(chunks))
        return True

    except Exception as exc:
        # Download/embed/upsert failure — retryable; propagate so the caller
        # parks it in the retry queue (previously swallowed as a silent skip).
        logger.error("SP processing failed for %s: %s", name, exc)
        raise
    finally:
        if own_conn:
            sp_conn.close()
        dest.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Drive resolution
# ---------------------------------------------------------------------------

def _resolve_drive_ids(site_cfg: dict, token: str) -> dict[str, str]:
    """Return {drive_name: drive_id} for a site config entry."""
    site_id = get_site_id(site_cfg["teams_group_id"], token)
    result = {}
    for drive_name in site_cfg.get("drives", ["Documents"]):
        drive_id = get_drive_id(site_id, drive_name, token)
        result[drive_name] = drive_id
        logger.info(
            "SP site '%s' drive '%s' → %s", site_cfg["name"], drive_name, drive_id
        )
    return result


# ---------------------------------------------------------------------------
# Full sync
# ---------------------------------------------------------------------------

TOKEN_REFRESH_SECONDS = 45 * 60  # refresh token after 45 min (expires at 60 min)


def _fresh_token(cfg: dict, token: str, token_ts: float) -> tuple[str, float]:
    """Return a refreshed token if TOKEN_REFRESH_SECONDS have elapsed, else current."""
    if time.monotonic() - token_ts >= TOKEN_REFRESH_SECONDS:
        try:
            token = authenticate(cfg["auth"])
            token_ts = time.monotonic()
            logger.info("SP token refreshed")
        except Exception as exc:
            logger.warning("SP token refresh failed, continuing with old token: %s", exc)
    return token, token_ts


def full_sync(site_cfg: dict, cfg: dict, token: str, skip_files: int = 0, keep_cache: bool = False) -> dict:
    """Enumerate all items and index each one. Establishes/refreshes delta baseline.

    Uses the Graph delta endpoint for listing (single paginated stream) instead of
    recursive children calls. This avoids token expiry during the listing phase and
    saves the delta baseline *before* processing starts — so if processing is
    interrupted, the next run uses delta (only changed items) rather than re-listing
    from scratch.
    """
    client = QdrantClient(url=QDRANT_URL)
    sp_conn = _get_sp_manifest_conn()
    temp_dir = Path(cfg.get("temp_dir", "/tmp/qnoe-sharepoint/"))
    stats: dict = {"processed": 0, "skipped": 0, "errors": 0, "failed_files": []}
    _ensure_chunk_server()  # start the clean chunk forkserver before any embedding
    token_ts = time.monotonic()

    drive_map = _resolve_drive_ids(site_cfg, token)
    for drive_name, drive_id in drive_map.items():
        logger.info("SP full sync: %s / %s", site_cfg["name"], drive_name)

        # Try listing cache first to skip the 15-min listing phase on restarts
        cached_meta = _check_listing_cache(drive_id)
        if cached_meta:
            delta_link, _ = cached_meta
        else:
            try:
                token, token_ts = _fresh_token(cfg, token, token_ts)
                all_items, delta_link = get_delta(drive_id, None, token, auth_cfg=cfg["auth"])
            except Exception as exc:
                logger.error("SP full sync listing failed for %s/%s: %s", site_cfg["name"], drive_name, exc)
                stats["errors"] += 1
                continue
            _save_listing_cache(drive_id, all_items, delta_link)
            del all_items  # free memory immediately — we stream from JSONL below

        logger.info(
            "SP: streaming items for %s / %s (skip_files=%d)",
            site_cfg["name"], drive_name, skip_files,
        )

        # Save delta baseline before processing — crash-safe
        _save_delta_link(drive_id, delta_link)
        logger.info("SP delta baseline saved for %s / %s", site_cfg["name"], drive_name)

        # Create a shared token holder — each worker thread calls holder.get()
        # which auto-refreshes when TOKEN_REFRESH_SECONDS have elapsed.
        token, token_ts = _fresh_token(cfg, token, token_ts)
        holder = _SharedToken(token, cfg["auth"])

        def _submit(item: dict) -> bool:
            return _process_item(item, site_cfg, drive_id, temp_dir, holder, client)

        # Bounded sliding-window submission: at most 2×workers futures in flight at once.
        # This avoids holding all 500K+ items in memory as a futures dict.
        MAX_QUEUED = THREAD_WORKERS * 2
        pending: dict = {}
        item_gen = _stream_listing_cache(drive_id, skip_files)
        done = 0

        def _fill_queue() -> None:
            while len(pending) < MAX_QUEUED and _memory_ok():
                try:
                    item = next(item_gen)
                    fut = pool.submit(_submit, item)
                    pending[fut] = item
                except StopIteration:
                    break
            if not _memory_ok():
                free_gb = psutil.virtual_memory().available / (1024 ** 3)
                logger.warning("SP memory guard: %.1f GB free — throttling submissions", free_gb)

        with _TPE(max_workers=THREAD_WORKERS) as pool:
            _fill_queue()
            while pending:
                for fut in as_completed(pending):
                    item = pending.pop(fut)
                    try:
                        ok = fut.result()
                        if ok:
                            stats["processed"] += 1
                        else:
                            stats["skipped"] += 1
                    except Exception as exc:
                        logger.error("SP item error for %s: %s", item.get("name", "?"), exc)
                        stats["errors"] += 1
                        stats["failed_files"].append(item.get("name", "unknown"))
                    done += 1
                    if done % 500 == 0:
                        logger.info(
                            "SP progress: %d files — %d indexed, %d skipped, %d errors",
                            done + skip_files,
                            stats["processed"], stats["skipped"], stats["errors"],
                        )
                    _fill_queue()
                    break  # restart as_completed with updated pending dict

        if not keep_cache:
            _clear_listing_cache(drive_id)
        else:
            logger.info("SP listing cache retained for post-processing (keep_cache=True)")

    sp_conn.close()
    return stats


# ---------------------------------------------------------------------------
# Delta sync
# ---------------------------------------------------------------------------

def delta_sync(site_cfg: dict, cfg: dict, token: str) -> dict:
    """Process only items changed since last sync using Graph delta API."""
    client = QdrantClient(url=QDRANT_URL)
    sp_conn = _get_sp_manifest_conn()
    temp_dir = Path(cfg.get("temp_dir", "/tmp/qnoe-sharepoint/"))
    stats: dict = {"processed": 0, "new": 0, "updated": 0, "skipped": 0, "deleted": 0, "errors": 0, "failed_files": [], "skipped_files": []}

    _ensure_chunk_server()  # start the clean chunk forkserver before any embedding
    token_ts = time.monotonic()
    drive_map = _resolve_drive_ids(site_cfg, token)
    for drive_name, drive_id in drive_map.items():
        stored_link = _get_stored_delta_link(drive_id)
        if stored_link is None:
            logger.info(
                "SP: no delta link for %s/%s — running full sync to establish baseline",
                site_cfg["name"], drive_name,
            )
            sp_conn.close()
            full_sync(site_cfg, cfg, token)
            return stats

        try:
            items, new_delta_link = get_delta(drive_id, stored_link, token)
        except Exception as exc:
            logger.error(
                "SP delta fetch failed for %s/%s: %s", site_cfg["name"], drive_name, exc
            )
            stats["errors"] += 1
            continue

        logger.info(
            "SP delta: %d changed items in %s/%s",
            len(items), site_cfg["name"], drive_name,
        )

        # Re-attempt items parked from earlier cycles first (bounded: one attempt
        # per cycle). Items that fail below wait for the next cycle's retry pass.
        token, token_ts = _fresh_token(cfg, token, token_ts)
        try:
            _process_retry_queue(site_cfg, cfg, drive_id, temp_dir, token, client, sp_conn, stats)
        except Exception as exc:
            logger.error("SP retry queue failed for %s/%s: %s", site_cfg["name"], drive_name, exc)

        for item in items:
            item_id = item["id"]
            if "deleted" in item:
                _delete_item(sp_conn, client, item_id)
                _dequeue_retry(sp_conn, item_id)  # nothing left to retry
                stats["deleted"] += 1
                continue
            if "file" not in item:
                continue
            token, token_ts = _fresh_token(cfg, token, token_ts)
            is_new_item = sp_conn.execute(
                "SELECT 1 FROM sp_manifest WHERE item_id = ?", (item["id"],)
            ).fetchone() is None
            try:
                ok = _process_item(item, site_cfg, drive_id, temp_dir, token, client, sp_conn)
                if ok:
                    stats["processed"] += 1
                    if is_new_item:
                        stats["new"] += 1
                    else:
                        stats["updated"] += 1
                    _dequeue_retry(sp_conn, item_id)  # clear any prior failure
                else:
                    # Permanent skip (unsupported ext / oversized / excluded /
                    # unchanged / empty parse) — not a failure, do not retry.
                    stats["skipped"] += 1
                    stats["skipped_files"].append(item.get("name", "unknown"))
            except Exception as exc:
                # Retryable failure (chunk crash, download/embed error). Park it
                # so it is NOT lost when the delta token advances below. See M47.
                logger.error("SP delta item error: %s", exc)
                stats["errors"] += 1
                stats["failed_files"].append(item.get("name", "unknown"))
                _enqueue_retry(sp_conn, item, drive_id, site_cfg["name"], str(exc))

        _save_delta_link(drive_id, new_delta_link)

    # Surface the retry queue so parked/stuck items aren't silently lost.
    try:
        depth = sp_conn.execute("SELECT COUNT(*) FROM sp_retry_queue").fetchone()[0]
        if depth:
            stats["retry_queued"] = depth
            exhausted = [r[0] for r in sp_conn.execute(
                "SELECT name FROM sp_retry_queue WHERE attempts >= ?",
                (SP_RETRY_MAX_ATTEMPTS,)).fetchall()]
            if exhausted:
                stats["retry_exhausted"] = exhausted
                logger.warning(
                    "SP retry: %d item(s) exhausted %d attempts, giving up: %s",
                    len(exhausted), SP_RETRY_MAX_ATTEMPTS, ", ".join(exhausted[:10]),
                )
    except Exception:
        pass

    sp_conn.close()
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="SharePoint sync for QNOE agent")
    parser.add_argument(
        "--full", action="store_true",
        help="Full sync (default: delta only)",
    )
    parser.add_argument("--site", default=None, help="Sync only this site (by name)")
    parser.add_argument("--config", default=None, help="Path to sharepoint.yaml")
    parser.add_argument(
        "--validate", action="store_true",
        help="Auth check + list drives only; no indexing",
    )
    parser.add_argument(
        "--skip-files", type=int, default=0, metavar="N",
        help="Skip the first N files in the listing (resume from a known position)",
    )
    parser.add_argument(
        "--keep-cache", action="store_true",
        help="Retain JSONL listing cache after sync (for use by post-processing jobs like ingest_sp_qcodes)",
    )
    args = parser.parse_args()

    cfg = load_sharepoint_config(args.config)
    token = authenticate(cfg["auth"])
    logger.info("Authentication OK")

    sites = cfg["sites"]
    if args.site:
        sites = [s for s in sites if s["name"] == args.site]
        if not sites:
            logger.error("Site '%s' not found in config", args.site)
            return

    if args.validate:
        for site in sites:
            site_id = get_site_id(site["teams_group_id"], token)
            logger.info("Site '%s' → %s", site["name"], site_id)
            drive_map = _resolve_drive_ids(site, token)
            for name, drive_id in drive_map.items():
                logger.info("  drive '%s' → %s", name, drive_id)
        return

    for site in sites:
        if args.full:
            stats = full_sync(site, cfg, token, skip_files=args.skip_files, keep_cache=args.keep_cache)
        else:
            stats = delta_sync(site, cfg, token)
        logger.info("SP sync done for '%s': %s", site["name"], stats)


if __name__ == "__main__":
    main()
