#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# STAGED deploy for Mem0 per-user memory (branch feature/mem0-per-user).
# See MEM0_INTEGRATION.md / decisions D13.
#
# RUN THIS ON THE DGX, by the user, ONLY during a vLLM window
# (i.e. after the SharePoint full sync finishes). It is NOT auto-run.
#
# What it does (reversible, no service restart):
#   1. deploy the edited qnoe_rag plugin  (/tmp -> /opt, chown, chmod)
#   2. set `user_profile_enabled: false` in the 3 profile configs
#      (drops USER.md; KEEPS MEMORY.md — memory_enabled left at default True)
# What it does NOT do (you do these manually, then verify):
#   - start vLLM        (competes with SP sync for memory — start only when safe)
#   - restart the agent (qnoe-hermes.service)
#
# Prereqs already done during validation on 2026-07-08:
#   - mem0ai 2.0.11 installed in hermes-venv
#   - Qdrant collection `episodic_memory` (768-dim) + user_id keyword index
# ---------------------------------------------------------------------------
set -euo pipefail

PLUGIN_SRC="/tmp/qnoe_rag_init.py"   # staged copy of the branch __init__.py
PLUGIN_DST="/opt/qnoe-agent/hermes/plugins/qnoe_rag/__init__.py"
PROFILES=(qnoe-orchestrator qnoe-qtm qnoe-photocurrent)

echo "== 0. sanity =="
if [[ ! -f "$PLUGIN_SRC" ]]; then
  echo "  ERROR: $PLUGIN_SRC missing. Re-stage it first, e.g. from your workstation:"
  echo "    scp hermes/plugins/qnoe_rag/__init__.py yzamir@10.3.8.21:$PLUGIN_SRC"
  exit 1
fi
python3 -c "import ast,sys; ast.parse(open('$PLUGIN_SRC').read()); print('  plugin syntax OK')"
/opt/qnoe-agent/hermes-venv/bin/python -c "import mem0; print('  mem0', mem0.__version__)"
curl -s http://localhost:6333/collections/episodic_memory \
  | python3 -c "import sys,json; d=json.load(sys.stdin)['result']; print('  episodic_memory:', d['status'], d['points_count'],'points')"

echo "== 1. deploy plugin =="
sudo cp "$PLUGIN_SRC" "$PLUGIN_DST"
sudo chown qnoe-ai:qnoe-ai "$PLUGIN_DST"
sudo chmod g+w "$PLUGIN_DST"
echo "  deployed -> $PLUGIN_DST"

echo "== 2. set user_profile_enabled: false in profile configs =="
for p in "${PROFILES[@]}"; do
  CFG="/opt/qnoe-agent/hermes/profiles/$p/config.yaml"
  TMP="/tmp/${p}.config.yaml"
  sudo cat "$CFG" > "$TMP"
  python3 - "$TMP" <<'PYEOF'
import sys
path = sys.argv[1]
text = open(path).read()
if "user_profile_enabled" in text:
    print("  already set; leaving as-is"); sys.exit(0)
out, inserted = [], False
for line in text.splitlines(keepends=True):
    out.append(line)
    if line.strip() == "memory:" and not line[:1].isspace() and not inserted:
        out.append("  user_profile_enabled: false\n")
        inserted = True
if not inserted:
    print("  WARNING: no top-level 'memory:' key; NOT modified"); sys.exit(2)
open(path, "w").writelines(out)
print("  inserted user_profile_enabled: false")
PYEOF
  sudo cp "$TMP" "$CFG"
  sudo chown qnoe-ai:qnoe-ai "$CFG"
  sudo chmod g+w "$CFG"
  echo "  [$p] updated"
  rm -f "$TMP"
done

cat <<'EOM'

== DONE (files + config). MANUAL steps remaining ==
  a) Confirm vLLM model id and update MEM0_LLM_MODEL if needed:
       curl -s http://localhost:8000/v1/models
     (if it is NOT "hermes-3-70b", export MEM0_LLM_MODEL=<id> in the agent's
      systemd env, or edit MEM0_CONFIG in qnoe_rag/__init__.py)
  b) Start vLLM (only when SP sync is done / memory is free):
       sudo systemctl start vllm.service     # ~5 min to load
  c) Restart the agent:
       sudo systemctl restart qnoe-hermes.service
  d) Verify (see MEM0_INTEGRATION.md §9):
       - logs show "Initializing Mem0 (episodic_memory)", no discovery crash
       - send a preference, then check it landed:
           curl -s http://localhost:6333/collections/episodic_memory \
             | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"]["points_count"])'
       - next turn shows "## What I remember about you" in the prompt
       - per-user isolation: user B does NOT see user A's fact
       - tool calls still fire (watch the ~19.5K context cliff)
       - watch for protobuf errors (mem0 downgraded protobuf 7->6)

== ROLLBACK ==
  - fast:  export MEM0_ENABLED=0 in the agent env, restart
  - config: remove the "user_profile_enabled: false" line from the 3 configs
  - code:  git checkout master -- hermes/plugins/qnoe_rag/__init__.py, redeploy
EOM
