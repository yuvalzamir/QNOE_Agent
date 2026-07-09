#!/bin/bash
exec /opt/qnoe-agent/venv/bin/vllm serve /opt/qnoe-agent/models/hermes-3-70b-awq --host 0.0.0.0 --port 8000 --quantization awq_marlin --max-model-len 65536 --kv-cache-dtype fp8 --max-num-seqs 4 --enable-auto-tool-choice --tool-call-parser hermes > /opt/qnoe-agent/logs/vllm.log 2>&1
