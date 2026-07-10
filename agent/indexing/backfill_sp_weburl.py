"""Backfill the ``web_url`` column in ``sp_manifest`` from Qdrant payloads.

The ``web_url`` column was added to ``sp_manifest`` after most SharePoint
files had already been indexed. For those existing rows the SharePoint web
link still lives in the Qdrant chunk payload's ``source`` field (set in
``sharepoint_sync._process_item``). This one-shot script copies it back into
the manifest so the ``find_file`` tool can return clickable SharePoint links
immediately — without waiting for a full re-sync (delta sync only rewrites
*changed* files, so unchanged rows would otherwise never be backfilled).

Idempotent + resumable: only rows where ``web_url`` IS NULL or '' are touched,
so re-running after an interruption just resumes.

Usage:
  python -m agent.indexing.backfill_sp_weburl            # backfill
  python -m agent.indexing.backfill_sp_weburl --dry-run  # report only
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from collections import defaultdict

from qdrant_client import QdrantClient

logger = logging.getLogger(__name__)

SP_MANIFEST_DB = os.environ.get("SP_MANIFEST_DB", "/opt/qnoe-agent/memory/sharepoint.db")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")

RETRIEVE_BATCH = 256


def _ensure_web_url_column(conn: sqlite3.Connection) -> None:
    """Add the web_url column if this manifest predates it.

    Lets the backfill run standalone before any sync has migrated the schema.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sp_manifest)")}
    if "web_url" not in cols:
        conn.execute("ALTER TABLE sp_manifest ADD COLUMN web_url TEXT")
        conn.commit()
        logger.info("Added missing web_url column to sp_manifest")


def _first_point_id(point_ids_json: str) -> str | None:
    try:
        ids = json.loads(point_ids_json) if point_ids_json else []
    except (json.JSONDecodeError, TypeError):
        return None
    return ids[0] if ids else None


def backfill(dry_run: bool = False) -> dict:
    stats = {"scanned": 0, "resolved": 0, "missing_payload": 0, "no_points": 0, "updated": 0}

    conn = sqlite3.connect(SP_MANIFEST_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_web_url_column(conn)

    # Rows still needing a web_url, grouped by collection so we can batch-retrieve.
    rows = conn.execute(
        """SELECT id, collection, point_ids
             FROM sp_manifest
            WHERE web_url IS NULL OR web_url = ''"""
    ).fetchall()
    stats["scanned"] = len(rows)
    logger.info("Rows needing web_url: %d", len(rows))
    if not rows:
        conn.close()
        return stats

    # collection -> list of (manifest_id, first_point_id)
    by_collection: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for manifest_id, collection, point_ids_json in rows:
        pid = _first_point_id(point_ids_json)
        if pid is None:
            stats["no_points"] += 1
            continue
        by_collection[collection].append((manifest_id, pid))

    client = QdrantClient(url=QDRANT_URL)
    updates: list[tuple[str, int]] = []  # (web_url, manifest_id)

    for collection, entries in by_collection.items():
        for i in range(0, len(entries), RETRIEVE_BATCH):
            batch = entries[i : i + RETRIEVE_BATCH]
            pid_to_mid = {pid: mid for mid, pid in batch}
            try:
                points = client.retrieve(
                    collection_name=collection,
                    ids=list(pid_to_mid.keys()),
                    with_payload=["source"],
                    with_vectors=False,
                )
            except Exception as exc:
                logger.warning("Retrieve failed for %s batch %d: %s", collection, i, exc)
                continue
            resolved_mids = set()
            for p in points:
                source = (p.payload or {}).get("source", "")
                mid = pid_to_mid.get(str(p.id)) or pid_to_mid.get(p.id)
                if mid is None or not source:
                    continue
                updates.append((source, mid))
                resolved_mids.add(mid)
                stats["resolved"] += 1
            # Anything in this batch we couldn't resolve (point gone from Qdrant,
            # or payload had no source) stays NULL and is left for a later re-sync.
            stats["missing_payload"] += len(batch) - len(resolved_mids)
            logger.info(
                "%s: %d/%d resolved so far", collection, stats["resolved"], stats["scanned"]
            )

    if dry_run:
        logger.info("[dry-run] would update %d rows", len(updates))
        stats["updated"] = 0
    else:
        conn.executemany("UPDATE sp_manifest SET web_url = ? WHERE id = ?", updates)
        conn.commit()
        stats["updated"] = len(updates)
        logger.info("Updated %d rows", len(updates))

    conn.close()
    return stats


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Backfill sp_manifest.web_url from Qdrant")
    parser.add_argument("--dry-run", action="store_true", help="Report only; no writes")
    args = parser.parse_args()

    stats = backfill(dry_run=args.dry_run)
    logger.info("Backfill done: %s", stats)


if __name__ == "__main__":
    main()
