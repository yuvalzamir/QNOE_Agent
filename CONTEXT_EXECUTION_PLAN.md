# CONTEXT_EXECUTION_PLAN — Context-Pressure Package, Steps 1-3

*Written: 2026-07-09 · Author: planning session (Claude Code) · Executor: **a separate agent session — not the author***
*Source of truth for rationale: [[CONTEXT_PRESSURE_REPORT]] §6 (accepted roadmap) and the inline **→ Answer/Decision** blocks there.*

> **Mission:** execute roadmap steps 1-3 on the DGX: (1) vLLM 64K + fp8 KV + max-num-seqs 4, (2) tool-schema
> slimming via toolset composition, (3) Provence reranker swap. Each step has a verification gate and a rollback.
> Do the steps **in order**; do not start a step until the previous step's gate passes.

---

## 0. Ground rules for the executing agent

- **SSH:** `ssh -i "/c/Users/yzamir/.ssh/id_ed25519_dgx" -o StrictHostKeyChecking=no yzamir@10.3.8.21 "command"`.
  Ask the user once for SSH approval at session start (per CLAUDE.md), then proceed.
- **File ownership on DGX:** everything under `/opt/qnoe-agent/` is owned by `qnoe-ai`. Deploy pattern:
  write to `/tmp/…` → `sudo cp` into place → `sudo chown qnoe-ai:qnoe-ai <file>` → `sudo chmod g+w <file>` (scripts need `+x`).
- **NOPASSWD sudo covers ONLY:** `cp`, `chown`, `chmod`, `mkdir`, `systemctl`, `cat`. Anything else needing sudo
  (e.g. editing with `sudo tee`, `sudo grep`) will hang — use the `/tmp` + `sudo cp` pattern or `sudo cat | grep`.
  If a genuinely interactive sudo command is unavoidable, hand it to the user to run and paste back output.
- **Files scp'd from the Windows workstation carry CRLF.** Always strip after copying: `sed -i 's/\r$//' /tmp/<file>`.
- **In the agent venvs use `pip3`, not `pip`.**
- **Repo hygiene:** the DGX is the live system; `Z:\code\AI_Student` is the mirror. After each step, apply the same
  change to the repo copy (`hermes/profiles/…`, `hermes/plugins/…`, `scripts/…` as relevant) and commit with a
  conventional message (`feat(context): …`). Push to `master` on https://github.com/yuvalzamir/QNOE_Agent.git.
- **Documentation duty (per CLAUDE.md):** as each step completes, tick the matching box in `TODO.md`
  ("ACCEPTED — Context-pressure package"), append a dated entry to `SETUP_LOG.md`, and record any bug you hit in
  `memory/mistakes.md`. At the end, write the results summary described in §5.
- **OUT OF SCOPE — do not touch:** the Mem0 deploy (`deploy_mem0.sh`, pending separately in TODO.md — but see the
  note in §1.6: the user may choose to fold it into the same restart window), re-enabling the nightly cron, the
  nightly SharePoint task, the gpt-oss-120b pilot (step 6), and prefix-caching/cliff work (steps 4-5).

## 0.1 Preconditions — verify before starting

| Check | Command | Expected |
|---|---|---|
| BM25 backfill complete | `sqlite3 /opt/qnoe-agent/memory/episodic.db "SELECT collection FROM sparse_backfill WHERE completed_at IS NULL"` | 0 rows *(verified complete 2026-07-09 ~12:04 UTC)* |
| vLLM currently stopped | `curl -s --max-time 2 http://localhost:8000/v1/models` | connection refused |
| Agent service running | `systemctl is-active qnoe-hermes.service` | `active` |
| No heavy job eating RAM | `free -g` | ≥ ~110 GB free (vLLM needs ~70 GB weights + KV pool) |

---

## 1. Step 1 — vLLM: 64K window, fp8 KV, max-num-seqs 4

**Goal:** window 32K → 64K (Hermes compaction fires at ~48K instead of ~24K) and ≥3-concurrent-user KV capacity.
**Requirement (user):** system must serve **≥3 concurrent users** → fp8 KV is the enabler (pool ≈ 256K KV tokens vs ~128K at fp16).

### 1.1 Edit the launch script

Current `/opt/qnoe-agent/scripts/start_vllm.sh` (single line, verified 2026-07-09):

