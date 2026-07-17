#!/usr/bin/env python3
"""SharePoint orphan sweep — remove manifest rows (and their Qdrant chunks)
for items that no longer exist on SharePoint.

The 2026-07-16 coverage audit found 8,320 noe-group manifest rows whose paths
are gone from SharePoint: full_sync never deletes, and the delta poller only
sees deletions that happen while its baseline is live — so moved/deleted files
accumulate as stale rows (and stale chunks keep surfacing in RAG/find_file).

Consumes the live-item inventory `logs/sp_live_items.jsonl` written by
`sharepoint_coverage_audit.py --dump-live` (every live file item's id+path per
site) — no second 45-min Graph listing needed. A site absent from the dump is
skipped entirely (a partial dump must never look like mass deletion).

Two removal classes:
  * orphans          — manifest item_id not among the site's live item ids
  * excluded junk    — item_path matches EXCLUDE_PATH_SUBSTRINGS (e.g.
                       .ipynb_checkpoints/ rows indexed before the exclusion
                       landed; M56 lineage) — only with --purge-excluded

Dry-run by default; --execute deletes (after backing up sharepoint.db).
Run as yzamir (same uid as the manifest writers — no cross-UID WAL, M52):

  PYTHONPATH=/opt/qnoe-agent /opt/qnoe-agent/venv/bin/python \
      /opt/qnoe-agent/scripts/sp_orphan_sweep.py [--execute] [--purge-excluded]
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests

QNOE_ROOT = os.environ.get("QNOE_ROOT", "/opt/qnoe-agent")
SP_MANIFEST_DB = os.environ.get("SP_MANIFEST_DB", f"{QNOE_ROOT}/memory/sharepoint.db")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333").rstrip("/")
DEFAULT_DUMP = f"{QNOE_ROOT}/logs/sp_live_items.jsonl"
MAX_DUMP_AGE_H = 48

# Mirror agent/ingest/sharepoint_sync.py EXCLUDE_PATH_SUBSTRINGS
EXCLUDE_PATH_SUBSTRINGS = {".env/", "/venv/", "site-packages/", "node_modules/",
                           "__pycache__/", ".ipynb_checkpoints/"}


def load_live(dump: str) -> dict[str, set[str]]:
    live: dict[str, set[str]] = defaultdict(set)
    with open(dump) as f:
        for line in f:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("site") and d.get("id"):
                live[d["site"]].add(d["id"])
    return live


def delete_qdrant_points(collection: str, point_ids: list[str]) -> bool:
    try:
        r = requests.post(
            f"{QDRANT_URL}/collections/{collection}/points/delete",
            json={"points": point_ids}, params={"wait": "true"}, timeout=120)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"WARN qdrant delete failed ({collection}, {len(point_ids)} pts): {e}")
        return False


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="SharePoint manifest orphan sweep")
    ap.add_argument("--dump", default=DEFAULT_DUMP)
    ap.add_argument("--execute", action="store_true", help="actually delete (default: dry-run)")
    ap.add_argument("--purge-excluded", action="store_true",
                    help="also purge rows matching EXCLUDE_PATH_SUBSTRINGS")
    ap.add_argument("--force-stale-dump", action="store_true",
                    help="accept a dump older than 48h")
    args = ap.parse_args(argv)

    age_h = (time.time() - os.path.getmtime(args.dump)) / 3600
    if age_h > MAX_DUMP_AGE_H and not args.force_stale_dump:
        print(f"ABORT: dump is {age_h:.0f}h old (> {MAX_DUMP_AGE_H}h) — rerun the "
              f"coverage audit with --dump-live, or pass --force-stale-dump")
        return 2
    live = load_live(args.dump)
    print(f"live dump: {args.dump} ({age_h:.1f}h old) — "
          + ", ".join(f"{s}: {len(ids)}" for s, ids in live.items()))

    conn = sqlite3.connect(SP_MANIFEST_DB)
    rows = conn.execute(
        "SELECT item_id, item_path, site_name, collection, point_ids FROM sp_manifest"
    ).fetchall()

    orphans, junk = [], []
    for item_id, path, site, coll, pids in rows:
        if any(p in path for p in EXCLUDE_PATH_SUBSTRINGS):
            junk.append((item_id, path, site, coll, pids))
        elif site in live and item_id not in live[site]:
            orphans.append((item_id, path, site, coll, pids))
    skipped_sites = {r[2] for r in rows} - set(live)
    if skipped_sites:
        print(f"sites NOT in dump (skipped, not counted as orphans): {skipped_sites}")

    print(f"manifest rows: {len(rows)} | orphans: {len(orphans)} | "
          f"excluded-junk: {len(junk)} ({'will purge' if args.purge_excluded else 'ignored without --purge-excluded'})")
    for label, group in (("orphan", orphans[:8]), ("junk", junk[:5])):
        for _, path, site, _, _ in group:
            print(f"  sample {label}: [{site}] {path[:120]}")

    targets = orphans + (junk if args.purge_excluded else [])
    if not args.execute:
        print(f"DRY-RUN: would delete {len(targets)} rows + their Qdrant points. "
              f"Re-run with --execute.")
        conn.close()
        return 0

    backup = f"{SP_MANIFEST_DB}.bak-pre-sweep-{time.strftime('%Y%m%d')}"
    if not os.path.exists(backup):
        shutil.copy2(SP_MANIFEST_DB, backup)
        print(f"backup: {backup}")

    by_coll: dict[str, list[str]] = defaultdict(list)
    for _, _, _, coll, pids in targets:
        try:
            by_coll[coll].extend(json.loads(pids) if pids else [])
        except (TypeError, ValueError):
            pass
    deleted_pts = 0
    for coll, pids in by_coll.items():
        for i in range(0, len(pids), 500):
            if delete_qdrant_points(coll, pids[i:i + 500]):
                deleted_pts += len(pids[i:i + 500])

    cur = conn.executemany("DELETE FROM sp_manifest WHERE item_id = ?",
                           [(t[0],) for t in targets])
    conn.commit()
    left = conn.execute("SELECT COUNT(*) FROM sp_manifest").fetchone()[0]
    conn.close()
    print(f"DONE: deleted {cur.rowcount} manifest rows "
          f"({len(orphans)} orphans, {len(junk) if args.purge_excluded else 0} junk) "
          f"+ {deleted_pts} Qdrant points | rows remaining: {left}")
    stats = Counter(t[2] for t in targets)
    print("by site:", dict(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
