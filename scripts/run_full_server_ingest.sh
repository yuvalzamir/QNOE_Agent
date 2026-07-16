#!/bin/bash
# Full server ingest launcher — reads /mnt/noe (broad cred), STORES /ICFO paths,
# parallel + resumable. See FULL_SERVER_INGEST_PLAN.md.
#
# PRECONDITION (do first, as admin): free the box for embedding/Docling —
#     sudo systemctl stop vllm.service
# Then run this as yzamir. Safe to re-run after any interruption (resumes).
#
# Usage: bash run_full_server_ingest.sh [workers] [extra args e.g. --dry-run]
#   EXCLUDE_EXTENSIONS=.txt bash run_full_server_ingest.sh 12   # skip raw .txt
set -euo pipefail

export INGEST_READ_ROOT=/mnt/noe
export INGEST_STORE_ROOT=/ICFO/groups/NOE
export SERVER_ROOT=/mnt/noe
export AGENT_DATA_DIR=/home/yzamir/qnoe_server_data
export QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
export FASTEMBED_CACHE_PATH=/opt/qnoe-agent/memory/fastembed_cache
export EMBED_MODEL_PATH=/opt/qnoe-agent/models/nomic-embed
export TRANSFORMERS_OFFLINE=1
export HF_HUB_OFFLINE=1
export PYTHONPATH=/opt/qnoe-agent
export EXCLUDE_EXTENSIONS="${EXCLUDE_EXTENSIONS:-.txt}"   # skip raw-measurement .txt (user decision 2026-07-16); override with EXCLUDE_EXTENSIONS="" to include
# Notebook stays /ICFO-scoped: its 77 ACL-locked per-person subfolders are
# deliberately private and must NOT be recovered via the broad /mnt/noe mount
# (user decision 2026-07-16). The /ICFO nightly still indexes Notebook's open part.
export EXCLUDE_FOLDERS="${EXCLUDE_FOLDERS:-Notebook}"

# --- memory-safety (a persistent worker ballooned to 31GB on Docling -> OOM) ---
export DOCLING_MAX_FILE_BYTES="${DOCLING_MAX_FILE_BYTES:-26214400}"  # 25MB — skip explosion-prone huge PDF/PPTX
export WORKERS="${WORKERS:-4}"        # max concurrent batch subprocesses (semaphore)
export BATCH_SIZE="${BATCH_SIZE:-40}" # files per subprocess — exits+frees Docling memory every 40 files
export MIN_FREE_GB="${MIN_FREE_GB:-50}"  # do not launch a new batch below this free RAM (big headroom)

# optional positional arg overrides the WORKERS env
if [ -n "${1:-}" ]; then WORKERS="$1"; shift || true; fi

exec /opt/qnoe-agent/venv/bin/python -m agent.ingest.parallel_server_ingest \
    --workers "$WORKERS" "$@"