```bash
#!/bin/bash
exec /opt/qnoe-agent/venv/bin/vllm serve /opt/qnoe-agent/models/hermes-3-70b-awq --host 0.0.0.0 --port 8000 --quantization awq_marlin --max-model-len 32768 --enable-auto-tool-choice --tool-call-parser hermes
```

New version (change `--max-model-len`, add two flags):

```bash
#!/bin/bash
exec /opt/qnoe-agent/venv/bin/vllm serve /opt/qnoe-agent/models/hermes-3-70b-awq --host 0.0.0.0 --port 8000 --quantization awq_marlin --max-model-len 65536 --kv-cache-dtype fp8 --max-num-seqs 4 --enable-auto-tool-choice --tool-call-parser hermes
```

Deploy: write to `/tmp/start_vllm.sh`, strip CRLF, `sudo cp` over the original, `sudo chown qnoe-ai:qnoe-ai`,
`sudo chmod 755`. (The systemd unit `/etc/systemd/system/vllm.service` just execs this script — **no unit edit, no
daemon-reload needed**.)

### 1.2 Start and confirm load

```bash
sudo systemctl start vllm.service        # ~5 min model load; TimeoutStartSec=600
journalctl is NOT in NOPASSWD — follow the log via:  tail -f /opt/qnoe-agent/logs/vllm*.log  (or sudo cat)
curl -s http://localhost:8000/v1/models  # note the served model id for the benchmark payloads
```

In the startup log confirm: `max_model_len=65536`, KV cache dtype fp8, and note the reported
"GPU KV cache size: N tokens" figure — **record it**; expect roughly ~256K-token order of magnitude.

### 1.3 Benchmark A — fp8 config

Run each probe 3× and average (temperature 0, fixed prompts):

1. **Decode speed:** `time curl -s http://localhost:8000/v1/chat/completions -d '{"model":"<id>","messages":[{"role":"user","content":"Write exactly 300 words about graphene."}],"max_tokens":400,"temperature":0}' -H 'Content-Type: application/json'`
   → tok/s = `usage.completion_tokens ÷ wall-seconds` (subtract TTFT if you want to be careful; consistency across A/B matters more than absolute value).
2. **Tool-call sanity (small context):** same endpoint with a minimal `tools` array (one `read_file`-style function)
   and a prompt that forces a call ("Read the file /etc/hostname"). Expect `finish_reason: "tool_calls"` with valid
   structured `tool_calls`. This has historically worked at 359 tokens — it must still work.
3. **Quality smoke:** 2-3 short physics/QNOE questions; eyeball for garbled output (fp8 KV should be near-lossless,
   but SM121 kernels are the open question — garbled text = automatic fail).

### 1.4 Benchmark B — fp16 comparison

Remove **only** `--kv-cache-dtype fp8` from the script (keep 65536 and max-num-seqs 4), `sudo systemctl restart
vllm.service`, wait for load, repeat §1.3.

**Decision gate:** keep fp8 iff (a) decode tok/s ≥ ~90% of fp16, AND (b) tool-call probe passes, AND (c) no garbled
output. Otherwise ship the fp16 variant and **flag in the report that the 3-user guarantee drops to ~2 concurrent
full-64K sessions** (still ≥3 for typical non-simultaneous use). Whichever wins, leave that script in place and
restart vLLM into the final config.

### 1.5 Raise the Hermes window

In all three profile configs on the DGX — `/opt/qnoe-agent/hermes/profiles/{qnoe-orchestrator,qnoe-qtm,qnoe-photocurrent}/config.yaml` —
change `context_length: 32768` → `context_length: 65536` (leave `compression.threshold: 0.75` → compaction @ ~48K).
Deploy via `/tmp` + `sudo cp` (configs are `qnoe-ai`-owned). Then:

```bash
sudo systemctl restart qnoe-hermes.service
```

### 1.6 Verify & sync

- Startup log clean (duplicate-adapter ERRORs are known harmless noise); send a Teams test message; confirm a reply.
- Mirror both changes (start_vllm.sh flags, 3× context_length) into the repo and commit.
- **Note for the user:** the Mem0 deploy (TODO.md, pending) was waiting for exactly this vLLM window. Tell the user
  vLLM is back up so they can decide whether to run it now — do **not** run it yourself.

**Rollback:** restore `--max-model-len 32768`, remove the two new flags, `sudo systemctl restart vllm.service`;
revert `context_length` in the 3 configs; restart agent. Total ~10 min.

