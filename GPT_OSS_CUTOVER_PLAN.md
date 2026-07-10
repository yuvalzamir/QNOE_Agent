# GPT_OSS_CUTOVER_PLAN — Make gpt-oss-120b (llama.cpp) the production model

*Written: 2026-07-10 · Author: planning session · Executor: **a separate agent session — not the author***
*Basis: pilot PASSED all gates 2026-07-10 (see MEMORY.md + `/tmp/probe_gptoss.md`, `/tmp/accept_gptoss.md` on the DGX):
decode 48.8 tok/s (8.4× Hermes-3), structured tool calls at 151→32,305 prompt tokens, all three confabulation
acceptance cases passed. User approved cutover + raising context. Hermes-3 stays on disk as the documented fallback.*

## 0. Ground rules

Inherit [[CONTEXT_EXECUTION_PLAN]] §0 verbatim (SSH, NOPASSWD sudo = `cp chown chmod mkdir systemctl cat`,
`/tmp` → `sudo cp` deploy, CRLF strip, `pip3`, documentation duties). Branch **`feature/gpt-oss-cutover`** off
`master` in a NEW isolated worktree (existing worktrees `AI_Student-cp`, `AI_Student-gptoss` — do not touch).
Never finish with production down: on any failure execute §7 rollback and verify `active` ×2.
Do not cross the 02:00 nightly cron with a downtime window. Check `memory/mistakes.md` M37-M41 first.

