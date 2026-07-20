# Cognee — Corpus Knowledge-Graph Layer (ground truth of group knowledge)

*Created: 2026-07-17. Status (2026-07-20): **Phase-0 pilot stood up on the DGX** (`/home/yzamir/cognee-pilot/`, Cognee 1.4.0 + OWL ontology, repo copy `cognee/`) — small pre-OWL graph produced; full overnight OWL run died 2026-07-18 (~02:54, box overload + embedded-URL fetching); fixes committed (da55efa) but **not yet redeployed/relaunched** — see [[TODO]] Cognee item. Critical memory layer.*

> Related: [[MEMORY_ARCHITECTURE]] (the Cognee+Mem0 boundary this realizes) · [[MEM0_HYGIENE_OPTIONS]] · `CLAUDE.md` §"Memory layers L1–L5" (this is the concrete L5) · [[memory/infrastructure]] (Qdrant, llama.cpp, watcher, nightly)

---

## 0. Goal & non-goals

**Goal.** A **knowledge graph of the group's RESEARCH PROGRAM**, built from the corpus (**DATA, not conversations**). It captures two things, not one:
- **The research** — the *concepts* and *phenomena* the group studies, the *open questions* and *research directions* it pursues, the *techniques/methods* and *setups* it uses as research tools, the *projects/efforts*, *hypotheses*, and *findings*. This is the knowledge the lab *thinks about*, not just the data it records.
- **The factual anchor** — the samples, runs, devices, setups, people that ground the research in what was actually measured, where, by whom.

