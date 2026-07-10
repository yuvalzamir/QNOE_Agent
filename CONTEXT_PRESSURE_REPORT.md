# Context-Pressure Report — QNOE Lab Agent

*Author: Claude Code · Date: 2026-07-09 · Status: user reviewed 2026-07-09 — steps 1-5 of the roadmap **accepted** (see inline **→ Answer/Decision** blocks). §2.1 open check **resolved by DGX source inspection**: core tools never defer; slimming goes via toolset composition. Steps 1-3 handed off via [[CONTEXT_EXECUTION_PLAN]]. Nothing deployed yet.*

> **Accepted requirement (2026-07-09):** the system must be operational for **≥3 concurrent users** — met by 64K window + fp8 KV + `--max-num-seqs 4` (see §3.2 answer).

> **Scope.** You asked for an extensive report on relieving context pressure on the DGX-Spark
> Hermes-3-70B deployment, covering both directions: **(A) reduce the context we demand per turn**
> and **(B) enlarge the context space we have** — including moving to a larger vLLM / different model.
> This report answers both, plus a third direction the research made unavoidable: **(C) change the
> model class**, which turns out to be the highest-leverage move by a wide margin.
>
> Method: a 5-angle, 24-source deep-research sweep with adversarial fact-checking (partial — the
> verify pass hit a session limit, so non-core numbers are labelled *"sourced, not vote-verified"*).
> The KV-cache arithmetic below is computed from first principles and is the load-bearing correction.

---

## 0. TL;DR — the three things that matter

1. **Your "64K is not feasible, it would cost +40 GB" assumption is wrong for a single user.**
   A single Llama-3.1-70B sequence at 64K costs **~20 GB of KV** at fp16, **~10 GB at fp8** — not 40 GB.
   The 40 GB figure conflated *per-sequence* KV with the *pre-allocated pool*. vLLM's pool size is set
   by `gpu_memory_utilization`, **not** by `max_model_len`. **You can raise `max_model_len` to 64K today,
   inside the existing pool, at zero extra memory cost** (details in §3). This directly buys headroom for
   the "compaction fires too early / no room for conversation" symptom.

2. **Enlarging the window will NOT, on its own, fix the ~19.5K tool-calling collapse** — because that
   collapse is *not* raw long-context degradation. On RULER, Llama-3.1-70B still scores **94.8 at 32K**
   (vs 96.5 at 4K) — essentially flat through your entire operating range. The tool-calling cliff is a
   *tool-specific* failure mode (tool-catalog size + long tool outputs + Hermes-3 format sensitivity),
   confirmed by IBM's LongFuncEval. So the window is a comfort lever, not the cure. The cure is **demand
   reduction (§2)** and/or **a model with real tool-calling robustness (§4)**.

3. **The current model is the worst possible fit for this hardware.** A dense 70B on the Spark's
   273 GB/s memory bus is bandwidth-starved to **single-digit tokens/sec** decode (LMSYS measured Llama-3.1-70B
   FP8 at **~2.7 tok/s** decode; your INT4 ceiling is ~7-8 tok/s — consistent with your "16-20s per reply").
   A **Mixture-of-Experts model of similar or larger size** (gpt-oss-120b MXFP4, Qwen3-class A3B, GLM-4.5-Air)
   activates only ~3-5B params/token, so it decodes at **~45-60 tok/s** on the *same box*, ships **native
   tool-calling parsers**, has a **131K window**, and with fp8 KV holds **~580K tokens of KV** — making the
   whole context-space problem evaporate. **This is the recommendation with the biggest payoff.**

**Ranked plan (fastest/cheapest → biggest lever):** raise `max_model_len` to 64K + `--kv-cache-dtype fp8`
(config-only, today) → cut RAG + tool-schema demand (§2) → **pilot gpt-oss-120b / an MoE on the same box (§4)**
→ only if you need a >120B-class model, cluster two Sparks (§5).

---

## 1. Diagnosis — what is actually pressing on context

Restating your numbers from `INFERENCE_MEMORY.md` and the migration notes:

| Component | Tokens/turn | Notes |
|---|---|---|
| Tool schemas | ~6,905 | 12 active tools; largest single item |
| RAG chunks (TOP_K=3) | ~3,600 | after cross-encoder rerank |
| System prompt / SOUL + framing | ~1,200 | already trimmed 817→423 words |
| **Fixed floor** | **~11,725** | fresh QTM session |
| Compaction threshold | ~24K | `compression.threshold: 0.75` × 32K |
| Tool-calling collapse | ~19.5K | *observed* — the real ceiling |
| Hard window | 32,768 | `max_model_len` |

