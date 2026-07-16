"""Parallelized full server ingest via the broad-access /mnt/noe mount.

WHY: the nightly scan reads /ICFO/groups/NOE (cred `yzamir`), which is ACL-denied
645+ folders. The /mnt/noe mount (cred `sberlanga`, uid=qnoe-ai) can read them.
This one-time job reads via /mnt/noe but STORES canonical /ICFO paths so files
already indexed via /ICFO dedupe by hash (no dupes). See FULL_SERVER_INGEST_PLAN.md.

CONCURRENCY MODEL — memory-gated semaphore over RECYCLED subprocess batches:
  Docling accumulates/leaks memory across conversions in a persistent process
  (a worker ballooned to 31 GB → OOM-killer → broke the whole ProcessPool). So
  instead of long-lived pool workers we run each small BATCH in a FRESH
  `run_ingest --file-list` subprocess that EXITS (freeing memory) when done. A
  semaphore caps concurrency AND gates on free RAM: a new batch launches only
  when (running < WORKERS) AND (MemAvailable >= MIN_FREE_GB). An OOM kills just
  that one batch (rc!=0, logged); the run continues and is resumable (the sha256
  manifest skips done files; the find-cache skips the find).

LESSONS: no find timeout (M7); cached find-manifest + per-file dedup = resumable;
run with vLLM STOPPED. Big files are skipped by DOCLING_MAX_FILE_BYTES.

RUN: bash scripts/run_full_server_ingest.sh   (sets all env)
"""
import argparse
import logging
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from .ingest_server import SERVER_FOLDERS, COLLECTION
from .run_ingest import _find_files, _store_key

logger = logging.getLogger("parallel_server_ingest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SERVER_ROOT = Path(os.environ.get("SERVER_ROOT", "/mnt/noe"))
AGENT_DATA_DIR = os.environ.get("AGENT_DATA_DIR", "/home/yzamir/qnoe_server_data")
FIND_CACHE = Path(os.environ.get("FIND_CACHE", os.path.join(AGENT_DATA_DIR, "full_scan_filelist.txt")))


def _mem_available_gb() -> float:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except Exception:
        pass
    return 0.0


def build_file_list(refresh: bool) -> list[Path]:
    """Every indexable file under the allowlisted folders (minus EXCLUDE_FOLDERS),
    caching the (un-timed) find to FIND_CACHE for resumability."""
    if FIND_CACHE.exists() and not refresh:
        files = [Path(x) for x in FIND_CACHE.read_text().splitlines() if x.strip()]
        logger.info("Reusing cached find-manifest: %d files (%s). --refresh-find to rescan.",
                    len(files), FIND_CACHE)
        return files
    excluded = {f.strip() for f in os.environ.get("EXCLUDE_FOLDERS", "").split(",") if f.strip()}
    folders = [f for f in SERVER_FOLDERS if f not in excluded]
    if excluded:
        logger.info("EXCLUDE_FOLDERS (skipped in this /mnt/noe scan): %s", sorted(excluded))
    all_files: list[Path] = []
    for folder in folders:
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


def _folder_of(path: Path) -> str:
    try:
        return path.relative_to(SERVER_ROOT).parts[0]
    except Exception:
        return "?"


def main() -> None:
    ap = argparse.ArgumentParser(description="Memory-gated parallel server ingest via /mnt/noe")
    ap.add_argument("--workers", type=int, default=int(os.environ.get("WORKERS", "6")),
                    help="max concurrent batch subprocesses (semaphore)")
    ap.add_argument("--batch-size", type=int, default=int(os.environ.get("BATCH_SIZE", "60")),
                    help="files per subprocess (small = memory recycled sooner)")
    ap.add_argument("--min-free-gb", type=float, default=float(os.environ.get("MIN_FREE_GB", "40")),
                    help="do not launch a new batch below this free RAM")
    ap.add_argument("--refresh-find", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    read_root = os.environ.get("INGEST_READ_ROOT", "")
    store_root = os.environ.get("INGEST_STORE_ROOT", "")
    logger.info("SERVER_ROOT=%s read=%s store=%s manifest=%s/episodic.db",
                SERVER_ROOT, read_root or "(none)", store_root or "(none)", AGENT_DATA_DIR)

    files = build_file_list(args.refresh_find)
    if args.limit:
        files = files[:args.limit]
    by_folder = Counter(_folder_of(p) for p in files)
    logger.info("Total indexable files: %d across %d folders", len(files), len(by_folder))

    if args.dry_run:
        for folder, n in sorted(by_folder.items(), key=lambda kv: -kv[1]):
            logger.info("  %-22s %d", folder, n)
        for p in files[:5]:
            logger.info("  %s -> %s", p, _store_key(p))
        logger.info("DRY-RUN: no writes.")
        return

    # Write small batch files, each processed by a fresh subprocess (recycles memory).
    batchdir = Path(tempfile.mkdtemp(prefix="ingest_batches_", dir="/tmp"))
    batches: list[Path] = []
    for i in range(0, len(files), args.batch_size):
        bf = batchdir / f"batch_{i // args.batch_size:05d}.txt"
        bf.write_text("\n".join(str(p) for p in files[i:i + args.batch_size]))
        batches.append(bf)
    logger.info("%d batches of <=%d files | semaphore: <=%d concurrent AND >=%.0fGB free | manifest dedup + resumable",
                len(batches), args.batch_size, args.workers, args.min_free_gb)

    cmd_base = [sys.executable, "-m", "agent.ingest.run_ingest",
                "--team", COLLECTION, "--no-list-force", "--file-list"]
    running: dict[subprocess.Popen, Path] = {}
    idx = done = failed = 0
    while idx < len(batches) or running:
        # reap finished
        for p in list(running):
            rc = p.poll()
            if rc is None:
                continue
            bf = running.pop(p)
            done += 1
            if rc != 0:
                failed += 1
                logger.error("batch %s exited rc=%d (rc=-9 => OOM-killed) — its files stay un-done; resumable",
                             bf.name, rc)
            if done % 20 == 0 or not running:
                logger.info("progress: %d/%d batches (%d failed) | running=%d | free=%.0fGB",
                            done, len(batches), failed, len(running), _mem_available_gb())
        # launch under the semaphore: concurrency cap AND memory gate
        while idx < len(batches) and len(running) < args.workers and _mem_available_gb() >= args.min_free_gb:
            bf = batches[idx]; idx += 1
            p = subprocess.Popen(cmd_base + [str(bf)])   # inherits env + stdout/stderr(-> main log)
            running[p] = bf
        time.sleep(3)

    logger.info("Parallel ingest complete: %d/%d batches, %d failed. "
                "Re-run (cache reused) to mop up failed/skipped.", done, len(batches), failed)


if __name__ == "__main__":
    sys.exit(main())
