#!/bin/bash
# B7 confinement verification probe — mechanism-agnostic acceptance test for
# the contract in config/sandbox-policy.yaml. Runs as qnoe-ai inside EITHER:
#   * qnoe-b7-test.service        — systemd namespace (same directives as
#                                   qnoe-hermes.service)
#   * qnoe-b7-sandbox-test.service — OpenShell sandbox (same policy + mounts as
#                                   qnoe-hermes-sandbox.service); endpoints and
#                                   cred delivery arrive via env (B7_LLM_URL,
#                                   B7_QDRANT_URL, TEAMS_ENV_FILE)
# Every line in the log must read PASS. Never touches real repo/lab files:
# blocked writes fail with EROFS (systemd) or path-absent (container — absence
# beats read-only); if enforcement were broken, only throwaway _b7_probe files
# would be created.

B7_LLM_URL="${B7_LLM_URL:-http://localhost:8000}"
B7_QDRANT_URL="${B7_QDRANT_URL:-http://localhost:6333}"

LOG=/opt/qnoe-agent/logs/b7_probe.log
: > "$LOG" || { echo "FATAL: cannot write $LOG"; exit 1; }
say() { echo "$*" | tee -a "$LOG"; }
say "B7 probe start: $(date -Is) (llm=$B7_LLM_URL qdrant=$B7_QDRANT_URL)"

FAILED=0

expect_blocked() {
    local label=$1; shift
    if "$@" >/dev/null 2>&1; then
        say "FAIL: $label SUCCEEDED (should be blocked)"; FAILED=1
    else
        say "PASS: $label blocked"
    fi
}
expect_works() {
    local label=$1; shift
    if "$@" >/dev/null 2>&1; then
        say "PASS: $label works"
    else
        say "FAIL: $label broken (should work)"; FAILED=1
    fi
}

# --- must be BLOCKED (read-only / inaccessible / not mounted) ---
expect_blocked "create file in repos/"        touch /opt/qnoe-agent/repos/_b7_probe
expect_blocked "write file under repos/"      bash -c 'echo x > /opt/qnoe-agent/repos/_redteam_b7_probe.txt'
expect_blocked "write to /ICFO lab share"     touch /ICFO/groups/NOE/ai_agent/_b7_probe
expect_blocked "write to /mnt/noe lab share"  touch /mnt/noe/ai_agent/_b7_probe
expect_blocked "write to config/"             touch /opt/qnoe-agent/config/_b7_probe
expect_blocked "write to scripts/"            touch /opt/qnoe-agent/scripts/_b7_probe
expect_blocked "write to agent/"              touch /opt/qnoe-agent/agent/_b7_probe
expect_blocked "write to /home/yzamir"        touch /home/yzamir/qnoe_server_data/_b7_probe
expect_blocked "read secrets/github_pat"      cat /opt/qnoe-agent/secrets/github_pat
expect_blocked "list secrets/"                ls /opt/qnoe-agent/secrets

# Container-only check: OpenShell's own gateway TLS/JWT keys must be invisible
# to the confined agent (the systemd mechanism cannot hide them — the gateway
# runs as their owner; that residual weakness is one reason for the migration).
if [ -n "${TEAMS_ENV_FILE:-}" ]; then
    expect_blocked "read OpenShell gateway TLS state" \
        cat /home/qnoe-ai/.local/state/openshell/tls/ca.key
fi

# --- must still WORK ---
expect_works "write to memory/"               touch /opt/qnoe-agent/memory/_b7_probe
expect_works "write to logs/"                 bash -c 'echo probe >> /opt/qnoe-agent/logs/_b7_probe'
expect_works "write to hermes/ (state)"       touch /opt/qnoe-agent/hermes/_b7_probe
expect_works "write to /tmp (private)"        touch /tmp/_b7_probe
# $HOME outside .mem0/.cache: systemd = rw (coarse), container = landlock-blocked
# (tighter by design — only the two mounted state dirs are writable).
if [ -n "${TEAMS_ENV_FILE:-}" ]; then
    expect_blocked "write to \$HOME outside .mem0/.cache" touch /home/qnoe-ai/_b7_probe
else
    expect_works "write to \$HOME"            touch /home/qnoe-ai/_b7_probe
fi
expect_works "write to ~/.mem0"               touch /home/qnoe-ai/.mem0/_b7_probe
expect_works "write to ~/.cache"              touch /home/qnoe-ai/.cache/_b7_probe
expect_works "read qcodes registry"           head -c 16 /home/yzamir/qnoe_server_data/episodic.db
expect_works "read a repo file"               head -c 16 /opt/qnoe-agent/repos/QTM-CodeBase/README.md
expect_works "read config"                    head -c 16 /opt/qnoe-agent/config/sandbox-policy.yaml
expect_works "llama.cpp endpoint"             curl -sf -o /dev/null "$B7_LLM_URL/v1/models"
expect_works "qdrant"                         curl -sf -o /dev/null "$B7_QDRANT_URL/collections"
expect_works "teams.env delivered"            bash -c 'test -r "${CREDENTIALS_DIRECTORY:-/nonexistent}/teams.env" -o -r "${TEAMS_ENV_FILE:-/nonexistent}"'

rm -f /opt/qnoe-agent/memory/_b7_probe /opt/qnoe-agent/hermes/_b7_probe \
      /opt/qnoe-agent/logs/_b7_probe /home/qnoe-ai/_b7_probe \
      /home/qnoe-ai/.mem0/_b7_probe /home/qnoe-ai/.cache/_b7_probe

if [ "$FAILED" -eq 0 ]; then say "B7 probe: ALL PASS"; else say "B7 probe: FAILURES PRESENT"; fi
exit $FAILED