The binding constraint is **not** the 32K wall — it is the **~19.5K tool-calling collapse**, which sits
*below* the 24K compaction trigger. You effectively have `19,500 − 11,725 ≈ 7,800` tokens of usable
conversation before tool calls start failing. That is the number to grow.

**The critical, counter-intuitive finding:** the 19.5K collapse is not the model "running out of long-context
ability." Evidence:

- **RULER (NVIDIA benchmark)** for Llama-3.1-70B: 4K = 96.5, 8K = 95.8, 16K = 95.4, **32K = 94.8**, 64K = 88.4,
  128K = 66.6. The model is *flat* through 32K. Its "effective context length" is **64K**, half the claimed
  128K (arXiv 2410.18745, RULER repo). *(retrieval accuracy — vote-confirmed for the 64K headline)*
- **IBM LongFuncEval** (arXiv 2505.10570) shows the *tool-specific* failure: for Llama-3.1-70B-Instruct,
  tool-*selection* accuracy falls **~93% @ 8K → ~22% @ 120K** of tool-catalog context, and answer retrieval
  from long tool responses **~74% @ 10K → ~35% @ 80K**. Tool calling degrades **far faster than plain retrieval**,
  driven by *number of tools* and *length of tool outputs*, not raw context length. *(sourced, not vote-verified)*

**Implication:** your 6,905 tokens of tool schemas are doing double damage — they eat the budget *and* they are
exactly the input LongFuncEval identifies as the tool-calling degrader. Shrinking the tool surface (§2.1) attacks
both at once. And because the degradation is partly Hermes-3-format-specific, a model with hardened tool calling
(§4) raises the cliff itself rather than just delaying it.

> **RESOLVED 2026-07-10 (measured on the box; closes roadmap step 5).** Bare probes against the same
> model/vLLM/parser held structured tool calls at **400 / 8.3K / 16.4K / 32.4K tokens** — there is **no raw-model
> cliff at 19.5K**. The live failure is **prose-fallback**: in agent-shaped context (large tool catalog, RAG chunks,
> multi-turn prose, long tool outputs) the model writes `read_file(path=…)` as text instead of the structured
> channel, and the parser yields nothing — exactly the LongFuncEval mechanism above. Consequences: (a) retire
> "~19.5K" as a constant — the failure point moves with context *composition*, so watch for prose-fallback
> symptoms, not a token number; (b) tool-schema slimming (12→7 resident, shipped) attacks the mechanism itself;
> (c) deterministic injection hooks (e.g. the QCoDeS registry hook) are immune to this failure class and should be
> preferred for critical lookups. See `memory/mistakes.md` M40.

---

## 2. Direction A — Reduce context demand

Ordered by payoff per unit effort. These are additive with everything in §3–§4.

### 2.1 Tool-schema slimming / lazy loading — biggest single win (~6.9K → ~1-2K)

Tool schemas are your largest fixed cost and your tool-calling degrader. Two levers:

- **You already have the mechanism half-built.** `tools.tool_search.enabled: 'on'` defers non-core plugin tools.
  Push it harder: audit the 12 active tools and move everything that isn't needed *every* turn behind Tool Search,
  so the base prompt carries only a compact summary line per tool, not the full JSON schema.
- **Research corroboration:** "Tool Attention" (arXiv 2604.21816) reports **95% tool-token reduction (47.3K → 2.4K)**
  on a 120-tool benchmark via a two-phase lazy loader — compact summaries in context, full schema promoted only for
  the top-k tools by an embedding-based intent-overlap score. It raises effective context utilization from 24% → 91%,
  and notes quality degrades around 70% utilization. *(sourced, not vote-verified; the paper's end-to-end numbers are
  simulated, so treat the 95% as an upper bound, not a promise.)*
- **Concrete for us:** we don't need to adopt that paper — Hermes native Tool Search is the same pattern. Target:
  keep `read_file`, `list_files`, `search`, `qnoe_rag` resident; defer QCoDeS, git, and any T2+ write tools. Expected
  saving **~3-5K tokens/turn** with no capability loss (deferred tools are still callable). This alone could move the
  floor from ~11.7K to ~7-8K, roughly doubling usable conversation before the 19.5K cliff.
- **Tool Slimmer (alias8818/hermes-tool-slimmer)** remains blocked — max supported Hermes is v0.14.0, we're on v0.17.0
  (last checked 2026-07-08). Native Tool Search is the supported path; don't wait on the plugin.

- **User response** I accpet the idea of pushing the mechanism and only leaving absolut necessties in context. But, from what I understand, in Hermes you can't defer the tools that are in the the core tools. If you can, please explain to me how, and then let's do exactly that.

