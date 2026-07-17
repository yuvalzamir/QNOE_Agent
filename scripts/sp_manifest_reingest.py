#!/usr/bin/env python3
"""Targeted SharePoint re-ingest driven by a missing-files manifest.

Closes the gap found by scripts/sharepoint_coverage_audit.py (2026-07-16):
~11.7K noe-group files silently unindexed because chunk-timeout / OOM-crashed
/ zero-chunk files write no manifest row and no failure record, and the delta
poller never revisits unchanged items (fail-once = fail-forever).

Two modes:

  --build    Diff the full-sync listing cache (JSONL, kept from the Jul-8 run
             via --keep-cache) against sp_manifest and write the missing items
             to a work-list manifest (default logs/sp_missing_manifest.jsonl).
             Plot-export PDFs (numeric names under .../pdf/) and manuscript
             figure PDFs are EXCLUDED by default — they carry no text and
             mostly re-fail; --include-plots overrides.

  --execute  Memory-gated semaphore over RECYCLED subprocess batches — the
             parallel_server_ingest pattern: the manifest is split into small
             batch files, each processed by a FRESH `--execute-batch`
             subprocess that EXITS when done (frees Docling/embed memory); a
             new batch launches only when (running < WORKERS) AND
             (MemAvailable >= MIN_FREE_GB). An OOM kills just that batch
             (rc=-9, logged); etag dedup makes re-runs resume instantly.
             Ends with a reconciliation: attempted items still absent from
             sp_manifest are written to logs/sp_reingest_failed.txt — this
             run's failures are NOT silent.

  --execute-batch FILE   (internal) process one batch JSONL sequentially in
             this process, then exit.

Env knobs:
  WORKERS / BATCH_SIZE / MIN_FREE_GB   semaphore shape (defaults 6 / 25 / 25)
  SP_FILE_CHUNK_TIMEOUT  per-file chunk cap, default 300 — raise to 1800 here
  PDF_TEXTLAYER_FAST=1   born-digital PDFs via pypdf (fast), scanned skipped

Run as yzamir (same uid as the watcher/nightly manifest writers — no
cross-UID WAL side-files, see mistakes M52) with the SharePoint creds
sourced from secrets/sharepoint.env (group-readable). See
scripts/run_sp_manifest_reingest.sh for the gated launcher.
"""
import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

logger = logging.getLogger("sp_manifest_reingest")

QNOE_ROOT = os.environ.get("QNOE_ROOT", "/opt/qnoe-agent")
sys.path.insert(0, QNOE_ROOT)

SP_MANIFEST_DB = os.environ.get("SP_MANIFEST_DB", f"{QNOE_ROOT}/memory/sharepoint.db")
DEFAULT_CACHE = ("/tmp/qnoe-sp-listing-cache/"
                 "b_6T8n2h74TUuwrCDNas_S6aIAyKOIvEJCshcZSSKGoTlNllfV.jsonl")
DEFAULT_MANIFEST = f"{QNOE_ROOT}/logs/sp_missing_manifest.jsonl"
FAILED_LIST = f"{QNOE_ROOT}/logs/sp_reingest_failed.txt"

# Mirror sharepoint_sync's filters (present = "the sync should index it")
SUPPORTED_EXTENSIONS = {".py", ".ipynb", ".md", ".rst", ".pdf", ".pptx", ".docx"}
EXCLUDE_PATH_SUBSTRINGS = {".env/", "/venv/", "site-packages/", "node_modules/",
                           "__pycache__/", ".ipynb_checkpoints/"}

_NUMPLOT = re.compile(r"^\d+(_\d+)?\.pdf$")


def _item_path(item: dict) -> str:
    """Verbatim from sharepoint_sync._item_path — must match manifest rows."""
    parent_path = item.get("parentReference", {}).get("path", "")
    if "root:" in parent_path:
        parent_path = parent_path.split("root:", 1)[1].lstrip("/")
    return f"{parent_path}/{item['name']}".lstrip("/") if parent_path else item["name"]


def _is_plot_class(rel: str, ext: str) -> bool:
    """Class A from the 2026-07-17 forensics: QCoDeS notebook plot exports and
    manuscript figure PDFs — no text layer, near-zero retrieval value."""
    if ext != ".pdf":
        return False
    base = rel.rsplit("/", 1)[-1]
    if "/pdf/" in rel or _NUMPLOT.match(base):
        return True
    return "figure" in rel.lower() or base.lower().startswith("fig")


def build(cache: str, site: str, out: str, max_mb: int, include_plots: bool) -> int:
    conn = sqlite3.connect(f"file:{SP_MANIFEST_DB}?mode=ro", uri=True)
    indexed = {p for (p,) in conn.execute(
        "SELECT item_path FROM sp_manifest WHERE site_name = ?", (site,))}
    conn.close()

    kept, skipped_class = [], Counter()
    with open(cache) as f:
        f.readline()  # metadata line
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
            ext = Path(item.get("name", "")).suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            if item.get("size", 0) > max_mb * 1024 * 1024:
                continue
            rel = _item_path(item)
            if any(s in rel for s in EXCLUDE_PATH_SUBSTRINGS):
                continue
            if rel in indexed:
                continue
            if not include_plots and _is_plot_class(rel, ext):
                skipped_class["plot/figure PDF (excluded)"] += 1
                continue
            kept.append(item)
            skipped_class[ext] += 1

    kept.sort(key=lambda it: it.get("size", 0))  # cheapest first
    with open(out, "w") as f:
        for item in kept:
            f.write(json.dumps(item) + "\n")

    total_mb = sum(it.get("size", 0) for it in kept) / 1048576
    print(f"manifest written: {out}")
    print(f"items: {len(kept)} ({total_mb:.0f} MB)   [excluded plot/figure: "
          f"{skipped_class.pop('plot/figure PDF (excluded)', 0)}]")
    for k, c in skipped_class.most_common():
        print(f"  {k:8} {c}")
    return 0


