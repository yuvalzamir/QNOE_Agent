"""Parallelized full server ingest via the broad-access /mnt/noe mount.

WHY: the nightly scan reads /ICFO/groups/NOE (cred `yzamir`), which is ACL-denied
645+ folders (whole Fabrication tree, Data Backup, per-person dirs, …). The
`/mnt/noe` mount (cred `sberlanga`, uid=qnoe-ai) can read them. This one-time job
reads via /mnt/noe but STORES canonical /ICFO paths (INGEST_STORE_ROOT), so files
already indexed via /ICFO dedupe by hash — NO duplicate points — and find_file
returns canonical paths. See FULL_SERVER_INGEST_PLAN.md.

LESSONS BAKED IN:
  * NO timeout on find (M7) — a full CIFS traversal takes hours; a timeout
    silently truncates. Let it finish.
  * Cached find-manifest — the (slow) find result is written to FIND_CACHE; an
    interrupted run re-uses it instead of re-scanning. `--refresh-find` forces a
    fresh find.
  * Resumable — the sha256 index_manifest skips already-indexed files, so a
    re-run (after a crash / partial run) only does the remainder.
  * Parallel worker PROCESSES (default cpu-2, capped) — run with vLLM STOPPED to
    free RAM/CPU for embedding + Docling.

RUN (all env explicit; see the plan doc for the wrapper):
  INGEST_READ_ROOT=/mnt/noe INGEST_STORE_ROOT=/ICFO/groups/NOE \
  SERVER_ROOT=/mnt/noe AGENT_DATA_DIR=/home/yzamir/qnoe_server_data \
  QDRANT_URL=http://localhost:6333 \
  FASTEMBED_CACHE_PATH=/opt/qnoe-agent/memory/fastembed_cache \
  PYTHONPATH=/opt/qnoe-agent /opt/qnoe-agent/venv/bin/python \
    -m agent.ingest.parallel_server_ingest --workers 12
"""
import argparse
import logging
import os
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from .ingest_server import SERVER_FOLDERS, COLLECTION
from .run_ingest import _find_files, ingest_directory

logger = logging.getLogger("parallel_server_ingest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SERVER_ROOT = Path(os.environ.get("SERVER_ROOT", "/mnt/noe"))
AGENT_DATA_DIR = os.environ.get("AGENT_DATA_DIR", "/home/yzamir/qnoe_server_data")
MANIFEST_DB = os.path.join(AGENT_DATA_DIR, "episodic.db")
FIND_CACHE = Path(os.environ.get("FIND_CACHE", os.path.join(AGENT_DATA_DIR, "full_scan_filelist.txt")))


def build_file_list(refresh: bool) -> list[Path]:
    """Return every indexable file under the allowlisted folders (via SERVER_ROOT),
    caching the (un-timed) find result to FIND_CACHE for resumability."""
    if FIND_CACHE.exists() and not refresh:
        files = [Path(x) for x in FIND_CACHE.read_text().splitlines() if x.strip()]
        logger.info("Reusing cached find-manifest: %d files (%s). Use --refresh-find to rescan.",
                    len(files), FIND_CACHE)
        return files
    all_files: list[Path] = []
    for folder in SERVER_FOLDERS:
        fp = SERVER_ROOT / folder
        if not fp.exists():
            logger.warning("Folder not present, skipping: %s", fp)
            continue
        logger.info("find (no timeout): %s ...", fp)
        found = _find_files(fp)          # NO timeout (M7)
        logger.info("  %-22s -> %d files", folder, len(found))
        all_files.extend(found)
    FIND_CACHE.parent.mkdir(parents=True, exist_ok=True)
    FIND_CACHE.write_text("\n".join(str(p) for p in all_files))
    logger.info("Cached find-manifest: %d files -> %s", len(all_files), FIND_CACHE)
    return all_files


def _worker(shard: list[str]) -> int:
    """Ingest a shard (runs in a child process; env inherited). list_force=False
    keeps hash-dedup so a re-run skips already-indexed files."""
    ingest_directory(
        team=COLLECTION,
        repo_path=SERVER_ROOT,
        repo_name="server",
        file_list=[Path(p) for p in shard],
        list_force=False,
        manifest_db=MANIFEST_DB,
    )
    return len(shard)


def _folder_of(path: Path) -> str:
    try:
        return path.relative_to(SERVER_ROOT).parts[0]
    except Exception:
        return "?"


def main() -> None:
    ap = argparse.ArgumentParser(description="Parallel full server ingest via /mnt/noe")
    ap.add_argument("--workers", type=int,
                    default=min(12, max(2, (os.cpu_count() or 4) - 2)))
    ap.add_argument("--refresh-find", action="store_true", help="re-run the CIFS find (ignore cache)")
    ap.add_argument("--dry-run", action="store_true", help="build the plan; no writes")
    ap.add_argument("--limit", type=int, default=0, help="cap N files (smoke test)")
    args = ap.parse_args()

    read_root = os.environ.get("INGEST_READ_ROOT", "")
    store_root = os.environ.get("INGEST_STORE_ROOT", "")
    logger.info("SERVER_ROOT=%s  read=%s  store=%s  manifest=%s",
                SERVER_ROOT, read_root or "(none)", store_root or "(none)", MANIFEST_DB)
    if not read_root or not store_root:
        logger.warning("INGEST_READ_ROOT / INGEST_STORE_ROOT not set — paths will NOT "
                       "be normalized (stored as read from %s).", SERVER_ROOT)

    files = build_file_list(args.refresh_find)
    if args.limit:
        files = files[:args.limit]

    by_folder = Counter(_folder_of(p) for p in files)
    logger.info("Total indexable files: %d across %d folders", len(files), len(by_folder))
    for folder, n in sorted(by_folder.items(), key=lambda kv: -kv[1]):
        logger.info("  %-22s %d", folder, n)

    if args.dry_run:
        logger.info("--- DRY-RUN sample (read path -> stored path) ---")
        from .run_ingest import _store_key
        for p in files[:8]:
            logger.info("  %s\n     -> %s", p, _store_key(p))
        logger.info("DRY-RUN: no writes. %d files would be processed by %d workers.",
                    len(files), args.workers)
        return

    # Round-robin shard so large/small files spread evenly across workers.
    shards: list[list[str]] = [[] for _ in range(args.workers)]
    for i, p in enumerate(files):
        shards[i % args.workers].append(str(p))
    shards = [s for s in shards if s]
    logger.info("Launching %d workers, ~%d files each. (Resumable: re-run to finish "
                "after any crash — the sha256 manifest skips done files.)",
                len(shards), len(files) // max(1, len(shards)))

    done = 0
    with ProcessPoolExecutor(max_workers=len(shards)) as ex:
        futs = {ex.submit(_worker, s): i for i, s in enumerate(shards)}
        for f in as_completed(futs):
            i = futs[f]
            try:
                done += f.result()
                logger.info("worker %d done (%d files cumulative)", i, done)
            except Exception as exc:
                logger.error("worker %d FAILED: %s — re-run to resume its remainder", i, exc)
    logger.info("Parallel ingest pass complete: %d files dispatched. "
                "Re-run (cache reused) to mop up any skipped/failed.", len(files))


if __name__ == "__main__":
    sys.exit(main())
