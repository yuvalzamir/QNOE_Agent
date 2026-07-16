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
export EXCLUDE_EXTENSIONS="${EXCLUDE_EXTENSIONS:-}"   # e.g. ".txt" to skip raw measurement text

WORKERS="${1:-12}"
shift || true

exec /opt/qnoe-agent/venv/bin/python -m agent.ingest.parallel_server_ingest \
    --workers "$WORKERS" "$@"