---

## 2. Step 2 — Tool-schema slimming via toolset composition

**Goal:** resident tool schemas ~6.4K → ~3.7K tokens/turn. **Do NOT attempt this via Tool Search** — verified in the
Hermes v0.17.0 source (`tools/tool_search.py`): tools in `toolsets._HERMES_CORE_TOOLS` are *never* deferred, and all
12 currently-resident tools are core. The lever is the profile `toolsets:` list, which feeds
`get_tool_definitions(enabled_toolsets=…)` and supports narrow built-in toolsets.

### 2.1 The change

In all three profile configs (same files as §1.5), replace:

```yaml
toolsets:
- hermes-cli
- qnoe-lab
```

with:

```yaml
toolsets:
- file        # read_file, write_file, patch, search_files  (~1,478 tok)
- terminal    # terminal, process                            (~1,737 tok)
- clarify     # clarify                                      (~490 tok)
- qnoe-lab    # QCoDeS plugin tools — deferrable behind tool_search
```

Keep `disabled_toolsets` and `tools.tool_search.enabled: 'on'` exactly as they are. This intentionally drops the
core `skills`, `memory`, `execute_code` toolsets (`memory` the *tool* ≠ the `qnoe_rag` memory *provider*, which is
configured separately under `memory.provider` and is untouched). **Note: removed = invisible** — tools excluded by
composition never enter the Tool Search catalog (it is rebuilt from the enabled tool-defs list each assembly), so
dropped toolsets cannot be found or called at all. If a dropped capability is missed later, the fallback is a thin
plugin wrapper (plugin tools are non-core → deferrable → discoverable via `tool_search` at ~zero resident cost).
This is also the intended path for the L4 skill registry when it lands: one compact `run_skill(name, args)` plugin
tool instead of re-adding the ~1,348-token core `skills` toolset. Deploy via `/tmp` + `sudo cp`, then
`sudo systemctl restart qnoe-hermes.service`.

### 2.2 Verification gate

1. **Resident set:** confirm from the Hermes startup/session log that the model-visible tools are now
   `read_file, write_file, patch, search_files, terminal, process, clarify` (+ `tool_search`/`tool_describe`/`tool_call`
   bridges if qnoe-lab defers). No `skill_*`, `memory`, `execute_code`.
2. **Floor measurement:** fresh session, one trivial message, read the logged prompt token count. Target ≤ ~9K
   (was ~11,725). Record the exact number.
3. **Tool calling still works:** ask (via Teams or CLI) a question that forces `read_file` or `search_files` — expect
   a structured call. Then ask a QCoDeS question (e.g. "what's in run 75000?") — expect the agent to reach the
   QCoDeS tool through the `tool_search` bridge.
4. Mirror to repo + commit.

**Rollback:** restore `toolsets: [hermes-cli, qnoe-lab]` in the 3 configs, restart agent. ~5 min.

---

## 3. Step 3 — Provence reranker swap in `qnoe_rag`

**Goal:** RAG injection ~3.6K → ~1.5-2K tokens/turn by replacing the cross-encoder reranker with Provence
(prune + rerank in one 0.4B DeBERTa-v3 model). Current implementation facts (from
`hermes/plugins/qnoe_rag/__init__.py`): reranker = `CrossEncoder(RERANK_MODEL_PATH)` on **cpu**, with
`RERANK_MODEL_PATH=/opt/qnoe-agent/models/cross-encoder-msmarco`, `RERANK_POOL=20`, `TOP_K=3`,
`RERANK_THRESHOLD=0.5` (below-threshold top score ⇒ retrieval declared failed).

### 3.1 Download the model

`naver/provence-reranker-debertav3-v1` from Hugging Face → `/opt/qnoe-agent/models/provence-reranker/`
(use `huggingface_hub.snapshot_download` from the **agent venv**, `pip3` for any missing deps; model is ~0.4B ≈ 1-2 GB).
Read the HF model card for the exact API — it is `model.process(question, context)` returning pruned context +
relevance score, loaded via `AutoModel.from_pretrained(..., trust_remote_code=True)`. **Note the
`trust_remote_code=True` requirement — pin the downloaded revision and load from the local path only.**

### 3.2 Offline eval — the gate before any deploy