def _mem_available_gb() -> float:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except Exception:
        pass
    return 0.0


def execute_batch(batch_path: str, site: str) -> int:
    """(child) Process one batch JSONL sequentially, then exit — memory is
    recycled with the process (parallel_server_ingest pattern)."""
    from qdrant_client import QdrantClient
    from agent.ingest import sharepoint_sync as sp
    from agent.ingest.sharepoint_client import authenticate

    cfg = sp.load_sharepoint_config(None)
    site_cfg = [s for s in cfg["sites"] if s["name"] == site][0]
    temp_dir = Path(cfg.get("temp_dir", "/tmp/qnoe-sharepoint/"))
    token = authenticate(cfg["auth"])
    holder = sp._SharedToken(token, cfg["auth"])
    client = QdrantClient(url=sp.QDRANT_URL)

    stats = Counter()
    with open(batch_path) as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            drive_id = item.get("parentReference", {}).get("driveId", "")
            try:
                ok = sp._process_item(item, site_cfg, drive_id, temp_dir, holder, client)
                stats["indexed" if ok else "not_indexed"] += 1
            except Exception as exc:
                logger.error("item error %s: %s", item.get("name", "?"), exc)
                stats["error"] += 1
    logger.info("BATCH DONE %s %s", Path(batch_path).name, dict(stats))
    return 0


def execute(manifest_path: str, site: str) -> int:
    """(parent) Split the manifest into batches; run each in a fresh
    subprocess under a memory-gated semaphore (parallel_server_ingest model)."""
    import subprocess
    import tempfile

    workers = int(os.environ.get("WORKERS", "6"))
    batch_size = int(os.environ.get("BATCH_SIZE", "25"))
    min_free_gb = float(os.environ.get("MIN_FREE_GB", "25"))

    lines = [l for l in open(manifest_path) if l.strip()]
    attempted_paths = [_item_path(json.loads(l)) for l in lines]
    batchdir = Path(tempfile.mkdtemp(prefix="sp_reingest_batches_", dir="/tmp"))
    batches: list[Path] = []
    for i in range(0, len(lines), batch_size):
        bf = batchdir / f"batch_{i // batch_size:05d}.jsonl"
        bf.write_text("".join(lines[i:i + batch_size]))
        batches.append(bf)
    logger.info("%d items -> %d batches of <=%d | semaphore: <=%d concurrent AND "
                ">=%.0fGB free | chunk timeout=%ss fast_pdf=%s | etag-resumable",
                len(lines), len(batches), batch_size, workers, min_free_gb,
                os.environ.get("SP_FILE_CHUNK_TIMEOUT", "300"),
                os.environ.get("PDF_TEXTLAYER_FAST", "0"))

    cmd_base = [sys.executable, os.path.abspath(__file__),
                "--site", site, "--execute-batch"]
    running: dict = {}
    idx = done = failed = 0
    while idx < len(batches) or running:
        for p in list(running):
            rc = p.poll()
            if rc is None:
                continue
            bf = running.pop(p)
            done += 1
            if rc != 0:
                failed += 1
                logger.error("batch %s exited rc=%d (rc=-9 => OOM-killed) — "
                             "its files stay un-done; resumable", bf.name, rc)
            if done % 10 == 0 or not running:
                logger.info("progress: %d/%d batches (%d failed) | running=%d | free=%.0fGB",
                            done, len(batches), failed, len(running), _mem_available_gb())
        while (idx < len(batches) and len(running) < workers
               and _mem_available_gb() >= min_free_gb):
            bf = batches[idx]; idx += 1
            running[subprocess.Popen(cmd_base + [str(bf)])] = bf
        time.sleep(3)

    # Reconciliation: what is STILL not in the manifest? (this run's failures,
    # made visible — the exact silence that created the original gap)
    conn = sqlite3.connect(f"file:{SP_MANIFEST_DB}?mode=ro", uri=True)
    indexed_now = {p for (p,) in conn.execute(
        "SELECT item_path FROM sp_manifest WHERE site_name = ?", (site,))}
    conn.close()
    still_missing = [p for p in attempted_paths if p not in indexed_now]
    with open(FAILED_LIST, "w") as f:
        f.write("\n".join(still_missing) + ("\n" if still_missing else ""))

    logger.info("DONE. attempted=%d batches=%d failed_batches=%d still_missing=%d (list: %s)",
                len(lines), done, failed, len(still_missing), FAILED_LIST)
    print(f"RESULT attempted={len(lines)} batches={done} failed_batches={failed} "
          f"still_missing={len(still_missing)} failed_list={FAILED_LIST}")
    return 0 if not still_missing else 1


def main(argv) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    ap = argparse.ArgumentParser(description="Manifest-driven SharePoint re-ingest")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--build", action="store_true")
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--execute-batch", metavar="FILE",
                      help="(internal) process one batch JSONL, then exit")
    ap.add_argument("--cache", default=DEFAULT_CACHE, help="full-sync listing cache JSONL")
    ap.add_argument("--site", default="noe-group")
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST)
    ap.add_argument("--max-mb", type=int, default=300)
    ap.add_argument("--include-plots", action="store_true",
                    help="also re-attempt plot-export/figure PDFs (class A)")
    args = ap.parse_args(argv)

    if args.build:
        return build(args.cache, args.site, args.manifest, args.max_mb, args.include_plots)
    if args.execute_batch:
        return execute_batch(args.execute_batch, args.site)
    return execute(args.manifest, args.site)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
