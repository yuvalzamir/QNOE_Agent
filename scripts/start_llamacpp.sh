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

exec /opt/qnoe-agent/llamacpp/bin/llama-server \
  -m /opt/qnoe-agent/models/gpt-oss-120b-gguf/gpt-oss-120b-mxfp4-00001-of-00003.gguf \
  --alias gpt-oss-120b \
  --host 0.0.0.0 --port 8000 \
  --jinja --chat-template-kwargs '{"reasoning_effort":"low"}' \
  -ngl 999 --flash-attn on -ub 2048 \
  --temp 0.2 --top-p 0.9 \
  -c 262144 --parallel 4 \
  > /opt/qnoe-agent/logs/llamacpp.log 2>&1
