#!/bin/bash
# QNOE Lab Agent — Hermes Gateway launcher, OpenShell sandbox edition (B7-OS).
# Runs as qnoe-ai via qnoe-hermes-sandbox.service. The confinement contract is
# config/sandbox-policy.yaml; the mount map is config/hermes-sandbox-mounts.json.
# The systemd-sandboxed qnoe-hermes.service remains the rollback:
#   sudo systemctl start qnoe-hermes   (Conflicts= stops this unit)
set -e

source /home/qnoe-ai/.profile
export OPENSHELL_LOCAL_TLS_DIR=/home/qnoe-ai/.local/state/openshell/tls

# Single-Teams-poller guard (belt; the unit's Conflicts= is the suspenders).
# In-container `hermes --replace` cannot see host processes, so it provides
# ZERO cross-boundary protection — this check is load-bearing.
if systemctl is-active --quiet qnoe-hermes.service; then
    echo "FATAL: qnoe-hermes.service is active — two Teams pollers forbidden" >&2
    exit 1
fi

# Remove any stale sandbox from a previous run
openshell sandbox delete qnoe-hermes 2>/dev/null || true

# In-sandbox, ALL egress goes through OpenShell's injected L7 proxy, which
# matches policy endpoints BY HOSTNAME. host.openshell.internal is the
# sandbox's alias for the host (verified Stage 3, 2026-07-14); llama.cpp binds
# 0.0.0.0:8000, Qdrant publishes 0.0.0.0:6333.
HOST_ALIAS="${HOST_ALIAS:-host.openshell.internal}"

# Blocks until the gateway exits => systemd Type=simple.
# Secrets: teams.env arrives as a single-file ro bind (mount map) at
# /run/teams.env (NOT under a subdir — docker auto-creates bind parent dirs
# root-only, unreadable for uid 1001); start_hermes.sh sources it via
# $TEAMS_ENV_FILE. Never pass secrets via --env (leaks through ps/gateway db).
exec openshell sandbox create \
    --name qnoe-hermes \
    --from qnoe-hermes:0.1 \
    --policy /opt/qnoe-agent/config/sandbox-policy.yaml \
    --no-auto-providers \
    --env QDRANT_URL="http://${HOST_ALIAS}:6333" \
    --env VLLM_BASE_URL="http://${HOST_ALIAS}:8000/v1" \
    --env TEAMS_ENV_FILE=/run/teams.env \
    --driver-config-json "$(cat /opt/qnoe-agent/config/hermes-sandbox-mounts.json)" \
    -- /opt/qnoe-agent/scripts/start_hermes.sh
