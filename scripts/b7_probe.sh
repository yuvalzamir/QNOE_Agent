#!/bin/bash
# B7 sandbox verification probe. Runs as qnoe-ai inside qnoe-b7-test.service,
# which carries the same confinement directives as qnoe-hermes.service.
# Every line in the log must read PASS. Never touches real repo/lab files:
# blocked writes fail with EROFS; if enforcement were broken, only throwaway
# _b7_probe files would be created.

LOG=/opt/qnoe-agent/logs/b7_probe.log
: > "$LOG" || { echo "FATAL: cannot write $LOG"; exit 1; }
say() { echo "$*" | tee -a "$LOG"; }
say "B7 probe start: $(date -Is)"

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

# --- must be BLOCKED (read-only / inaccessible) ---
expect_blocked "create file in repos/"        touch /opt/qnoe-agent/repos/_b7_probe
expect_blocked "write file under repos/"      bash -c 'echo x > /opt/qnoe-agent/repos/_redteam_b7_probe.txt'
expect_blocked "write to /ICFO lab share"     touch /ICFO/groups/NOE/ai_agent/_b7_probe
expect_blocked "write to /mnt/noe lab share"  touch /mnt/noe/ai_agent/_b7_probe
expect_blocked "write to config/"             touch /opt/qnoe-agent/config/_b7_probe
expect_blocked "write to agent/"              touch /opt/qnoe-agent/agent/_b7_probe
expect_blocked "write to /home/yzamir"        touch /home/yzamir/qnoe_server_data/_b7_probe
expect_blocked "read secrets/github_pat"      cat /opt/qnoe-agent/secrets/github_pat
expect_blocked "list secrets/"                ls /opt/qnoe-agent/secrets

# --- must still WORK ---
expect_works "write to memory/"               touch /opt/qnoe-agent/memory/_b7_probe
expect_works "write to logs/"                 bash -c 'echo probe >> /opt/qnoe-agent/logs/_b7_probe'
expect_works "write to hermes/ (state)"       touch /opt/qnoe-agent/hermes/_b7_probe
expect_works "write to /tmp (private)"        touch /tmp/_b7_probe
expect_works "write to \$HOME (.mem0 etc.)"   touch /home/qnoe-ai/_b7_probe
expect_works "read qcodes registry"           head -c 16 /home/yzamir/qnoe_server_data/episodic.db
expect_works "read a repo file"               head -c 16 /opt/qnoe-agent/repos/QTM-CodeBase/README.md
expect_works "read config"                    head -c 16 /opt/qnoe-agent/config/sandbox-policy.yaml
expect_works "llama.cpp endpoint :8000"       curl -sf -o /dev/null http://localhost:8000/v1/models
expect_works "qdrant :6333"                   curl -sf -o /dev/null http://localhost:6333/collections
expect_works "teams.env via LoadCredential"   test -r "${CREDENTIALS_DIRECTORY:-/nonexistent}/teams.env"

rm -f /opt/qnoe-agent/memory/_b7_probe /opt/qnoe-agent/hermes/_b7_probe \
      /opt/qnoe-agent/logs/_b7_probe /home/qnoe-ai/_b7_probe

if [ "$FAILED" -eq 0 ]; then say "B7 probe: ALL PASS"; else say "B7 probe: FAILURES PRESENT"; fi
exit $FAILED