**Community-validated facts to rely on (researched 2026-07-10):**
- Spark llama.cpp guidance (LMSYS "Optimizing GPT-OSS on DGX Spark", corti.com practical guide, ggml discussion
  #16578): `--flash-attn on`, `-ub 2048`, `--kv-unified`, full GPU offload (`-ngl 999`), no CPU-MoE flags,
  ~50 tok/s expected (we measured 48.8), consider `--no-mmap` only if load time regresses (our mmap load was 57 s — fine).
- Empty-content behavior is a known gpt-oss trait (HF gpt-oss-120b discussion #67): reasoning eats `max_tokens`;
  fix = server-level `--chat-template-kwargs '{"reasoning_effort":"low"}'` + generous per-request budgets.
- Multi-user reference (dendro-logic Spark recipe, vLLM-based): ~24 tok/s per user at 5 concurrent — batching
  scales well on this box; 3-4 users is comfortable.

## 1. Stage artifacts (production stays up)

The pilot ran everything from `/home/yzamir/` which `qnoe-ai` (the service user) cannot read. Move into place:

1. `sudo cp -r /home/yzamir/gpt-oss-120b-gguf /opt/qnoe-agent/models/gpt-oss-120b-gguf` + chown -R qnoe-ai:qnoe-ai
   (3 GGUF shards, ~60 GB; verify sizes match the source).
2. `sudo mkdir -p /opt/qnoe-agent/llamacpp && sudo cp -r /home/yzamir/llama.cpp/build/bin /opt/qnoe-agent/llamacpp/`
   + chown -R (contains `llama-server` + its `.so` libs; verify `ldd` resolves from the new location, else copy the
   missing libs or set `LD_LIBRARY_PATH` in the launch script).
3. Write the new launch script to `scripts/start_llamacpp.sh` (repo) and deploy to
   `/opt/qnoe-agent/scripts/start_llamacpp.sh` (chmod 755, qnoe-ai). Baseline content:

```bash
#!/bin/bash
exec /opt/qnoe-agent/llamacpp/bin/llama-server \
  -m /opt/qnoe-agent/models/gpt-oss-120b-gguf/gpt-oss-120b-mxfp4-00001-of-00003.gguf \
  --alias gpt-oss-120b \
  --host 0.0.0.0 --port 8000 \
  --jinja --chat-template-kwargs '{"reasoning_effort":"low"}' \
  -ngl 999 --flash-attn on -ub 2048 --kv-unified \
  -c 262144 --parallel 4 \
  > /opt/qnoe-agent/logs/llamacpp.log 2>&1
```

`-c 262144 --parallel 4` = **4 slots × 64K each** (the context raise the user approved, sized for the ≥3-user
requirement). This is the first thing the serving window validates — see §3.

## 2. Serving window prep

Announce downtime start in the report log. `sudo systemctl stop qnoe-hermes.service vllm.service`. Verify
`free -g` available ≥110 GB. Arm the memory watchdog (pattern from M41): kill `llama-server` if available < 8 GB,
5 s poll, log to `/opt/qnoe-agent/logs/llamacpp_watchdog.log`. Run it for the whole window.

## 3. Boot + size the context (measured, stepped)

Launch via the new script (as qnoe-ai is not required for the test boot; final run is via systemd). Read the log:
llama.cpp prints the **KV cache allocation**. Decision ladder — use the largest config that boots with
**≥20 GB available** while serving:

1. `-c 262144 --parallel 4` (64K/slot) — try first.
2. `-c 131072 --parallel 4` (32K/slot) — fallback.
3. `-c 98304 --parallel 3` (32K/slot) — minimum acceptable (3 users).

Record: KV size, available RAM while serving, load time. If even (3) fails, rollback + STOP.

## 4. Validation gates (all against the running server)

1. **Health + id:** `/health` ok; `/v1/models` reports `gpt-oss-120b` (the `--alias`).
2. **Coherence + speed:** 300-word physics answer, temp 0 — coherent, ≥40 tok/s decode.
3. **Reasoning budget:** with `reasoning_effort: low` baked in, a simple question with `max_tokens: 400` must
   return non-empty content (this was the empty-content failure mode at default effort).
4. **Tool calls:** structured `tool_calls` probes at ~400 / 16K / 32K prompt tokens (reuse `/tmp/pilot_probe.py`).
5. **Concurrency:** 3 simultaneous 300-token generations (background curls) — all complete, record per-stream tok/s
   (expect ~25-40 each per community + batching).
6. **Acceptance spot-check:** rerun `/tmp/accept_run.py` cases 1-3 (contexts in `/tmp/accept_ctx.json`) — case 1
   must say run 75000 does not exist; case 3 must not invent a `.db`.

## 5. Wire the production stack to it

1. Point the service at llama.cpp **keeping the unit name** (preserves the `Requires=` chain and every doc/runbook):
   copy current `/etc/systemd/system/vllm.service` to `/tmp`, change `ExecStart=` to
   `/opt/qnoe-agent/scripts/start_llamacpp.sh`, update `Description=` to mention llama.cpp/gpt-oss, `sudo cp` back,
   `sudo systemctl daemon-reload`. (Old Hermes-3 line lives on in git + `scripts/start_vllm.sh` untouched — that IS
   the rollback.)
2. Hermes profile configs ×3 (`/tmp` + `sudo cp` deploy):
   - `model.default: gpt-oss-120b` (matches the alias)
   - `context_length:` = the per-slot size from §3 (65536 or 32768)
   - `agent.tool_use_enforcement: false` (D11 was a Hermes-3 hack; native enforcement expected — if the tool-call
     verification in §6 fails, set back to `true` and note it)
3. `scripts/start_hermes.sh` on the DGX: `MEM0_LLM_MODEL=gpt-oss-120b` (Mem0's fact-extraction LLM must match the
   served id).
4. `sudo systemctl start vllm.service` (now llama.cpp) → wait healthy → `sudo systemctl restart qnoe-hermes.service`.

## 6. End-to-end verification (server-side)

- Profile logs: tool_search activation (7 kept / 3 deferred), `QnoeRag initialized`, `Initializing Mem0`, no
  parser/template errors.
- Force a `read_file` through the agent (hermes CLI if usable, else document as human check).
- QCoDeS registry hook fires: a "run 75000" query through the agent must produce the "does not exist" answer.
- Watch one full agent turn in the log for empty-content symptoms (if the agent shows blank replies, raise
  Hermes `max_tokens` in profile configs from 4096 → 8192 and restart — reasoning shares the output budget).
- Record final: decode tok/s via agent, memory while serving, KV config.

## 7. Rollback (any point, ~10 min)

`/etc/systemd/system/vllm.service` ExecStart back to `/opt/qnoe-agent/scripts/start_vllm.sh` (file untouched);
daemon-reload; revert the 3 profile configs + `MEM0_LLM_MODEL` (git has all originals);
`sudo systemctl restart vllm.service` (Hermes-3, ~5 min) → `qnoe-hermes.service`; verify `active` ×2 + coherent reply.

## 8. Report + documentation

Final report: chosen context/parallel config + measured KV and memory; validation table (§4 gates 1-6);
concurrency numbers; what shipped; deviations; rollback state; human-needed Teams verifications (round-trips ×3
profiles, run-75000, run-159, gate-sweep, band-structure, Mem0 recall + isolation — all should now be re-asked
against gpt-oss). Update `SETUP_LOG.md`, `TODO.md` (step 6 → done/cutover), `memory/infrastructure.md` (new serving
stack section), `memory/decisions.md` (supersede D14: cutover executed), `memory/agent-code.md` (model id, enforcement
flag outcome), MEMORY.md Current State. Commit + push the branch; do NOT merge to master — the user merges after the
human Teams verifications pass.
