#!/usr/bin/env python3
"""Rewrite a stored path PREFIX across the index manifest + Qdrant payloads.

Fixes rows that were ingested via a non-canonical mount so every stored
surface (RAG chunk `source` payloads, find_file's index_manifest, orphan
sweep's missing_files) shows the canonical path. First use: CavityQED was
ingested via /mnt/noe (the broad-cred mount — /ICFO is ACL-denied there), so
its 97 manifest rows + 1,193 chunks say `/mnt/noe/...` instead of the
canonical `/ICFO/groups/NOE/...` (same SMB share, different credential).

Surgical: only rows LIKE <from-prefix>/% are touched, and Qdrant updates go
by each row's stored point_ids — no scroll-filter over the collection.
Rows whose rewritten path already has a manifest row are SKIPPED with a
warning (UNIQUE constraint; means the file was later re-ingested canonically
— resolve those by hand or purge the stale row).

Dry-run by default. Typical CavityQED invocation (on the DGX):

  PYTHONPATH=/opt/qnoe-agent /opt/qnoe-agent/venv/bin/python3 \
    scripts/rewrite_path_prefix.py \
    --manifest /home/yzamir/qnoe_server_data/episodic.db \
    --from-prefix /mnt/noe --to-prefix /ICFO/groups/NOE [--execute]
"""
import argparse
import json
import os
import sqlite3
import sys

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", required=True, help="episodic.db holding index_manifest")
    ap.add_argument("--from-prefix", required=True)
    ap.add_argument("--to-prefix", required=True)
    ap.add_argument("--execute", action="store_true", help="apply (default: dry-run)")
    args = ap.parse_args(argv)

    src = args.from_prefix.rstrip("/")
    dst = args.to_prefix.rstrip("/")
    conn = sqlite3.connect(args.manifest)
    rows = conn.execute(
        "SELECT id, file_path, collection, point_ids FROM index_manifest "
        "WHERE file_path LIKE ?", (src + "/%",)).fetchall()
    print(f"{len(rows)} manifest row(s) under {src}/")
    if not rows:
        conn.close()
        return 0

    qc = None
    if args.execute:
        from qdrant_client import QdrantClient
        qc = QdrantClient(url=QDRANT_URL)
    has_missing = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='missing_files'"
    ).fetchone() is not None

    done = skipped = points = 0
    for row_id, old_path, collection, point_ids_json in rows:
        new_path = dst + old_path[len(src):]
        conflict = conn.execute(
            "SELECT 1 FROM index_manifest WHERE file_path = ?", (new_path,)
        ).fetchone()
        if conflict:
            print(f"  SKIP (canonical row already exists): {old_path}")
            skipped += 1
            continue
        ids = json.loads(point_ids_json or "[]")
        if args.execute:
            conn.execute("UPDATE index_manifest SET file_path = ? WHERE id = ?",
                         (new_path, row_id))
            if has_missing:
                conn.execute("UPDATE OR IGNORE missing_files SET file_path = ? "
                             "WHERE file_path = ?", (new_path, old_path))
            if ids:
                qc.set_payload(collection_name=collection,
                               payload={"source": new_path}, points=ids)
        done += 1
        points += len(ids)

    if args.execute:
        conn.commit()
    conn.close()
    mode = "REWROTE" if args.execute else "DRY-RUN would rewrite"
    print(f"{mode} {done} row(s) ({points} Qdrant point(s)); {skipped} skipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
