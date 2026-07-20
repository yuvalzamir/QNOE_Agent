#!/bin/bash
# QNOE Lab Agent — llama.cpp inference server for gpt-oss-120b (MXFP4 GGUF)
# Replaces the vLLM/Hermes-3 serving stack (cutover 2026-07-10).
# Launched by systemd unit vllm.service (name kept for the Requires= chain).
# Rollback: point vllm.service ExecStart back to scripts/start_vllm.sh.
#
# KV config: -c 262144 --parallel 4 WITHOUT --kv-unified => 4 fixed slots x 65536
# (64K) each = 262144-token KV pool, guaranteeing >=4 concurrent users at 64K.
# --kv-unified was dropped intentionally: with it, per-slot ctx is set to the full
# -c and then capped to the model's 131072 train context, yielding a shared 128K
# pool (only ~32K/user at 4 concurrent). Non-unified delivers the required 4x64K.
# Measured 2026-07-10: 48 GB RAM available while serving, decode 46.6 tok/s.

export LD_LIBRARY_PATH=/opt/qnoe-agent/llamacpp/bin:${LD_LIBRARY_PATH}

# Reasoning effort is baked server-side (llama.cpp ignores per-request
# reasoning_effort). Default low = the production agent setting. Override for a
# bounded window (e.g. the Cognee cognify pilot needs high) via the systemd
# manager env — no file edit, survives nothing, easy to revert:
#   sudo systemctl set-environment LLAMA_REASONING_EFFORT=high
#   sudo systemctl restart vllm.service
#   ... high-effort work ...
#   sudo systemctl unset-environment LLAMA_REASONING_EFFORT
#   sudo systemctl restart vllm.service
# NOTE: while set, ALL traffic (Teams agent included) runs at that effort.
EFFORT="${LLAMA_REASONING_EFFORT:-low}"

exec /opt/qnoe-agent/llamacpp/bin/llama-server \
  -m /opt/qnoe-agent/models/gpt-oss-120b-gguf/gpt-oss-120b-mxfp4-00001-of-00003.gguf \
  --alias gpt-oss-120b \
  --host 0.0.0.0 --port 8000 \
  --jinja --chat-template-kwargs "{\"reasoning_effort\":\"${EFFORT}\"}" \
  -ngl 999 --flash-attn on -ub 2048 \
  --temp 0.2 --top-p 0.9 \
  -c 262144 --parallel 4 \
  > /opt/qnoe-agent/logs/llamacpp.log 2>&1
