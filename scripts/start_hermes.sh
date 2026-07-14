#!/bin/bash
set -e

# QNOE Lab Agent — Hermes Gateway launcher
# Runs as qnoe-ai user via systemd (no Docker needed)

# OpenShell forces HOME=/sandbox (its workspace convention) on sandboxed
# processes, overriding /etc/passwd. Our persistent state binds live under the
# real home — restore it so ~/.mem0 and ~/.cache resolve to the mounted dirs
# (found live: "RAG prefetch failed: Permission denied: '/sandbox'" — Mem0
# tried to mkdir /sandbox/...).
if [ "${OPENSHELL_SANDBOX:-}" = "1" ]; then
    export HOME=/home/qnoe-ai
fi

# Hermes home — profiles, plugins, config, memory
export HERMES_HOME=/opt/qnoe-agent/hermes
# BM25 sparse-model cache — must live under a persistent rw path, NOT /tmp
# (R9: PrivateTmp/tmpfs wiped it → RAG down, prefetch hooks crashed)
export FASTEMBED_CACHE_PATH=/opt/qnoe-agent/memory/fastembed_cache

# Model paths
export EMBED_MODEL_PATH=/opt/qnoe-agent/models/nomic-embed
export RERANK_MODEL_PATH=/opt/qnoe-agent/models/cross-encoder-msmarco

# Infrastructure endpoints — default-only so sandbox launchers can override
# (in-container "localhost" is the container; the OpenShell sandbox passes the
# docker bridge gateway IP via --env, see start_hermes_sandbox.sh)
export QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
export VLLM_BASE_URL="${VLLM_BASE_URL:-http://localhost:8000/v1}"
# Mem0 fact-extraction LLM — must match the vLLM served model id exactly
export MEM0_LLM_MODEL=gpt-oss-120b

# Data paths
export AGENT_DATA_DIR=/home/yzamir/qnoe_server_data

# Offline mode for transformers (models are local)
export TRANSFORMERS_OFFLINE=1
export HF_HUB_OFFLINE=1
export HF_DATASETS_OFFLINE=1

# ── Access control (enforced by the gateway; UX by hermes/plugins/qnoe_authz) ─
# GATEWAY_ALLOWED_USERS = permanent members ("floor") — always allowed, never
# lockable. Everyone else is approved dynamically via the notify-and-approve
# flow (native pairing store, teams_polling-approved.json): an unknown user's
# first DM posts an access request to the Agent Logs channel, and an admin runs
# the /approve <id> Teams DM command. GATEWAY_ALLOW_ALL_USERS must stay false or
# the allowlist is bypassed.
#   Floor: Yuval Zamir, Frank Koppens, Alexander Rothstein
export GATEWAY_ALLOW_ALL_USERS=false
export GATEWAY_ALLOWED_USERS="862ec907-3e65-4c00-aa0c-02948656ae7f,1ce94aba-44e9-43ce-863d-42ff77cc277c,ef6f38c9-f873-4cc8-bbf3-e43cb69d8a16"
# Admins who may run /pending /approve /deny /revoke (subset of the floor):
#   Yuval Zamir, Frank Koppens
export QNOE_ADMIN_USER_IDS="862ec907-3e65-4c00-aa0c-02948656ae7f,1ce94aba-44e9-43ce-863d-42ff77cc277c"
# Agent Logs channel (non-secret IDs) — qnoe_authz posts access requests here.
# secrets/report.env is InaccessiblePaths under the B7 sandbox, so pass via env.
export REPORT_TEAM_ID="2d85892e-d22b-41d1-ac92-4a54f60512c9"
export REPORT_CHANNEL_ID="19:ea61d54cf6aa40569022a334bc005c9a@thread.tacv2"

# Home channel for cron job delivery and cross-platform messages (Yuval's DM)
export TEAMS_POLLING_HOME_CHANNEL="19:862ec907-3e65-4c00-aa0c-02948656ae7f_aa2b5ee6-797a-4d95-9cf2-485c04f3958e@unq.gbl.spaces"

# Load Teams credentials — one chain, three delivery mechanisms:
#   1. systemd B7 unit: secrets/ is InaccessiblePaths=, teams.env arrives via
#      LoadCredential= ($CREDENTIALS_DIRECTORY)
#   2. OpenShell sandbox: secrets/ is not mounted at all, teams.env arrives as
#      a single-file ro bind whose path is passed in $TEAMS_ENV_FILE
#   3. bare rollback unit: direct read from secrets/
if [ -n "${CREDENTIALS_DIRECTORY:-}" ] && [ -r "${CREDENTIALS_DIRECTORY}/teams.env" ]; then
    set -a
    source "${CREDENTIALS_DIRECTORY}/teams.env"
    set +a
elif [ -n "${TEAMS_ENV_FILE:-}" ] && [ -r "${TEAMS_ENV_FILE}" ]; then
    set -a
    source "${TEAMS_ENV_FILE}"
    set +a
elif [ -r /opt/qnoe-agent/secrets/teams.env ]; then
    set -a
    source /opt/qnoe-agent/secrets/teams.env
    set +a
fi

# Launch Hermes gateway (uses active profile: qnoe-orchestrator)
exec /opt/qnoe-agent/hermes-venv/bin/hermes gateway run --replace -v