Write a standalone script (run in the agent venv, vLLM not needed) that takes ~20 representative queries
(reuse/adapt from `benchmark/benchmark_scores.md`; cover QTM, photocurrent, QCoDeS-runs, and group-wide topics),
runs the *existing* retrieval to get the 20-chunk rerank pool per query, then compares side by side:

| Metric | Cross-encoder (current) | Provence |
|---|---|---|
| Top-3 chunks token count | … | … (after pruning) |
| Does the answer-bearing passage survive? (manual/heuristic per query) | … | … |
| Rerank/prune wall time (cpu) | … | … |

Save the full comparison to `logs/provence_eval.md` **and commit a copy to the repo** for user review.

**Gate:** Provence keeps the answer-bearing content in ≥ 18/20 queries AND mean top-3 token count drops ≥ 35%
AND cpu latency ≤ ~2× cross-encoder. If the gate fails, STOP, report, do not deploy (the fallback candidate is
LLMLingua-2 per the report §2.2, but that is a new decision for the user, not yours).

### 3.3 Integrate behind a flag

Modify `hermes/plugins/qnoe_rag/__init__.py` (repo first, then deploy):

- New env knob `RAG_RERANKER` = `provence` | `cross-encoder` (default **`provence`** after the gate passes).
- `provence` path: lazy-load singleton like `_load_reranker()`; replace `_rerank()`'s scoring with
  `process(query, chunk_text)`; use Provence's relevance score for ordering and threshold (recalibrate the 0.5
  threshold against the eval data — Provence scores are not on the cross-encoder's scale; pick a value that
  reproduces the same accept/reject split on the 20 eval queries) and use the **pruned** text as the chunk content.
- Keep the cross-encoder path fully intact for rollback.
- Watch the known pitfall class in `memory/mistakes.md` (M30/M31 were the BM25-era hits); embedding/Qdrant code is
  untouched by this change.

Deploy via `/tmp` + `sudo cp` to `/opt/qnoe-agent/hermes/plugins/qnoe_rag/__init__.py` (profiles symlink to the shared
`hermes/plugins/`, so one copy serves all three), restart `qnoe-hermes.service`.

### 3.4 Verification gate

1. Ask 3-4 known-good RAG questions via Teams; answers remain correct and cite sensible sources.
2. From session logs: RAG injection block now ~1.5-2K tokens (was ~3.6K). Record the number.
3. A deliberately off-topic query still triggers the "retrieval failed" path (threshold behaviour preserved).
4. Mirror to repo + commit + push.

**Rollback:** `RAG_RERANKER=cross-encoder` in the service environment (or flip the default) + restart. ~2 min.

---

## 4. Expected end state (record actuals next to these)

| Quantity | Before | Target after 1-3 |
|---|---|---|
| Window / compaction trigger | 32K / ~24K | 64K / ~48K |
| KV pool capacity | ~128K tok (fp16) | ~256K tok (fp8) → ≥3 concurrent 64K users |
| Tool schemas | ~6,905 tok | ~3,700-3,900 tok |
| RAG injection | ~3,600 tok | ~1,500-2,000 tok |
| Fixed floor (fresh session) | ~11,725 tok | ~6,500-7,500 tok |
| Usable conversation before ~19.5K cliff | ~7,800 tok | ~12,000-13,000 tok (cliff itself re-measured in step 5, not yours) |

## 5. Final report — what to hand back

Write results into `SETUP_LOG.md` (dated section) and a short summary for the user containing: the fp8 vs fp16
benchmark table and which config shipped; the recorded KV-pool token count; the new fixed floor; the Provence eval
verdict (link `logs/provence_eval.md`); every deviation from this plan; and explicit statements of what was **not**
done (Mem0 deploy, nightly cron, steps 4-6). Update `memory/infrastructure.md` (new vLLM flags + KV numbers),
`memory/agent-code.md` (new toolset composition + floor), and MEMORY.md's "Current State" block. If anything failed
and was rolled back, say so plainly with the log excerpt.

## 6. Rollback summary

| Step | Rollback | Time |
|---|---|---|
| 1 | Restore 32K script (remove 2 flags), restart vllm; revert 3× `context_length`, restart agent | ~10 min |
| 2 | Restore `toolsets: [hermes-cli, qnoe-lab]` ×3, restart agent | ~5 min |
| 3 | `RAG_RERANKER=cross-encoder`, restart agent | ~2 min |
