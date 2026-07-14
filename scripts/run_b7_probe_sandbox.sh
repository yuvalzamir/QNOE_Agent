#!/bin/bash
# B7-OS acceptance test: run scripts/b7_probe.sh inside a throwaway OpenShell
# sandbox carrying the IDENTICAL policy + mounts + env as the production
# gateway sandbox (keep in sync with start_hermes_sandbox.sh). Exit code =
# probe result; log at /opt/qnoe-agent/logs/b7_probe.log.
# Run as qnoe-ai (via qnoe-b7-sandbox-test.service — no sudo -u on this box).
set -e

source /home/qnoe-ai/.profile
export OPENSHELL_LOCAL_TLS_DIR=/home/qnoe-ai/.local/state/openshell/tls

openshell sandbox delete qnoe-b7-probe 2>/dev/null || true

HOST_ALIAS="${HOST_ALIAS:-host.openshell.internal}"

exec openshell sandbox create \
    --name qnoe-b7-probe \
    --from qnoe-hermes:0.1 \
    --policy /opt/qnoe-agent/config/sandbox-policy.yaml \
    --no-auto-providers \
    --env B7_LLM_URL="http://${HOST_ALIAS}:8000" \
    --env B7_QDRANT_URL="http://${HOST_ALIAS}:6333" \
    --env TEAMS_ENV_FILE=/run/teams.env \
    --driver-config-json "$(cat /opt/qnoe-agent/config/hermes-sandbox-mounts.json)" \
    -- bash /opt/qnoe-agent/scripts/b7_probe.sh
