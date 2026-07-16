"""Ingest document files from the NOE lab server into the group-wide Qdrant collection.

Usage:
  python -m agent.ingest.ingest_server              # index all target folders
  python -m agent.ingest.ingest_server --dry-run    # print plan without writing
  python -m agent.ingest.ingest_server --force      # re-index even if unchanged
  python -m agent.ingest.ingest_server --folder Meetings  # only one folder

Reads PDF, PPTX, DOCX files from the folders listed in SERVER_FOLDERS.
All content goes into the 'group-wide' Qdrant collection.

Server root is read from SERVER_ROOT env var (default: /ICFO/groups/NOE).
"""
import argparse
import logging
import os
import sys
from pathlib import Path

from .run_ingest import ingest_directory

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

SERVER_ROOT = Path(os.environ.get("SERVER_ROOT", "/ICFO/groups/NOE"))

# Folders to ingest — all go to group-wide. This is an ALLOWLIST: anything not
# listed is skipped. Expanded 2026-07-16 for the full /mnt/noe scan.
# DELIBERATELY EXCLUDED (do NOT add): Fabrication, Personal (privacy — user
# decision 2026-07-16); Data Backup (large archive, low RAG value); ai_agent,
# Pictures, Rendering Files, National Instruments Downloads, .obsidian, Obsidian,
# .TemporaryItems (junk / not documents). Per-file junk (venv, __pycache__,
# .ipynb_checkpoints, "Personal/Sergi/QTM - Copy") is pruned by watcher.yaml.
SERVER_FOLDERS = [
    "Lab_Instruments",
    "Manuscripts",
    "Matlab scripts",   # added 2026-07-16
    "Meetings",
    "Notebook",
    "Notebooks",
    "Papers & Books",
    "Posters",
    "Presentation",
    "Presentations",
    "Projects",
    "Python scripts",   # added 2026-07-16
    "QCoDeS",           # added 2026-07-16
    "QTLab",            # added 2026-07-16
    "Samples",          # added 2026-07-16
    "Scripts",          # added 2026-07-16
    "Setups",           # added 2026-07-16
    "Spectromag",
    "Teaching",         # added 2026-07-16
    "Theses & reports",
]

COLLECTION = "group-wide"


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest NOE server documents into Qdrant")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without writing")
    parser.add_argument("--force", action="store_true", help="Re-index even if unchanged")
    parser.add_argument("--folder", default=None, help="Only process this folder name")
    args = parser.parse_args()

    if not SERVER_ROOT.exists():
        logger.error("Server not mounted: %s", SERVER_ROOT)
        sys.exit(1)

    folders = SERVER_FOLDERS
    if args.folder:
        folders = [f for f in SERVER_FOLDERS if f == args.folder]
        if not folders:
            logger.error("Unknown folder: %s. Valid: %s", args.folder, SERVER_FOLDERS)
            sys.exit(1)

    logger.info("Server root: %s", SERVER_ROOT)
    logger.info("Collection:  %s", COLLECTION)
    logger.info("Folders:     %d", len(folders))

    for folder_name in folders:
        folder_path = SERVER_ROOT / folder_name
        if not folder_path.exists():
            logger.warning("Folder not found, skipping: %s", folder_path)
            continue

        logger.info("=" * 60)
        logger.info("Indexing: %s", folder_name)
        try:
            ingest_directory(
                team=COLLECTION,
                repo_path=folder_path,
                repo_name=f"server/{folder_name}",
                force=args.force,
                dry_run=args.dry_run,
                manifest_db=os.path.join(
                    os.environ.get("AGENT_DATA_DIR", "/opt/qnoe-agent/memory"), "episodic.db"
                ),
            )
        except Exception as exc:
            logger.error("Failed to index %s: %s", folder_name, exc, exc_info=True)

    logger.info("=" * 60)
    logger.info("Server ingestion complete.")


if __name__ == "__main__":
    main()
