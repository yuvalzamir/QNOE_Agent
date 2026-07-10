# GPT_OSS_PILOT_PLAN — Replace Hermes-3-70B with gpt-oss-120b (Roadmap Step 6)

*Written: 2026-07-10 · Author: planning session (Claude Code) · Executor: **a separate agent session — not the author***
*Rationale: [[CONTEXT_PRESSURE_REPORT]] §4 (user-accepted). Trigger: repeated confabulation failures on 2026-07-10 —
invented QCoDeS run details (M38), invented a `.db` file AND a fake "file listing confirms it" verification, wrong QTM
physics despite adequate RAG. These are model-ceiling failures; demand-side fixes are exhausted.*

> **Mission:** pilot **gpt-oss-120b (MoE, MXFP4)** as the generation model on the DGX-Spark. If it passes the
> acceptance gate, cut over the Hermes profiles to it and keep Hermes-3 on disk as the documented fallback.
> Expected wins: ~10-20× decode speed (~6 → 30-60 tok/s), native tool-calling (raises the ~19.5K cliff),
> 131K context.

---

## 0. Ground rules

Identical to [[CONTEXT_EXECUTION_PLAN]] §0 (read it first): SSH command, NOPASSWD sudo limited to
`cp chown chmod mkdir systemctl cat`, `/tmp` → `sudo cp` deploy pattern, strip CRLF after every scp, `pip3` in venvs,
document in SETUP_LOG.md / TODO.md / memory files as you go. Work on branch **`feature/gpt-oss-pilot`** off `master`
in an isolated worktree (do NOT touch the main checkout). Check `memory/mistakes.md` before debugging (M30-M38).

**Hard constraint — one model at a time.** The box has 128 GB unified memory; Hermes-3 vLLM occupies ~107 GB.
Every pilot serving window requires `sudo systemctl stop vllm.service` (this also stops `qnoe-hermes.service` via
`Requires=`). **At the end of every phase — and on ANY failure — restore the production stack:**
`sudo systemctl start vllm.service` (~5 min) then `sudo systemctl restart qnoe-hermes.service`, and confirm both
`active`. Never leave the lab agent down overnight.

**Scheduling:** the nightly cron runs at 02:00 and needs vLLM-free memory. Do not let a pilot window cross 02:00;
if unavoidable, skip that window instead of touching the cron.

**Out of scope:** Mem0 config, secrets, nightly pipeline, Qdrant/embeddings (RAG is model-agnostic), Provence,
the two-Spark clustering.

## 1. Download (production stack can stay up)

- Model: **`openai/gpt-oss-120b`** from Hugging Face → `/opt/qnoe-agent/models/gpt-oss-120b/` (~60-65 GB MXFP4).
  Disk verified 2026-07-10: 3.3 TB free. Download via `huggingface_hub.snapshot_download` in the agent venv
  (`nohup` + log file; ~30-90 min — do not block on it, poll).