It is NOT merely a catalogue of measurements. It complements — not replaces (at first) — the existing chunk-RAG, and its factual tier becomes the **oracle** the grounding validator and the Mem0 own-work rule defer to (see [[MEMORY_ARCHITECTURE]] decisions #2/#5).

**Two tiers of truth (the central design point).** The graph mixes two epistemically different kinds of node, and they must not be conflated:
- **Tier 1 — factual anchor (deterministic).** Samples/runs/setups/devices from the registry via `add_data_points`, no LLM → *actual ground truth*.
- **Tier 2 — research/conceptual (LLM-inferred).** Concepts/questions/techniques/directions/hypotheses extracted from prose by `cognify` → *derived knowledge*, only as reliable as the extraction. This is the real value of the graph AND its real risk: a weak model can confabulate concepts/relationships, producing a "ground-truth graph" that is actually an LLM's guess — the exact failure mode this project fights (R11 / grounding validator / Mem0 poisoning). **Tier-2 nodes therefore carry provenance (source doc + extracting model) and are treated as inferred-with-source, never asserted as authoritative fact.** Cognee tracks provenance to source episodes, which supports this — but it does not remove the need for a strong extraction model + validation.

**Non-goals.** Per-user personalization (that stays Mem0). Conversational memory. Replacing chunk-RAG on day one (KG runs *alongside* until it proves out).

**Hard constraints.** Fully offline / self-hosted (no data leaves the lab). Reuse existing infra where possible (Qdrant, nomic-embed, llama.cpp, the ingest/watcher pipeline). Runs on the DGX; the ~47 tok/s local model is the binding performance constraint.

---

## 1. Framework choice — Cognee, and why (alternatives honestly weighed)

Four frameworks were researched against these exact constraints. Summary:

| Criterion (for THIS use case) | **Cognee** | LightRAG | MS GraphRAG | Graphiti |
|---|---|---|---|---|
| **Load structured registry as typed nodes WITHOUT LLM** | ✅ `add_data_points()` | ⚠️ extraction-based | ⚠️ extraction-based | ⚠️ open issue only |
| Typed/prescribed ontology (ground-truth enforcement) | ✅ Pydantic `DataPoint` + prescribed ontology | ❌ soft prompt only | ⚠️ prompt entity_types | ✅ Pydantic types |
| Continuous/incremental update | ✅ `incremental_loading=True` + skip-unchanged | ✅ set-merge | ❌ batch; update can degrade to full re-index | ✅ real-time bi-temporal |
| Reuse existing **Qdrant** | ✅ native backend | ✅ (own collections) | ⚠️ custom, vectors-only | ❌ Milvus-only overlay |
| Graph DB = **Kùzu** (matches L5, embedded) | ✅ default | ❌ | ❌ Parquet files | ⚠️ Kùzu deprecated |
| Local LLM + local embeddings (llama.cpp + nomic) | ✅ | ✅ | ✅ | ⚠️ works, `#1116` gotcha |
| Chat integration (official MCP + SDK) | ✅ `cognee-mcp` + SDK | ⚠️ community MCP only | ⚠️ no MCP | ✅ official MCP |
| Local extraction cost on ~1M chunks @47 tok/s | ⚠️ heavy — **but avoidable** (see below) | ⚠️ heavy; **<50 tok/s timeout risk flagged** | ❌ impractical (weeks; MS's 5GB≈$33k GPT-4) | ⚠️ heavy backfill, cheap steady-state |
| License | Apache-2.0 | MIT | MIT | Apache-2.0 |

**The deciding factor — `add_data_points()`.** Cognee can insert **typed nodes directly from already-structured data, bypassing the LLM entirely.** Our largest, highest-value, most-authoritative source — the **76k-row QCoDeS registry** — is already structured. We load it as `Sample`/`Run`/`Setup`/`Person` nodes deterministically: **zero LLM calls, perfectly accurate, and it sidesteps the ~47 tok/s extraction-throughput risk that dominates every other option.** LLM extraction (`cognify`) is then reserved only for the genuinely unstructured docs (papers, repo prose, SharePoint). No other framework combines *(structured-direct load)* + *(typed ontology)* + *(Qdrant reuse)* + *(Kùzu)* this cleanly.

**Honest dissent (recorded).** The LightRAG research argued LightRAG is a "closer architectural fit" because Cognee is marketed as agent-memory-oriented. Fair caution — but it under-weights the two things that actually decide it here: our data is *mostly a structured registry* (so `add_data_points` matters more than extraction quality), and *reusing Qdrant + Kùzu* matters. LightRAG's ontology is soft prompt-guidance (weak for a *ground-truth* typed graph) and its extraction would sit right on the documented <50 tok/s timeout cliff. **Graphiti** is the strongest on continuous-update + MCP, but it **cannot reuse Qdrant** (Milvus-only vector overlay), Kùzu is deprecated there, and it's episode-oriented, not bulk-corpus — three strikes against our reuse-infra requirement. **GraphRAG** is disqualified by batch indexing + Parquet-not-a-DB + impractical local cost.

**The conceptual re-weighting (important).** `add_data_points` decides the *factual anchor* (Tier 1). But the **research/conceptual graph (Tier 2) is the heart**, and it is **LLM-extraction-bound for every framework** — so the conceptual layer does NOT differentiate Cognee from LightRAG/Graphiti on capability; the real lever there is the **extraction model + validation**, not the framework. Cognee still wins overall because it *also* nails the factual anchor + Qdrant/Kùzu reuse + a typed conceptual ontology + provenance — but choosing Cognee does not by itself buy a good conceptual graph. That must be earned in Phase 0/2 (§7, §8).

**Verdict: Cognee.** Eyes open on the real risks (§8): first and foremost the **conceptual-tier extraction quality** (a weak model confabulates a research graph), then local structured-output reliability, extraction throughput, and Cognee's fast-moving API.

---

## 2. Architecture

```
        CORPUS (data, not chat)                         COGNEE (one graph, hybrid build)
 ┌──────────────────────────────────┐        ┌───────────────────────────────────────────┐
 │ QCoDeS registry (76k runs, SQLite)│──STRUCTURED──▶ add_data_points()  (NO LLM)          │
 │ repos / papers / SharePoint (text)│──UNSTRUCT.──▶ cognify(ontology)  (LLM, gated)       │
 └──────────────────────────────────┘        │   ▼                                         │
   reuse ingest/parse output (don't re-parse) │  Kùzu graph  +  Qdrant vectors  + SQLite    │
   share nomic embedder (one vector space)    │  (typed nodes/edges) (cognee_* collections) │
                                              └───────────────┬─────────────────────────────┘
                                                              │ search(GRAPH_COMPLETION / INSIGHTS)
                                                    ┌─────────▼─────────┐
                                                    │ Hermes chat (hook │  ← KG context injected;
                                                    │  + kg_search tool)│    becomes grounding ORACLE
                                                    └───────────────────┘
```

- **Graph:** Kùzu (embedded, on-disk under `/opt/qnoe-agent/cognee_data/`) — matches the deferred L5 Kùzu plan, no service to run.
- **Vector:** the **existing Qdrant** (`VECTOR_DB_PROVIDER=qdrant`, same `localhost:6333`), in **its own `cognee_*` collections** (Cognee re-embeds into its own space — see §4 for the honest reuse boundary).
- **Relational:** SQLite (Cognee's bookkeeping), separate file.
- **LLM/embeddings:** llama.cpp `/v1` for `cognify` extraction; **nomic-embed served OpenAI-compatible** so KG vectors share the RAG embedding space. Structured output via **instructor/BAML** (matters — it constrains gpt-oss's flaky JSON).
- **Hybrid build (the core idea):** a **deterministic backbone** from the registry (`add_data_points`, no LLM) + **LLM enrichment** from docs (`cognify` constrained by the ontology), both landing in **one Kùzu graph**.

---

## 3. Point 1 — Ontology: a research-program schema, seeded then extracted

The ontology has **two tiers** (per §0). Cognee supports a **prescribed ontology** (you define the schema) **plus learned** (extraction discovers entities/relations around it) — so the factual tier is seeded deterministically and the conceptual tier is extracted and linked onto it.

**Tier 2 — the research/conceptual node types (the heart; LLM-extracted, provenance-tagged):**
- `Concept` / `Phenomenon` — e.g. *moiré flat bands*, *photothermoelectric effect*, *quantum Hall photocurrent*, *momentum-resolved tunneling*, *cavity QED*, *hyperbolic phonon-polaritons*.
- `Question` / `OpenProblem` / `ResearchDirection` — e.g. *how twist angle tunes the tunneling momentum*, *non-local conductivity in BLG*, *what limits photocurrent responsivity*.
- `Technique` / `Method` — e.g. *scanning photocurrent microscopy*, *momentum-resolved tunneling spectroscopy*, *nano-IR*.
- `Project` / `Effort` — a research thrust (maps to repos like `SLG07-PhQH`, `BLG-QED`, and to funded directions).
- `Hypothesis` / `Claim` / `Finding` — asserted or concluded statements (each with source + status).
- `Paper` / `Publication`, `Material` (graphene, hBN, MoO₃, BSCCO — bridges tiers).

**Tier 1 — the factual anchor node types (deterministic, from the registry/configs):** `Sample`, `Run`, `Setup`, `MeasurementType`, `Device`, `Person`.

**Relationships that make it a *research* graph (not a catalogue):**
`Project –studies→ Concept` · `Project –pursues→ Question` · `Project –uses→ Technique` · `Technique –runs_on→ Setup` · `Paper –addresses→ Question` · `Run –tests→ Hypothesis` · `Run –probes→ Phenomenon` · `Concept –motivates→ Question` · `Project –builds_on→ Project/Paper` · `Person –works_on→ Project` · `Finding –supports/refutes→ Hypothesis` · plus the factual edges `Run –measured_on→ Sample`, `Run –on_setup→ Setup`, `Run –is_type→ MeasurementType`, `Person –owns→ Sample`.

This is what lets the graph answer *research* questions — "what techniques does the group use to study moiré flat bands", "which open questions is the photocurrent team pursuing and what have they found", "what setups enable momentum-resolved spectroscopy" — not just "list runs on sample X".

Define node types as `DataPoint` subclasses; `metadata.index_fields` says which fields to embed for search.

```python
from cognee.infrastructure.engine import DataPoint
from pydantic import SkipValidation
from typing import Any

class Setup(DataPoint):        # L110 QTM, L206 Photocurrent, L208 Opticool
    name: str
    metadata: dict = {"index_fields": ["name"]}

class Sample(DataPoint):
    name: str                  # folder name, e.g. Tip5Sample9
    crystal: str = ""          # from registry sample_name free text
    in_setup: SkipValidation[Any] = None      # -> Setup
    metadata: dict = {"index_fields": ["name", "crystal"]}

class Run(DataPoint):
    run_id: int
    db_path: str
    run_name: str = ""
    exp_name: str = ""
    completed_at: str = ""
    measured_on: SkipValidation[Any] = None   # -> Sample
    is_type: SkipValidation[Any] = None       # -> MeasurementType (gate-sweep/IV/...)
    metadata: dict = {"index_fields": ["run_name", "exp_name"]}

class Person(DataPoint):       # from Notebook/<name>/, user_profiles.yaml, maintainer.yaml
    name: str
    owns: SkipValidation[Any] = None          # -> Sample
    member_of: SkipValidation[Any] = None     # -> Project/Subteam
    metadata: dict = {"index_fields": ["name"]}

# also: Project, Paper, Material, Device/Chip, MeasurementType
```

**Registry → backbone mapping (deterministic, `add_data_points`, NO LLM):**
- `Run` per `(db_path, run_id)` composite (the same key the grounding validator uses).
- `Sample` from `sample_name` + folder (`2026.05_Tip5Sample9_qcodes` → `Tip5Sample9`); edge `Run –measured_on→ Sample`.
- `Setup` from the db path (`L110 QTM`, `L206`, `L208`); edge `Run –on_setup→ Setup`.
- `MeasurementType` from `run_name`/`exp_name` (reuse the grounding validator's `_TYPE_RULES` phrase-regexes for gate-sweep/IV/photocurrent/temperature); edge `Run –is_type→ MeasurementType`.
- `Device`/`Person` from `sample_name` parsing + `Notebook/` owners + the roster configs.

**Docs → enrichment (LLM `cognify`, constrained by the ontology):** papers/repo-prose/SharePoint entities (materials, methods, people, projects) are extracted and **linked to the backbone** (a paper cites a Sample; a Person authored a Paper; a Project uses a Setup). *Verify in Phase 0:* the exact `cognify(graph_model=…)` API to pin extraction to our schema (the tutorial page was unreachable; the capability is documented in Cognee blogs, but confirm the call signature before relying on it).

> **Why seed structure (answering your Point 1):** the registry is authoritative and already typed — extracting it with an LLM would be inaccurate *and* impossibly slow at 47 tok/s. Seeding the typed backbone deterministically gives a **correct, complete skeleton**; the LLM then only has to attach the fuzzy, genuinely-unstructured knowledge to it. Best of prescribed + learned.

---

## 4. Point 2 — Continuous update + reuse the RAG layer

**Continuous update (native).** `incremental_loading=True` is the default for both `add()` and `cognify()`; two dedup layers (content-hash in `add()`, then a `pipeline_status==COMPLETED` skip in `cognify()`) mean unchanged data costs **no LLM, no re-embed, no graph write**. So re-running the pipeline over the corpus is cheap in steady state — only new/changed items pay.

**Source the conceptual corpus FROM Qdrant, not the filesystem (no CIFS/SharePoint/Docling re-read).** The RAG chunk payload already carries everything needed — verified fields: `text`, `source`, `start_line`, `chunk_type` (`code`|`prose`), `repo`. So the conceptual ingest is:
1. **Scroll Qdrant with a scope filter** (below) — a local `localhost:6333` read; no file access.
2. **Reconstruct documents:** group chunks by `source`, order by `start_line`, concatenate (dedup overlap). Reassembly restores document-level context so `cognify` extracts coherent concepts/relations, not fragments.
3. `cognee.add()` + `cognify()` the reconstructed docs.

This reuses all the expensive parsing, skips the painful CIFS/SP auth+mounts, and is **always current** (the watcher keeps Qdrant fresh) — so incremental conceptual updates = "read the changed points." For registry changes, re-run `add_data_points` (deterministic, cheap).

**Scoping = the same Qdrant filter (this is how the conceptual corpus is chosen, and the cost lever from §8):**
- `chunk_type == "prose"` — drops code deterministically (the field exists).
- **Exclude junk** — `source` patterns `site-packages`, `/venv/`, `/.env/Lib/`, `Anaconda`, `node_modules`, `/figures/` decks, raw-data dirs. *(Not hypothetical: vendored library code — mpl_toolkits, nidaqmx under `site-packages` — is currently in the index; M56 stale-index class. Scoping keeps it out of the research graph.)*
- **Allowlist research locations** — `Papers_Books/`, `Manuscripts/`, `Theses/`, research `Projects/` prose, proposals, `docs/`/READMEs, SharePoint research docs, notebook *markdown*; use the `repo` field (e.g. `confirmed_papers`). Exclude the `qcodes-runs` collection (Tier-1 anchor via `add_data_points`).
- **Optional refinement:** semantic-relevance ranking (reuse the embeddings vs research-concept seed queries) + a research-vocabulary density score for borderline docs. **Curate the base allowlist WITH the group** — they know research folders vs raw dumps.
- Same discipline as the existing full re-ingest: **memory-gated, resumable, off the llama.cpp during user hours**, plus a coverage audit.

**The honest reuse boundary (your "rely on existing RAG if possible").** Cognee reuses the **Qdrant server** and can share the **nomic embedder** (serve it OpenAI-compatible so KG vectors live in the same space) — but it writes its **own `cognee_*` collections** and embeds its **own graph nodes**; it does **not** read the existing RAG chunk-collections as-is. So:
- ✅ reuse: Qdrant instance, nomic embedding model/space, the ingest/parse output as input, the llama.cpp endpoint.
- ❌ not reused: the existing chunk *embeddings* are not Cognee's node embeddings (different granularity — chunks vs typed entities). Some vector duplication is unavoidable and acceptable; namespace `cognee_*` so it never clobbers the 15 RAG collections.

---

## 5. Point 3 — Integration into conversations

**Retrieval API.** `cognee.search(query, query_type=SearchType.GRAPH_COMPLETION)` (default; vector-hint → graph-traversal → structured context — multi-hop, unlike top-k chunks) and `INSIGHTS` (entity/relationship view). No LLM needed at query time for the traversal itself.

**How it plugs into Hermes (two complementary paths, mirroring the existing hooks):**
1. **Prefetch hook in `qnoe_rag`** (like the qcodes/find_file/RAG blocks) — for **deterministically-detected multi-hop / entity queries** ("which of my samples were measured on the setup Peio uses", "what's the latest gate-sweep for Tip5Sample9", "how do X and Y connect", "who ran the BLG cooldowns"), inject a `## Knowledge graph` context block. Deterministic detection = reliability (the pattern that works here).
2. **A `kg_search` tool** — for open-ended graph questions the model chooses to ask. (Cognee also ships an official `cognee-mcp` server as an alternative wiring.)

**Routing.** KG for **entity/relationship/multi-hop/"how are these connected"** questions; chunk-RAG for **prose/paper content**; hybrid where both help. Start KG **alongside** RAG (shadow/A-B), shift traffic where it wins.

**The payoff — KG as the grounding oracle.** Once the KG holds the authoritative entity graph, it **graduates** the grounding validator's misattribution/own-work checks from the flat SQLite registry to the KG, and it is the **precondition** to unlock Mem0 own-work facts + the SOUL-guard relaxation ([[MEMORY_ARCHITECTURE]] decisions #2/#5). This is the through-line: **the KG is what makes the whole memory architecture safe to complete.**

---

## 6. Configuration (concrete; verify starred items in Phase 0)

```env
# --- LLM (extraction for cognify) — llama.cpp OpenAI-compatible /v1 ---
LLM_PROVIDER="openai"                 # * or "ollama"; verify which cleanly targets llama.cpp
LLM_MODEL="gpt-oss-120b"              # * consider a dedicated faster/stronger EXTRACTION model
LLM_ENDPOINT="http://localhost:8000/v1"
LLM_API_KEY="sk-local"               # dummy; local endpoint
STRUCTURED_OUTPUT_FRAMEWORK="instructor"   # * instructor|BAML — constrains gpt-oss JSON
# reasoning_effort:high for cognify ONLY (per-request or dedicated batch config);
# the live gateway stays effort:low. High effort needs generous max_tokens or the
# long reasoning truncates the JSON (cf. the 512->1536 Mem0 bump).

# --- Embeddings — nomic-embed served OpenAI-compatible (share the RAG space) ---
EMBEDDING_PROVIDER="openai"          # * OpenAICompatibleEmbeddingEngine
EMBEDDING_MODEL="nomic-embed-text-v1.5"
EMBEDDING_ENDPOINT="http://localhost:<nomic-oai-port>/v1"   # * stand up an OAI-compatible embed endpoint
EMBEDDING_DIMENSIONS=768             # nomic-embed-text-v1.5

# --- Stores ---
GRAPH_DATABASE_PROVIDER="kuzu"       # embedded, on-disk; matches L5
DB_PROVIDER="sqlite"
VECTOR_DB_PROVIDER="qdrant"
VECTOR_DB_URL="http://localhost:6333"
VECTOR_DB_KEY=""
# data dirs under /opt/qnoe-agent/cognee_data/{kuzu,cognee.db}; collections namespaced cognee_*
```

**Deployment shape:**
- **Separate venv** `/opt/qnoe-agent/cognee-venv` — Cognee pulls heavy deps (instructor/BAML, kuzu, qdrant-client, docling, …); **isolate from the hermes/agent venvs** to avoid conflicts. **Pin the Cognee version** (fast-moving; v1.3.0 as of 2026-07-12).
- **Ingestion runs OUTSIDE the gateway sandbox** (like the existing watcher/nightly, as `qnoe-ai`): reads corpus, writes Kùzu/Qdrant/SQLite. Egress = localhost only (llama.cpp, Qdrant) → fits the OpenShell L7 policy; no new external hosts.
- **Gateway retrieval** reads the KG **read-only** via the search API (or `cognee-mcp` on localhost).
- **systemd/cron:** nightly incremental `cognify` + `add_data_points` refresh (same slot/discipline as the existing nightly re-ingest); memory-gated, resumable, off-peak.

---

## 7. Phased development & deployment (KG-first, retrieval-later, additive)

- **Phase 0 — Standup & the CONCEPTUAL-QUALITY gate (small but decisive).** Separate venv, pinned Cognee, local LLM+embeddings+Kùzu+Qdrant wired; `add()`→`cognify()`→`search()`. **Gate the whole plan on:** (a) fully-offline round-trip works; (b) **conceptual extraction quality** — run `cognify` on a handful of *real* group papers/proposals with the research ontology and have a human judge whether the extracted concepts/questions/techniques/relationships are *sensible and non-confabulated*, not just whether the JSON parsed. **This is the real go/no-go** — if gpt-oss-120b produces a noisy/hallucinated conceptual graph, either commit to a dedicated stronger extraction model or rethink the conceptual tier. Confirm the `cognify(graph_model=…)` signature.
- **Phase 1 — Factual anchor / scaffold (deterministic, LLM-free).** Define the ontology; load the **QCoDeS registry via `add_data_points`** → the `Sample/Run/Setup/MeasurementType/Person` skeleton the concepts attach to. Validate counts+edges against the registry (reuse the grounding validator's checks). This is the safe *scaffold* and can already back the factual grounding oracle — but it is NOT the point of the graph; the research knowledge is.
- **Phase 2 — The research/conceptual graph (the heart).** `cognify` the corpus (papers, proposals, repo prose, notebooks, SharePoint) with the research ontology; extract Concept/Question/Technique/Direction/Hypothesis/Finding and **link them to the Phase-1 anchor** (Project studies Concept, Run tests Hypothesis, Technique runs_on Setup). Every Tier-2 node carries provenance. Validate the conceptual graph (human spot-check) before it backs answers; measure latency/cost; use the dedicated extraction model if Phase-0 required it; gate list-heavy/code files. **This is where the graph earns its keep.**
- **Phase 3 — Continuous update.** Wire incremental `cognify` + `add_data_points` into the nightly/watcher (reuse ingest output); memory-gated, resumable, off-peak; coverage audit. Then run the **full-corpus** enrichment as a one-time heavy job (like the existing re-ingest).
- **Phase 4 — Chat integration + oracle graduation.** `kg_search` prefetch hook + tool; routing; shadow/A-B on multi-hop vs RAG. Then make the KG the grounding oracle → **unlock Mem0 own-work facts + the SOUL-guard relaxation** ([[MEMORY_ARCHITECTURE]]).
- **Rollback:** every phase is additive — the KG never replaces chunk-RAG until Phase 4 proves it; disable the hook/tool to fall back instantly.

---

## 8. Risks & mitigations

- **⚠️ Conceptual-tier extraction quality (THE central risk — this is the heart of the graph, not a detail).** Tier 2 (concepts/questions/techniques/directions) is entirely LLM-inferred, so its value = extraction quality. A weak model doesn't just miss things — it **confabulates concepts and relationships**, producing a "ground-truth graph" that is an LLM's guess (the R11/poisoning failure mode at graph scale). This is **framework-independent** — Cognee, LightRAG, Graphiti are all LLM-bound here — so the real lever is the **extraction MODEL + validation**, not the framework. Mitigations: (a) a **strong dedicated extraction model** is likely *required* (gpt-oss-120b @ `reasoning_effort:low` is a real doubt for nuanced conceptual relations — probe it explicitly in Phase 0 on CONCEPTUAL, not just entity, extraction); (b) instructor/BAML typed output; (c) a tight prescribed ontology to constrain what can be created; (d) **provenance on every Tier-2 node** (source doc + model) so the graph is auditable and the grounding oracle can label conceptual context as *inferred*, never assert it as fact; (e) human spot-validation of the conceptual graph before it backs answers. The factual anchor (Tier 1) is deterministic and safe regardless — but the anchor is the *scaffold*, not the point.
- **gpt-oss structured-output reliability for `cognify`** (weak local models emit malformed JSON). → instructor/BAML; validate in Phase 0/2; dedicated extraction model as the fallback.
- **Extraction throughput @47 tok/s** (LightRAG flagged the <50 tok/s timeout cliff). → structured-first minimizes extraction; nightly/off-peak; gate list-heavy/code; incremental keeps steady-state cheap.
- **Extraction effort & cost (no $ — DGX wall-clock only).** Use `reasoning_effort:high` for cognify (per-request or a dedicated batch config; live gateway stays low) — it's the cheaper experiment to try *before* a dedicated model, and batch/off-peak so it costs users no latency. High effort ≈ **3–5× the tokens/time** of low. Order-of-magnitude one-time build over the *scoped conceptual corpus* (research docs only, NOT the ~1M-chunk full corpus): ~3k chunks ≈ 3–5 days at high effort; ~10k chunks ≈ 1–2 weeks. **The dominant lever is SCOPE** (cognify only research-bearing docs — papers/proposals/notebooks/key repo prose — cutting the denominator 50–100×), plus it's one-time (incremental after is cheap) and off-peak. **Phase 0 measures the real per-paper time at effort:high and extrapolates before committing to the full run.**
- **Cognee API drift** (fast releases). → pin the version; wrap the client behind a thin `qnoe` interface so upgrades are localized.
- **Vector duplication / embedding-space match.** → share the nomic embedder (serve OAI-compatible, dim 768); namespace `cognee_*`; accept chunk-vs-entity duplication.
- **Dep conflicts.** → separate venv.
- **`cognify` custom-graph-model API unconfirmed** (docs page was down). → confirm in Phase 0 before relying on schema-constrained extraction; the deterministic backbone doesn't depend on it.

---

## 9. Open decisions (need the user)

1. **Ontology v1 scope** — which node/edge types ship first (proposal: Sample, Run, Setup, MeasurementType, Person for the backbone; add Paper, Project, Material, Device in enrichment).
2. **Extraction model** — accept gpt-oss-120b for `cognify`, or stand up a dedicated faster/stronger extraction model? (Decide after the Phase-0 reliability probe.)
3. **Integration mode** — prefetch hook, `kg_search` tool, `cognee-mcp`, or hook+tool (recommended)?
4. **KG vs RAG endgame** — KG alongside chunk-RAG indefinitely, or eventually subsume retrieval once it wins?
5. **Graph DB** — Kùzu embedded (recommended, matches L5) or Neo4j/FalkorDB if we later need concurrent multi-writer / heavier querying?

---

*Next step per project workflow: this is a plan, not an execute-now task. On approval, add a TODO execute-task for **Phase 0 + Phase 1** (standup + structured backbone — the LLM-free, highest-value core) and stop there for review before the extraction phases. Do not auto-build.*
