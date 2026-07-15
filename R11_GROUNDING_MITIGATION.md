# R11 — Grounding / Anti-Confabulation Mitigation Options

*Created 2026-07-15. Design options for the redteam R11 finding. No option is
built yet — this is the decision menu. See `redteam/BACKLOG.md` R11 and [[TODO]]
(🔴 grounding hardening item).*

## The problem (what R11 actually was)

Live Teams, 2026-07-15: **"What high-bias photocurrent measurements do we have on
bilayer graphene?"** RAG retrieval worked (9 KB of real context; genuine files
surfaced), but the model **fabricated on top of it**:

- **Invented a QCoDeS run** at `/opt/qnoe-agent/qcodes_dbs/photocurrent/highbias_blg_2024-07-03.db`
  — path does not exist, no such run in either registry, the registry hook never
  fired (`qcodes_block=False`), and it claimed to have "already run" a
  `qcodes_search` invocation with invented params (NDC 0.35 V, 150 nA peaks).
- **Invented a file** (a `.pptx` under a `Literature/Slides/` dir that doesn't exist).
- **Attached invented experimental numbers** (responsivities, critical currents)
  to both real and fake files.

Not a B7/sandbox issue — pure grounding, same class as M46/M47 (Mem0 poisoning)
and R3 (calibration future-prediction). Would repeat identically on the host.
**For a lab assistant, authoritative fabricated physics with fake paths is the
worst failure mode there is.**

### Why our case is special (and easier than generic RAG)

The fabricated claims were mostly **structured and deterministically checkable**:
a file path, a run ID, a `.db` path. We can verify each against the file manifest
and the QCoDeS registry with **zero model judgment**. We already own the oracle
(the registry/find_file hooks + the redteam `oracle.py`). This makes a post-hoc
validator (Option 2) far cheaper and more certain for us than for a generic
corpus. Only the *embellished prose numbers* need a softer, model-based check.

### Trigger diagnosis

Survey / enumeration phrasing ("**what X do we have**", "list all…", "summarize
the measurements") does **not** trip the specific-run registry hook, so the model
free-forms. gpt-oss-120b @ temp 0.2 still confabulates on open-ended synthesis.
The existing SOUL grounding rules (retrieval-only for lab facts) are violated
under this open-ended mode.

---

## Options

Ordered roughly cheapest-first. Each: what it is · how the field uses it · fit
for us · cost · what it catches.

### Option 1 — Cite-or-abstain (citation-enforced generation)
- **What:** every load-bearing claim must carry an inline source tag pointing to a
  retrieved chunk or tool row (`[/ICFO/…/file.pdf]`, `[run 848]`) or be explicitly
  marked "general literature." Uncited factual claims → reject/regenerate.
- **Field:** the highest-leverage lever in accuracy-critical RAG. Key nuance:
  citation *correctness* is insufficient — you need citation *faithfulness* (the
  claim must actually be supported by the cited source, not superficially aligned
  = "post-rationalization").
- **For us:** extends current SOUL "should cite" → "must cite, per claim." Cheap
  (prompt). Enforcement only probabilistic unless paired with Option 2.
- **Cost:** low (prompt only). **Catches:** most casual fabrication; makes Option 2
  trivial (claims are pre-tagged with their supposed source).

### Option 2 — Deterministic post-hoc validation ⭐ (highest ROI for us)
- **What:** a non-LLM guard after generation: extract every file path / QCoDeS run
  ID / `.db` path in the reply and verify each against the manifest + registry.
  Non-existent references get stripped, flagged ("⚠️ referenced a run I can't
  confirm"), or trigger a regenerate. Enforces "every load-bearing claim traces to
  a real record or is marked as not doing so" — in code.
- **Field:** the "enforced traceability" backstop; sentence/line-level checks that
  expose citation-shaped hallucinations.
- **For us:** directly kills the worst class — the fake `qcodes_dbs/…` path and the
  invented run are both a `SELECT` away from being caught. **We already have the
  pieces** (registry hook, find_file manifest search, redteam oracle). Cheaper &
  more certain for us than for generic corpora because our structured claims are
  100% checkable.
- **Cost:** medium (one code component; ~no latency — it's a DB lookup).
  **Catches:** every fabricated path / run ID / db path. Does NOT catch embellished
  prose numbers (→ Option 4).

### Option 3 — Abstention tuning ("know when not to answer")
- **What:** explicitly teach the model to withhold when retrieved context lacks the
  answer. Tiers: prompt-only few-shot ("I don't have that recorded") → `[IDK]`
  vocabulary token → fine-tune on unfamiliar examples (PREREQ-TUNE).
- **Field:** consensus that today's best LLMs do NOT natively know when to abstain;
  it must be trained/prompted in.
- **For us:** prompt tier only — add abstention exemplars to SOUL for
  "what do we have / list / survey" phrasing (the exact R11 trigger). No fine-tune
  needed for gpt-oss.
- **Cost:** low (prompt). **Catches:** the "answer beyond the evidence" root cause,
  especially on surveys.

### Option 4 — Chain-of-Verification (gated self-check pass)
- **What:** draft → generate per-claim verification questions → answer them
  *independently* against tools/RAG (no draft visible, to avoid self-confirmation)
  → revise. Published: CoVe lifted LLaMA-65B FactScore ~63.7 → ~71.4.
- **Field:** standard for high-stakes info-seeking; strongest when verification
  calls real tools/retrieval.
- **For us:** the only option that catches **embellished experimental numbers**
  Option 2 can't (re-checks prose against sources). Multi-pass → real added latency
  on our hardware (~47 tok/s decode), so **gate it** to fire only on
  measurement/survey questions, not every turn.
- **Cost:** high (extra model passes = seconds/answer). **Catches:** embellished
  numbers, unsupported synthesis.

### Option 5 — Tool-forced structured answers for measurement queries
- **What:** for any runs/measurements/data question, *require* a `qcodes_search`
  tool call and build the answer **only** from returned rows via a template — the
  model fills fields, never narrates free-form. We already do this for single-run
  queries via the deterministic registry hook; the fix is to **extend that hook to
  survey/list phrasings** so those also become tool-sourced.
- **Field:** grounding + tool-use enforcement (Microsoft best-practices; tiered
  retrieval).
- **For us:** natural extension of the run-id + find_file hooks we built; closes the
  exact gap R11 hit (`qcodes_block=False` on the survey → free-form). Surgical fix
  for the qcodes-fabrication flavor.
- **Cost:** medium (extend an existing hook). **Catches:** fabricated QCoDeS
  runs/params on survey questions, at the source.

### Option 6 — Uncertainty gating (semantic entropy / self-consistency)
- **What:** sample N times / measure semantic entropy; claims that vary across
  samples are low-confidence → abstain or flag.
- **Field:** where a confidence signal is needed without a checkable oracle.
- **For us:** **weakest fit** — N-sample generation is expensive on our hardware, and
  we HAVE an oracle (Option 2 gives certainty where this gives only a probability).
  Listed for completeness; not recommended to lead with.
- **Cost:** high (N× generation). **Catches:** low-confidence claims generally.

---

## Recommended stack (layered — not a single pick)

No single technique suffices; accuracy-critical deployments layer a cheap first
line with a hard backstop. For our failure + hardware:

1. **Options 1 + 3 — cheap first line** (prompt/SOUL): per-claim citation +
   abstention exemplars for survey phrasing. No latency cost; catches the easy
   majority. Days, not weeks.
2. **Option 2 — hard backstop** (highest ROI): deterministic validation of paths /
   run IDs / db paths against manifest + registry. Makes a fabricated run/path
   **structurally impossible to present unflagged** — the guarantee a lab wants.
   Reuses infra we already own.
3. **Option 5 — close the qcodes survey gap at the source**: extend the registry
   hook to list/survey phrasings so measurement claims are tool-sourced.
4. **Option 4 (gated CoVe)** — ONLY if embellished prose numbers still slip after
   1–3, because it's the only one that costs real latency.

Plus a standing **red-team survey/confabulation probe suite** (the harness already
has the shape) so we measure this the way we now measure injection.

**Suggested first increment:** 1 + 3 + 2 (biggest risk-reduction per effort).

---

## Sources
- Correctness is not Faithfulness in RAG Attributions — https://arxiv.org/abs/2412.18004
- Citation-Enforced RAG (fiscal) — https://arxiv.org/html/2603.14170v1
- Trustworthy RAG with in-text citations — https://haruiz.github.io/blog/improve-rag-systems-reliability-with-citations
- RAG Grounding: 11 tests that expose fake citations — https://medium.com/@Nexumo_/rag-grounding-11-tests-that-expose-fake-citations-30d84140831a
- The Art of Refusal: Survey of Abstention in LLMs — https://arxiv.org/html/2407.18418v1
- Do LLMs Know When to NOT Answer? — https://arxiv.org/abs/2407.16221
- Fictitious Synthetic Data / PREREQ-TUNE — https://arxiv.org/pdf/2410.19290
- Chain-of-Verification Reduces Hallucination — https://arxiv.org/pdf/2309.11495
- CoVe explainer — https://learnprompting.org/docs/advanced/self_criticism/chain_of_verification
- Domain-Grounded Tiered Retrieval — https://arxiv.org/pdf/2603.17872
- Microsoft — best practices for mitigating hallucinations — https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/best-practices-for-mitigating-hallucinations-in-large-language-models-llms/4403129
- Hallucination Mitigation for RAG: a Review — https://www.mdpi.com/2227-7390/13/5/856
- How to Reduce LLM Hallucinations (Zep) — https://www.getzep.com/ai-agents/reducing-llm-hallucinations/