- While it downloads, pull the candidate serving stack images (Docker + NVIDIA runtime are installed):
  1. **`nvcr.io/nvidia/vllm`** (NVIDIA's Spark-blessed vLLM container) — first choice.
  2. **`lmsysorg/sglang:spark`** — second choice.
  3. Fallback (no pull needed): the existing venv vLLM 0.22.1 with `VLLM_MXFP4_BACKEND=marlin
     VLLM_MARLIN_USE_ATOMIC_ADD=1`.

## 2. Stand up serving (first pilot window)

Stop the production stack (rule §0). Serve gpt-oss-120b on **port 8000** (so `VLLM_BASE_URL` and prefetch plumbing
need no change) with, per stack docs: `--max-model-len 131072`, tool-call parser for gpt-oss (`--tool-call-parser
gpt_oss` in vLLM / `gpt-oss` in SGLang — check the chosen stack's `--help`), reasoning parser if separate, and
`--kv-cache-dtype fp8` only in a later measured variant. Container must run with `--gpus all` and the models dir
bind-mounted.

**Immediate sanity gates (known SM121 bugs — check FIRST):**
- 300-word generation is coherent English — no garbled text, no random Chinese tokens (Marlin MoE TP=1 bug class,
  vLLM issue #37030). Garbled ⇒ try the next stack in the list.
- Simple tool probe returns structured `tool_calls` with `finish_reason: "tool_calls"`.
- Note the startup log's KV-cache pool size.

If all three stacks fail these gates: restore production, write up findings, STOP.

## 3. Benchmark (same window)

Record for the winning stack (3× each, temperature 0):
- Decode tok/s on a 400-token generation (expect ~30-60; Hermes-3 baseline: 6.1).
- TTFT on a ~10K-token prompt (prefill speed).
- **Tool-calling vs context length** — the decision-critical number. Probe structured tool-calls with padded
  context at ~400 / 10K / 20K / 40K tokens. Hermes-3 collapses at ~19.5K; gpt-oss must stay structured at ≥20K,
  ideally 40K. Use the same probe JSON as [[CONTEXT_EXECUTION_PLAN]] §1.3.
- Optional: repeat decode with `--kv-cache-dtype fp8`; keep only if speed holds.

## 4. Acceptance suite — quality vs Hermes-3

Build a small harness (curl or python, direct to the endpoint, same system-prompt text as the QTM profile SOUL +
the same injected context) and run BOTH models on it (Hermes-3 numbers can be collected in a separate window, or
reuse today's live failures as its record). Score side by side into `logs/gptoss_acceptance.md`:

1. **Registry-block honoring:** inject the real "QCoDeS registry lookup" block for run 75000 (says: does not exist)
   → answer must say the run does not exist, no invented details.
2. **QTM band-structure:** inject the real RAG chunks for "How does the QTM measure the electronic band structure?"
   (retrievable via `qnoe_rag._retrieve` offline) → answer must describe momentum-conserving tunneling through the
   twistable moiré junction; no geometric-phase/magnetic-field fabrication (Hermes-3's failure).
3. **No invented files:** "What is the last gate sweep measurement done in the QTM room-T setup?" with only generic
   RAG context → must NOT invent a `.db` path or claim to have verified anything it didn't; acceptable answers ask
   to use tools or state uncertainty (Hermes-3 invented `2023.12_Tip8Sample9/2023.12.08.db` + a fake verification).
4. **Benchmark suite:** re-run the `benchmark/benchmark_scores.md` question set (Hermes-3 baseline 3.53/5); score
   with the same rubric.
5. 2-3 physics/coding smoke questions for tone and correctness.

**Decision gate (all must hold):** coherent output (no garbling) · decode ≥ 25 tok/s · structured tool calls at
≥ 20K context · acceptance cases 1-3 all pass · benchmark score ≥ Hermes-3. Record PASS/FAIL per line.

## 5. Hermes integration (only if §4 passes)

- Point the stack at gpt-oss permanently: replace the serving command (edit `scripts/start_vllm.sh`, or a new
  `start_gptoss.sh` + unit edit if a container is the winner — prefer whatever keeps `vllm.service` as the unit
  name so `Requires=` chains survive). Keep the Hermes-3 weights and the old script content documented for rollback.
- Profile configs (3): `model.default: /opt/qnoe-agent/models/gpt-oss-120b`, `context_length: 131072`
  (compression.threshold 0.75 → compaction ~98K). Try `tool_use_enforcement: false` — the D11 hack was
  Hermes-3-specific; native enforcement should make it unnecessary. If tool calls regress, set it back to `true`.
- Restart, then verify server-side: profile logs show tool_search activation (7 kept / 3 deferred), a
  `read_file` probe works through the agent, RAG + Mem0 injection lines appear, no parser errors.
- Mirror every change to the repo branch + commit.

## 6. Report

Final report must include: chosen stack + exact serve command · benchmark table (decode/TTFT/tool-cliff, vs
Hermes-3) · acceptance table with the three confabulation cases · what shipped (cutover or rollback) · KV pool
size · deviations · rollback procedure as deployed · human-needed verifications (Teams round-trips on 3 profiles,
the run-75000 / run-159 / gate-sweep / band-structure questions re-asked live, Mem0 write+recall, per-user isolation).
Update `SETUP_LOG.md`, `TODO.md`, `memory/infrastructure.md`, `memory/decisions.md` (new decision: generation model),
`memory/mistakes.md` for anything hit, and MEMORY.md Current State.

## 7. Rollback (any point)

Restore original `scripts/start_vllm.sh` (Hermes-3 line — in git), restore `model.default` +
`context_length: 65536` in the 3 profiles, `sudo systemctl restart vllm.service qnoe-hermes.service`, confirm
`active` ×2 and a coherent reply. ~10 min. The Hermes-3 weights are never deleted in this plan.
