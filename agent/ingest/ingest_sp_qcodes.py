"""One-time ingestion of QCoDeS .db files from SharePoint into the qcodes-runs collection.

Streams .db items from the existing JSONL listing cache (written by sharepoint_sync --keep-cache),
downloads each file to a temp path, runs scan_specific_dbs (same pipeline as CIFS),
then deletes the temp file. Results land in qcodes_registry + qcodes-runs Qdrant collection.

Run immediately after:
  python -m agent.ingest.sharepoint_sync --full --site noe-group --keep-cache

Then:
  cd /opt/qnoe-agent
  PYTHONPATH=/opt/qnoe-agent \\
    SHAREPOINT_USERNAME=$(grep SHAREPOINT_USERNAME secrets/sharepoint.env | cut -d= -f2) \\
    SHAREPOINT_PASSWORD=$(grep SHAREPOINT_PASSWORD secrets/sharepoint.env | cut -d= -f2) \\
    nohup venv/bin/python -m agent.ingest.ingest_sp_qcodes \\
    > logs/sp_qcodes_ingest.log 2>&1 &

Do NOT run again after completion — SharePoint DBs are treated as historic snapshots only.
"""
import asyncio
import logging
import os
import time
from pathlib import Path

import psutil
import yaml

from .sharepoint_client import authenticate, download_to_temp, get_delta, get_drive_id, get_site_id
from .sharepoint_sync import (
    _check_listing_cache,
    _stream_listing_cache,
    _save_listing_cache,
    _resolve_drive_ids,
    _SharedToken,
    load_sharepoint_config,
    TOKEN_REFRESH_SECONDS,
    MIN_FREE_GB,
)
from .qcodes_scanner import scan_specific_dbs

logger = logging.getLogger(__name__)

SP_CONFIG_PATH = os.environ.get("SHAREPOINT_CONFIG", "/opt/qnoe-agent/config/sharepoint.yaml")
TEMP_DIR = Path(os.environ.get("SP_TEMP_DIR", "/tmp/qnoe-sharepoint-qcodes/"))
MAX_FILE_MB = int(os.environ.get("SP_QCODES_MAX_MB", "3000"))
_SKIP_DB_NAMES = {"thumbs.db", "desktop.ini"}


def _stream_db_items(drive_id: str, token: str, auth_cfg: dict):
    """Stream .db file items from JSONL cache, falling back to fresh Graph API listing."""
    if not _check_listing_cache(drive_id):
        logger.info("No listing cache found — fetching from Graph API (takes ~15 min)")
        all_items, delta_link = get_delta(drive_id, None, token, auth_cfg=auth_cfg)
        _save_listing_cache(drive_id, all_items, delta_link)
        del all_items
    else:
        logger.info("Using existing JSONL listing cache for DB scan")

    for item in _iter_all_from_cache(drive_id):
        name = item.get("name", "")
        if Path(name).suffix.lower() == ".db" and name.lower() not in _SKIP_DB_NAMES:
            yield item


def _iter_all_from_cache(drive_id: str):
    """Stream ALL items (files + folders) from JSONL cache — used for DB scanning."""
    import json
    from .sharepoint_sync import _listing_cache_path
    p = _listing_cache_path(drive_id)
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
            if "file" in item and "deleted" not in item:
                yield item


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    cfg = load_sharepoint_config()
    token = authenticate(cfg["auth"])
    token_ts = time.monotonic()
    logger.info("Authentication OK")

    total_dbs = 0
    total_new_runs = 0
    total_errors = 0

    for site in cfg["sites"]:
        site_name = site["name"]
        drive_map = _resolve_drive_ids(site, token)

        for drive_name, drive_id in drive_map.items():
            logger.info("SP QCoDeS scan: %s / %s", site_name, drive_name)
            holder = _SharedToken(token, cfg["auth"])

            for item in _stream_db_items(drive_id, token, cfg["auth"]):
                name = item["name"]
                size_mb = item.get("size", 0) / (1024 * 1024)

                if size_mb > MAX_FILE_MB:
                    logger.warning("Skipping oversized DB (%.0f MB): %s", size_mb, name)
                    continue

                # Memory guard — DB files are large, only proceed when headroom exists
                free_gb = psutil.virtual_memory().available / (1024 ** 3)
                if free_gb < MIN_FREE_GB:
                    logger.warning("Memory guard: %.1f GB free — waiting before next DB", free_gb)
                    while psutil.virtual_memory().available / (1024 ** 3) < MIN_FREE_GB:
                        time.sleep(10)

                parent_path = item.get("parentReference", {}).get("path", "")
                if "root:" in parent_path:
                    parent_path = parent_path.split("root:", 1)[1].lstrip("/")
                rel_path = f"{parent_path}/{name}".lstrip("/") if parent_path else name

                dest = TEMP_DIR / site_name / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)

                logger.info("Downloading (%.0f MB): %s", size_mb, rel_path)
                try:
                    tok = holder.get()
                    download_to_temp(drive_id, item["id"], dest, tok)
                    stats = asyncio.run(scan_specific_dbs([dest]))
                    new_runs = stats.get("new_runs", 0)
                    logger.info(
                        "  → %d new runs, %d cards upserted (qcodes-runs)",
                        new_runs, stats.get("cards_upserted", 0),
                    )
                    total_dbs += 1
                    total_new_runs += new_runs
                except Exception as exc:
                    logger.error("Failed to process %s: %s", name, exc)
                    total_errors += 1
                finally:
                    dest.unlink(missing_ok=True)

    logger.info(
        "Done. %d DBs processed, %d new runs ingested, %d errors.",
        total_dbs, total_new_runs, total_errors,
    )


if __name__ == "__main__":
    main()