- **→ Answer (RESOLVED 2026-07-09, by source inspection on the DGX):** **You were right — core tools can never be deferred.** From `tools/tool_search.py` (Hermes v0.17.0, site-packages): *"Core tools defined in `toolsets._HERMES_CORE_TOOLS` are never deferred. Always-load means always-load. No exceptions."* The deferral predicate (`is_deferrable_tool_name`) hard-excludes every name in `_HERMES_CORE_TOOLS`, even if a plugin shadows it. Worse: running `classify_tools()` against our exact QTM profile config shows **all 12 resident tools are core and 0 tools are deferrable — Tool Search is currently a no-op for our profiles** (only the qnoe-lab plugin tools ever defer, and they were already excluded from the 12).

  Measured per-tool schema cost (chars/4 estimate, QTM profile): `terminal` 1,419 · `skill_manage` 1,040 · `memory` 694 · `execute_code` 604 · `clarify` 490 · `patch` 482 · `search_files` 446 · `process` 318 · `write_file` 288 · `read_file` 262 · `skill_view` 232 · `skills_list` 76 = **~6,351 tokens, 100% core**.

  **The supported lever is toolset *composition*, not deferral.** `TOOLSETS` in `toolsets.py` defines narrow basic toolsets (`file` = read/write/patch/search_files ~1,478 tok · `terminal` = terminal+process ~1,737 · `skills` ~1,348 · `memory` ~694 · `code_execution` ~604 · `clarify` ~490), and the profile `toolsets:` list feeds `get_tool_definitions(enabled_toolsets=...)` directly. So replace `toolsets: [hermes-cli, qnoe-lab]` with **`toolsets: [file, terminal, clarify, qnoe-lab]`** → resident schemas drop **~6,351 → ~3,705 tokens (−~2.6-3K)**. Dropped toolsets (`skills`, `memory`, `code_execution`) become *uncallable* (composition removes, it doesn't defer) — acceptable: the RAG/Mem0 injection is a memory *provider*, not the `memory` tool; skills registry (L4) is not yet in use; `execute_code` is a nice-to-have. If any dropped capability is later missed, the wrapper fallback applies: re-expose it as a plugin tool, which *is* deferrable behind Tool Search. Granularity limit confirmed: no config mechanism disables *individual* core tools (`platform_toolsets` is also toolset-level), so `write_file`/`patch` ride along with `file` — fine, since permissions are enforced at the shell layer (T0/T1).

### 2.2 RAG-chunk compression (~3.6K → ~1.5-2K)

Your 3 reranked chunks are prose-heavy. Two production-grade, *local, tiny-model* compressors:

- **Provence** (`naver/provence-reranker-debertav3-v1`, ICLR 2025). A **0.4B DeBERTa-v3** model that does context
  pruning *and* reranking in one pass — it can **replace your current cross-encoder reranker**, not add to the stack.
  Reported **49-82% context compression with negligible/no accuracy drop** (NQ 62-77%, HotpotQA 66-84%), sometimes
  *improving* answers by removing noise, plus a **1.2-2× generation speedup** at ~49% compression. Runs "almost free."
  *(sourced, not vote-verified)* — **strong fit**: it's a drop-in upgrade to a component you already run.
- **LLMLingua-2** (Microsoft, ACL 2024 Findings). Task-agnostic prompt compression via small encoders (XLM-RoBERTa),
  3-6× faster than LLMLingua-1. Measured **~5× on RAG prompts** (2,946 → 605 tokens) and up to **14× on CoT** with
  ~1 pt accuracy loss. *(sourced, not vote-verified.)* Caveat the researchers raise: **token-pruning methods (the
  LLMLingua family) often lag simple extractive selection** — so prefer Provence-style extractive pruning first, and
  reach for LLMLingua-2 only if you need aggressive ratios on already-selected chunks.
- **Recommendation:** swap the cross-encoder reranker for Provence (compression + rerank in one 0.4B model). Expected
  **~1.5-2K tokens/turn saved** and a latency win. This is the cleanest §2 change after tool slimming.

- **User response** Let's go for Provence.

- **→ Decision (2026-07-09): accepted.** Plan: download `naver/provence-reranker-debertav3-v1` to the DGX, eval offline against the current cross-encoder on ~20 representative QNOE queries (answer quality + tokens saved + latency), then swap it into `qnoe_rag` as the reranker+pruner. Rollback = revert the plugin file.

### 2.3 System-prompt / SOUL compression (minor, already largely done)

SOUL is already 817→423 words. Diminishing returns here (~1.2K tokens). Only worth another pass if you adopt prefix
caching (§3.4), which makes the *static* part of the prompt effectively free after the first turn — at which point
SOUL size stops mattering for latency and only matters for the token budget.

### 2.4 Net effect of Direction A

Tool slimming (~−4K) + Provence RAG (~−1.7K) ≈ **fixed floor ~11.7K → ~6K**. Usable conversation before the 19.5K
cliff roughly **triples** (~7.8K → ~13.5K), *without touching the model or memory config.* This is the safest,
fastest package and should ship first.

---

## 3. Direction B — Enlarge the context space (config-only, keep Hermes 3)

### 3.1 The KV-cache arithmetic (this is the load-bearing section)

For Llama-3.1-70B (80 layers, GQA, **8 KV heads**, head_dim 128):

```
KV bytes / token / layer = 2 (K and V) × 8 heads × 128 dim × bytes_per_elem
fp16:  2 × 8 × 128 × 2 = 4,096 B/layer  × 80 layers = 327,680 B/token = 0.3125 MiB/token
fp8:   2 × 8 × 128 × 1 = 2,048 B/layer  × 80 layers = 163,840 B/token = 0.1563 MiB/token
```

Per **single** sequence:

| Context | KV @ fp16 | KV @ fp8 |
|---|---|---|
| 32K (today) | **10.0 GiB** | 5.0 GiB |
| 64K | **20.0 GiB** | 10.0 GiB |
| 128K | 40.0 GiB | **20.0 GiB** |

**A single 32K conversation uses only ~10 GB of KV — not 40 GB.** The ~40 GB you're seeing is the *pool* vLLM
pre-allocated (`gpu_memory_utilization` × unified memory − weights − activations), sized for the default
`max_num_seqs=256` continuous-batching regime. **The pool size does not change when you raise `max_model_len`.**
*(vote-confirmed: vLLM docs — "vLLM pre-allocates GPU cache using this percentage of memory … increasing utilization
provides more KV cache space"; pool is a function of the util flag, not max_model_len.)*

### 3.2 Therefore: raise `max_model_len` to 64K today, for free

vLLM only requires that the pool can hold **at least one `max_model_len` sequence**. A ~40 GB pool ÷ 0.3125 MiB/token
≈ **128K tokens of total KV capacity** at fp16 — so it can already hold a **single 64K sequence (20 GB) with room to
spare**, or even a 128K sequence. Concretely:

```
vllm serve /opt/qnoe-agent/models/hermes-3-70b-awq --host 0.0.0.0 --port 8000 \
  --quantization awq_marlin --max-model-len 65536 \
  --enable-auto-tool-choice --tool-call-parser hermes
```

Then set Hermes `model.context_length: 65536` and keep `compression.threshold: 0.75` → **compaction now fires at
~48K instead of ~24K.** This is the direct fix for "no room for actual conversation." **Correct the memory note**
(`memory/infrastructure.md` line 79 and `MEMORY.md`): 64K is feasible for single-user at no extra memory; the "+40 GB"
claim is a per-seq/pool conflation.

**Caveat — this does not raise the 19.5K tool-calling cliff.** It gives the *scheduler* more room, but if the model
still degrades tool calls past ~20K, you've bought summarisation headroom, not reliability. Pair with §2 (fewer tools)
and/or §4 (better model). Validate the cliff empirically after the change — it may move if the tool schemas shrink.

**user response** Let's raise to 64k then. How many users does this mean the system can serve at the same time? I want it to be operational for at least 3. 

**→ Answer (2026-07-09):** Straight from the §3.1 table — the ~40 GB pool holds **~128K tokens of KV at fp16**:

| KV dtype | Pool capacity | Concurrent users **guaranteed** at full 64K | At the 48K compaction ceiling |
|---|---|---|---|
| fp16 (today) | ~128K tokens | **2** (2 × 20 GiB) | 2 comfortably, 3 marginal (45 GiB vs 40) |
| fp8 (§3.3) | ~256K tokens | **3–4** (3 × 10 GiB = 30 GiB, room to spare) | **4–5** |

Two softeners: real conversations are rarely all at the ceiling simultaneously (PagedAttention allocates KV on demand, so average concurrency is much higher than the guarantee), and when the pool *does* run out vLLM **preempts and recomputes** — requests slow down, nothing crashes. Decode speed also survives 3 users: on a bandwidth-bound box, batching 3 decode streams amortizes the weight streaming, so per-user tok/s holds roughly flat up to ~4 streams (§3.5); the real contention is prefill (one user's long prefill delays others' first token — chunked prefill interleaves it).

**Bottom line: your ≥3-user requirement is met by 64K + fp8 KV together.** fp8 KV (§3.3) moves from "optional experiment" to **the enabler of the 3-user guarantee**. Decision: **accepted — raise to 65536, pair with fp8 KV and `--max-num-seqs 4`.**

### 3.3 fp8 KV cache — halves KV, minor quality cost, enables 128K

*(vote-confirmed core facts.)* `--kv-cache-dtype fp8` (fp8_e4m3) **halves KV storage** and can cut memory-bound decode
cost to **54% of BF16**. Quality: on **Llama-3.3-70B-Instruct**, fp8 KV + fp8 attention **recovers 97-98% of baseline
128K accuracy (AUC@128k)** — i.e. near-lossless for your model class. It's supported on CUDA 11.8+.

**But two Spark-specific caveats:**
- One vote-refuted claim asserted the Blackwell fp8 path runs via FlashInfer on B200 — that was *refuted* for our
  GB10, so **don't assume the fastest fp8 attention kernel is wired up on SM121**. A DGX-Spark-specific source warns
  fp8 KV "may affect model predictability and can carry a noticeable performance cost on Spark for some workloads."
  *(sourced, not vote-verified.)*
- **Net:** fp8 KV is the lever that makes **128K single-user context trivially fit** (20 GB), and quality loss on
  70B-Llama is negligible — but **benchmark decode speed on the actual box before committing**, because the SM121
  kernel maturity is the open question, not the memory math.

**user response** I don't understand then - is there a risk here in trying this? if not, let's just give it a try.

**→ Answer (2026-07-09): no meaningful risk — it's a config flag, fully reversible.** The two possible downsides are: (1) **decode might get *slower*** on this specific GPU if the fp8 attention kernel path is immature (that's the SM121 caveat — it's a *performance* unknown, not a safety one), and (2) a theoretical quality dip, measured at 97-98% retained on the same model family, i.e. negligible. Nothing touches data or state; revert = remove the flag + restart (~5 min each way). **Decision: accepted — try it.** Protocol: benchmark tok/s + run 3-4 tool-calling queries before and after; keep the flag only if decode speed holds. Note it is now on the critical path anyway — it's what guarantees the 3-user requirement (§3.2 answer).

### 3.4 Prefix caching — make the static prompt ~free after turn 1

*(sourced, not vote-verified.)* vLLM V1 has **automatic prefix caching on by default**, and DGX-Spark guidance calls
it out as **"specifically useful for chat workloads with a long shared system prompt."** Your SOUL + tool schemas are
identical every turn — prefix caching means their prefill is computed once and reused, cutting TTFT. It doesn't reduce
the *token budget* (they still occupy the window), but it removes their *latency* cost, which de-risks §2.3/§3.2.
Confirm it's active (V1 engine) and that RAG/Mem0 injection is appended *after* the static prefix so the cacheable
prefix stays stable.

**user response** lets to it.

**→ Decision (2026-07-09): accepted.** Verify vLLM is on the V1 engine with automatic prefix caching active (startup log), and check where `qnoe_rag` injects RAG/Mem0 content — if it lands *before* the SOUL + tool schemas in the prompt, it busts the cacheable prefix every turn and the injection order must be flipped.

### 3.5 Single-user scheduler tuning

- **`--max-num-seqs`**: you are practically single-user. Lowering it (e.g. 2-4) reduces the *concurrent* KV demand
  the scheduler plans for — one verify vote pushed back on "=1 shrinks the pool" (the pool is pre-allocated regardless),
  so the honest framing is: **low max-num-seqs doesn't grow the pool, but it removes contention** so a single long
  sequence reliably gets the blocks it needs. DGX-Spark Nemotron guidance recommends keeping it **≤4** anyway, because
  above ~4 concurrent decode streams the memory-bandwidth tax outweighs batching gains on this box. *(sourced.)*
- **Chunked prefill**: on by default in V1 — no action.
- **Swap space**: V1 prefers *recompute* over CPU-swap, so legacy `--swap-space` is largely moot; LMSYS still suggests
  enabling some swap on Spark "for stability." Low priority.

  **user response** - I am not a single user. For now I am but the system is intended to be used by many more. Do these recommendation hold for that?

  **→ Answer (2026-07-09): yes — the ≤4 recommendation is a property of the box, not of your user count.** Above ~4 concurrent decode streams the memory-bandwidth tax on this hardware slows *everyone* down, whether it's 1 user or 20. So the multi-user setting is **`--max-num-seqs 4`**: it serves your 3-user target with one slot of headroom, and a 5th simultaneous request queues briefly instead of degrading the other four. More users than that are fine as long as they aren't all generating at the same instant — Teams turn-taking makes true 4-way simultaneous decode rare. If the group genuinely outgrows ~4 simultaneous streams, that's an argument for the MoE swap (§4), which reads ~10× fewer bytes per token and changes this arithmetic entirely. **Decision: set `--max-num-seqs 4`.**

### 3.6 Net effect of Direction B

Config-only, still Hermes 3: **64K window (compaction at ~48K), 128K reachable with fp8 KV, static prompt latency
removed.** Solves the *space* symptom. Does **not** by itself solve the *tool-calling reliability* symptom. Zero new
software, ~5 min vLLM restart, reversible.

---

## 4. Direction C — Change the model class (the big lever)

This is the direction the research kept pointing at, and it addresses **both** symptoms at once plus the latency
problem you've been living with.

### 4.1 Why the dense 70B is the wrong tool for this box

The Spark is **memory-bandwidth-bound at 273 GB/s** (LPDDR5x, shared CPU+GPU — vote-adjacent, widely reported).
A dense model must stream *all* its weights per token:

| Model | Active params/token | Bytes read/token | Bandwidth-ceiling decode | Measured on Spark |
|---|---|---|---|---|
| Hermes-3-70B dense, INT4 | 70B | ~35 GB | ~7-8 tok/s | (your ~2.7-8 range) |
| Hermes-3-70B dense, FP8 | 70B | ~70 GB | ~3.9 tok/s | **~2.7 tok/s** (LMSYS, SGLang FP8) |
| **gpt-oss-120b MoE, MXFP4** | **~5B** | **~3 GB** | **~90 tok/s** | **~45-59 tok/s** (multiple sources) |
| Qwen3-class 30-35B **A3B** MoE | ~3B | ~2 GB | high | **~44-70 tok/s** |
| Nemotron-3-Super-120B-**A12B** NVFP4 | ~12B | ~7 GB | ~35 tok/s | **~22-24 tok/s** |

*(All Spark numbers: sourced, not vote-verified — llama.cpp discussion #16578, LMSYS Spark reviews, NVIDIA forum
362824, ai-muninn blog.)* **The MoE advantage on this hardware is roughly 10-20× decode throughput** because only a
fraction of the weights are read per token. Your "16-20s per short reply" becomes "<2s."

### 4.2 Candidate replacements (all fit the single 128 GB box)

- **gpt-oss-120b (MoE, MXFP4, ~60 GB weights)** — the front-runner.
  - Runs on one Spark (TP=1), ~50-59 tok/s decode, ~1000 tok/s prefill, TTFT 2-5s. *(sourced.)*
  - **131K context**; with `--kv-cache-dtype fp8 --gpu-memory-utilization 0.90` the KV pool holds **~580K tokens** —
    context space is a *non-issue*. *(sourced.)*
  - **Native tool-call + reasoning parsers** in both vLLM (`--tool-call-parser`) and SGLang (`--tool-call-parser gpt-oss`)
    — directly attacks the tool-calling cliff that Hermes 3 hits.
  - **Big caveat — SM121 maturity.** As of early-mid 2026, stock vLLM was **not turnkey** on GB10: needs Marlin MXFP4
    backend (`VLLM_MXFP4_BACKEND=marlin`, `VLLM_MARLIN_USE_ATOMIC_ADD=1`), and there are real bugs — **TP=1 produced
    malformed chat output on some builds** (shared-memory race in the Marlin MoE kernel; vLLM issue #37030 logit
    corruption), and reports of "random Chinese words"/tool glitches. **SGLang's Spark image (`lmsysorg/sglang:spark`)
    and NVIDIA's `nvcr.io/nvidia/vllm` container are the safer serving paths** than hand-built vLLM. *(sourced.)*
    → **Pilot behind a flag; do not cut over blind.**
- **Qwen3-class A3B MoE (e.g. Qwen3-Next-80B-A3B / Qwen3-30B-A3B)** — ~44-70 tok/s on Spark, strong tool calling and
  long-context reputation, actively benchmarked on this hardware. Good "second quote" to gpt-oss.
- **GLM-4.5-Air / GLM-4.7-Flash (NVFP4)** — community-run on Spark (single and dual node); competitive MoE option.
- **Nemotron-3-Super-120B-A12B (NVFP4)** — NVIDIA-native, runs at `max_model_len 131072`, ~22-24 tok/s (heavier
  A12B activation → slower than gpt-oss but still ~10× the dense 70B). Best if you want an NVIDIA-supported stack.
- **Hermes 4** — the research did not return a verified Hermes-4 size/spec sheet; **treat as an open item to confirm**
  before assuming it's a drop-in successor. Don't put it on the critical path yet.

### 4.3 What a model swap costs you

- **Re-validation of the whole agent stack** against the new tool-call format (your `--tool-call-parser` changes;
  SOUL/persona prompts may need light retuning). Your D11 fix (`tool_use_enforcement: true`) was Hermes-3-specific —
  gpt-oss/Qwen have native enforcement, so that hack likely becomes unnecessary (a simplification).
- **RAG/Mem0 unaffected** — embeddings (nomic) and Qdrant don't change; only the generation model does.
- **Serving-stack risk** on SM121 (above). Budget a real pilot: stand up gpt-oss-120b on SGLang-Spark on the side,
  A/B a dozen QNOE queries incl. tool calls, compare tool-call reliability + latency + answer quality vs Hermes 3.

### 4.4 Recommendation

**Pilot gpt-oss-120b (SGLang-Spark image first) as the generation model.** If it clears tool-calling and quality bars,
it simultaneously (a) removes the context-space constraint (580K KV), (b) raises tool-calling robustness via native
parsers, and (c) delivers ~10-20× decode speed. This is a larger change than §2/§3 but it's the one that makes the
context problem stop being a problem. Keep Hermes 3 as the fallback profile during the pilot.

---

## 5. Direction D — Scale out (two DGX Sparks)

*(All sourced, not vote-verified — LMSYS, corti.com, StorageReview cluster review, NVIDIA forum 358755.)*

- **What it is:** two Sparks linked via their dual **ConnectX-7 200 GbE QSFP** ports, point-to-point RoCE/RDMA
  (200 GbE QSFP56 DAC cable, no switch for 2 nodes), aggregating **256 GB** unified memory. ~$4K/unit.
- **What it buys — model size, not speed, and not (mainly) context.** Bandwidth is **per-node ~273 GB/s and does not
  aggregate**; inference stays memory-bound. A ~120B model goes from ~35-50 tok/s (1 node) to ~55-75 tok/s (2 nodes) —
  a modest gain — while the *ceiling* on model size jumps to **405B-class FP4 / 235B MoE**. High-batch aggregate on
  gpt-oss-120b PP=2 hits ~464-505 tok/s, but **single-user throughput on bigger clustered models drops to single digits.**
  The StorageReview review is explicit: clustering is for **fitting models too big for one box, not for longer context.**
- **Real-world friction:** TP=2 over the 200 GbE fabric is bottlenecked (ConnectX-7 sits behind PCIe Gen5 x4);
  **pipeline parallelism (PP=2) is the practical mode**. Requires careful NCCL config (`NCCL_IB_HCA` listing *both*
  NIC ports, jumbo frames MTU 9000) or it **silently falls back to TCP sockets** and tanks. Community reports of a
  node dropping / GPU pegged at 100% traced to a PyTorch version mismatch; `--enforce-eager` was a workaround (but
  halves decode). A **community Docker build (`eugr/spark-vllm-docker`)** is the recommended, "battle-tested" path.
  Scaling beyond 2 nodes "degrades sharply."
- **Verdict for us:** **not the answer to context pressure.** We don't need a >120B model, and clustering doesn't
  extend context per-user meaningfully. Revisit *only* if a future requirement demands a 200B+ model locally. The
  single-box MoE swap (§4) is strictly better for our problem.

---

## 6. Recommended roadmap *(updated 2026-07-09 after user review + §2.1 source inspection — this is the accepted workflow)*

| # | Action | Direction | Effort | Payoff | Risk |
|---|---|---|---|---|---|
| 1 | **vLLM flags in one restart:** `--max-model-len 65536 --kv-cache-dtype fp8 --max-num-seqs 4`; then Hermes `context_length: 65536` (3 profiles), threshold 0.75 → compaction @ ~48K. Benchmark decode + tool calls with/without the fp8 flag; keep fp8 only if decode holds | B | ~1-2 h (incl. 2 vLLM restarts) | 64K window; **≥3 concurrent users guaranteed** (fp8 pool ≈ 256K tokens); compaction headroom doubled | Low — each flag independently revertible; fp8 is the only measured gate |
| 2 | **Tool-schema slimming via toolset composition** (NOT Tool Search — core tools never defer, see §2.1): `toolsets: [file, terminal, clarify, qnoe-lab]` replacing `hermes-cli`, all 3 profiles | A | ~1 h + agent restart | Schemas ~6.4K → ~3.7K (**−~3K tok/turn**); fewer resident tools also attacks the tool-calling degrader | Low — one-line revert; drops `skills`/`memory`/`execute_code` tools (uncallable until re-added or wrapped as deferrable plugin tools) |
| 3 | **Swap cross-encoder reranker → Provence** (`naver/provence-reranker-debertav3-v1`, prune+rerank in one 0.4B model) in `qnoe_rag`, behind an env flag, after a 20-query offline eval vs `cross-encoder-msmarco` | A | ~1 day | ~−1.7K tok/turn + generation speedup | Low-med — eval gate + flag rollback |
| 4 | **Confirm prefix caching active** (V1 engine log); verify RAG/Mem0 injection lands *after* the static SOUL+tools prefix | B | Hours | Lower TTFT; makes the remaining static prompt ~free | Low |
| 5 | **Re-measure the ~19.5K tool-calling cliff** after 1-2 (smaller tool surface may move it); update D11 notes and the compaction margin accordingly | — | ~1 h | Validates the package; sets the new operating envelope | None |
| 6 | **Pilot gpt-oss-120b MoE** (SGLang-Spark image), A/B vs Hermes 3 on tool calls + latency + quality | C | ~1 week | ~10-20× decode; native tool calling; 580K KV — dissolves the problem | Med-high — validate tool format + SM121 |
| — | Cluster two Sparks | D | High | Only enables >120B models; not a context fix | High; deferred |

**Sequencing:** steps **1-3 are packaged for execution by a separate agent — see [[CONTEXT_EXECUTION_PLAN]]** (step-by-step
commands, verification gates, rollback). Prereq already met: the BM25 backfill completed 2026-07-09 ~12:04 UTC (all 10
collections stamped in `sparse_backfill`), so vLLM can restart with the new flags at any time. Expected combined result:
floor ~11.7K → ~7K, window 32K → 64K, ≥3 concurrent users. Run 4-5 afterwards as measured checks. Run 6 as a parallel
pilot; if it lands, it supersedes much of the need for 1-4's careful budgeting. Skip D unless requirements change.

Two hacks that likely become **removable** after step 6, worth noting for the migration audit: the
`tool_use_enforcement: true` workaround (D11) and the ~19.5K-token discipline itself — both are Hermes-3 artifacts.

---

## 7. Confidence & caveats

- **Vote-confirmed (3-0 adversarial):** fp8 KV halves storage / ~54% decode cost; fp8 KV + attention recovers 97-98%
  of 128K accuracy on Llama-3.3-70B; vLLM `--kv-cache-dtype` fp8/fp8_e4m3/fp8_e5m2 support; KV pool sized by
  `gpu_memory_utilization` not `max_model_len`; Llama-3.1-70B effective context = 64K on RULER.
- **Vote-refuted (excluded from recommendations as stated):** "Blackwell fp8 runs via FlashInfer on B200" (not our GB10);
  "max_num_seqs=1 shrinks the pool" (pool is pre-allocated).
- **Sourced, not vote-verified (session limit cut the 3-vote pass):** all Spark tokens/sec figures, LongFuncEval
  numbers, Provence/LLMLingua-2 compression ratios, Tool-Attention 95% claim, multi-Spark clustering details, SM121
  bug list. These come from primary/high-quality sources (arXiv, vLLM blog, LMSYS, NVIDIA forums, llama.cpp) but
  **should be treated as strong indicators, not settled facts** — re-run verification when the session budget resets,
  and **measure the two decision-critical numbers on the actual box**: (a) fp8-KV decode speed on Hermes-3/SM121,
  (b) gpt-oss-120b tool-call reliability + decode speed under SGLang-Spark.
- **The KV arithmetic in §3.1 is computed, not sourced** — it's standard GQA KV math and is the correction I'm most
  confident about. If Hermes-3-70B's config differs from stock Llama-3.1-70B (it shouldn't — same architecture), re-check
  `num_key_value_heads`, `num_hidden_layers`, `head_dim` and rescale linearly.

### Key sources
vLLM FP8 KV-cache blog (2026-04-22) · vLLM DGX-Spark blog (2026-06-01) · vLLM docs: quantized_kvcache,
optimization, conserving_memory · arXiv 2410.18745 (effective context) + NVIDIA/RULER · arXiv 2505.10570 (LongFuncEval)
· arXiv 2501.16214 + naver/provence HF (Provence) · llmlingua.com + arXiv 2407.08892 (LLMLingua-2) · arXiv 2604.21816
(Tool Attention) · LMSYS Spark reviews (2025-10-13, 2025-11-03) · llama.cpp discussion #16578 · NVIDIA forums 362824 /
358755 · corti.com + StorageReview two-Spark cluster reviews.
