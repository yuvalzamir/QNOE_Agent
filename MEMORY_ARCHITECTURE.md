# Memory Architecture — Cognee corpus KG + Mem0 personalization, and the boundary between them

*Created: 2026-07-17. Status: **design proposal** — no decision executed, nothing deployed.*

> Related: [[MEM0_HYGIENE_OPTIONS]] · [[MEM0_INTEGRATION]] · [[memory/decisions#D13]] · [[memory/mistakes#M47]] · [[memory/mistakes#M55]] · `CLAUDE.md` §"Memory layers L1–L5"

---

## Part 1 — Two-layer architecture

The future memory system splits into two stores with **different owners of truth**, plus the existing output guard:

| Layer | Holds | Source of truth | Scope | Maps to today's |
|---|---|---|---|---|
| **Cognee** — corpus knowledge graph | the group's corpus as entities+relationships (samples, runs, devices, papers, people, setups, projects) | the **data** (registry, repos, papers, SharePoint) | shared, group-visible (with ACLs) | L1 dense RAG + L2 BM25 + the deferred **L5 KùzuDB KG** — they *converge* into Cognee |
| **Mem0** — per-user personalization | the user's own context: what they work on, prefer, are interested in | the **user** (first-party) | per-user, private | L3.5 Mem0 (thinned — no more lab-fact payloads) |
| **Grounding validator** (already built) | — | the registry/manifests | output-time | unchanged; guards every reply |

**Cognee does not replace Mem0.** Per the neutral comparisons, they solve *fundamentally different problems*: Cognee builds a structured KG over a **corpus** ("how do entities connect"); Mem0 stores **per-user** facts ("what does this user prefer"). In *our* stack the layer Cognee actually absorbs is **retrieval + the planned KG (L1/L2/L5)**, not Mem0.

**Why it still fixes the Mem0 problem (M47/M55).** The poisoning happened because Mem0 was storing *lab facts* (runs, params) distilled from assistant replies. Once Cognee holds the authoritative, source-grounded corpus KG, per-user memory **no longer needs to store lab-fact payloads at all** — it shrinks to genuine personal context, a tiny low-risk surface. The dangerous content leaves the distillation loop.

### Phased migration (KG-first, retrieval-later)

We already run a mature hybrid RAG (dense + BM25 + cross-encoder, 10 collections, ~1M chunks). Cognee is **not** a one-day drop-in for that. Sequence:

- **A — Stand up, small slice.** Cognee with an **embedded Kùzu** graph (on-disk, no service — matches the L5 Kùzu intent) + **reuse the existing Qdrant** as the vector store (Cognee has a Qdrant adapter) + the local llama.cpp `/v1` endpoint. Cognify a *small* slice (the QCoDeS registry + one subteam's repos). Validate KG quality and recall.
- **B — Shadow.** Run Cognee graph-recall *alongside* the current RAG; A/B on multi-hop questions ("which of my samples were measured on the setup Peio also used"), where graph recall should beat chunk recall.
- **C — Expand + shift.** Grow corpus coverage; route retrieval to Cognee where it wins; keep dense/BM25 for pure lexical until parity.
- **D — Thin Mem0.** Drop lab-fact storage from Mem0; implement the first-party/third-party write gate (Part 2). Grounding validator stays throughout.

### DGX footprint / cost

- **Graph DB:** Kùzu is embedded (a directory on disk) — near-zero service overhead. Neo4j/FalkorDB would be a Docker service (heavier; only if we outgrow Kùzu).
- **Vector:** reuse Qdrant — no second vector store.
- **The real cost is `cognify`:** entity/edge extraction makes **LLM calls per document** on the local model. Over ~1M chunks the full build is a heavy one-time job — treat it exactly like the existing full re-ingest: **memory-gated, resumable, off the llama.cpp during user hours, nightly/off-peak**. Incremental updates thereafter are cheap.

---

## Part 2 — The knowledge boundary: first-party vs third-party

### The real dividing axis (a reframe)

The tempting line is *subjective (Mem0) vs objective (Cognee)*. **That line is wrong**, and the user spotted why: facts about one's own work are *objective lab facts* that we nonetheless want in Mem0. The true axis is:

> **First-party** — the user is a *principal* in the work (they did it / own the sample / run the project) → the user is an authoritative source → **Mem0** (+ Cognee once ingested).
> **Third-party** — the user is an *observer* of others'/the group's work they merely ask or talk about → the **corpus** is the authoritative source → **Cognee only**.

Ownership, not subjectivity, is the boundary. This is exactly what the user articulated: *"Mem0 holds information about the user's own work — work they are directly involved in… work of the group that the user asks about or talks about but is not directly theirs should not be fed to their Mem0."*

### The core rule + one principle that resolves most cases

1. **Write-gate on first-party involvement.** A turn writes to Mem0 only if the user is a principal in the work being described (first-person authorship: "I measured…", "my sample…", "our team's project…"), or it is pure personalization (a preference/interest). Third-party talk ("what does the group have…", "who works on…", "run 999999?") writes **nothing** — this alone kills the M55 query-log class.
2. **Store the POINTER in Mem0, the PAYLOAD in Cognee.** Mem0 holds *"user works on X / owns sample Z / is interested in topic T / thinks run R was a bad cooldown."* The actual content and relationships of X/Z/T/R live in Cognee. Interests and dependencies are pointers *at* corpus entities, not copies of them.

### Examples (where each utterance goes)

| User says | First-party? | → Mem0 (pointer / personal) | → Cognee (payload) | Note |
|---|---|---|---|---|
| "My sample is Tip5Sample9; I'm doing gate sweeps." | yes | `focus: gate sweeps on Tip5Sample9` | (already has Tip5 runs) | dual; user authoritative on *focus* |
| "I just cooled down Tip9, first data tomorrow." | yes | `in-flight: Tip9 cooldown (~date)` | not yet ingested | **write-ahead** — expire when Cognee catches up |
| "Remember I like answers in bullet points." | pref | `pref: bullet-point answers` | — | pure personalization |
| "What gate sweeps does the group have on BLG?" | no (asking) | *nothing* | answers from KG | a *query*, not a fact — M55 killer |
| "I'm building on Peio's cavity-QED result for my project." | mixed | `interest: cavity-QED; my project depends on it` | Peio's result lives here | store the **interest**, not Peio's data |
| "My run 848 measured 1.2 nA." | yes, but a **verifiable record** | (at most `references run 848`) | 848's real params — authoritative | **Cognee wins on the value** (see contradiction #2) |
| "I think run 852 was a bad cooldown." | yes (judgment on own work) | `annotation: user flagged 852 as bad cooldown` | 852's record | subjective *annotation* of own work → legit Mem0, not in Cognee |
| "Who in the group does MoO₃ hyperbolics?" | no | *nothing* | KG answers | third-party query |

The last two are the instructive ones: a **subjective annotation on your own record** (bad cooldown) is legitimately Mem0-only content Cognee won't have; a **verifiable value of your own record** (the 1.2 nA) is Cognee's even though it's your work.

---

## Part 3 — Edge cases & contradictions (the hard part)

### ⚠️ Contradiction #2 is the dangerous one — first-party authority vs source-of-truth

The rule says the user is *authoritative about their own work*. But a user can be **authoritative and wrong**: "my last run was 848, a gate sweep" when the registry shows their last run was 852, an IV. If Mem0 trusts the user unconditionally, it stores and later re-asserts a false record — **self-poisoning**, a fresh M47 with the user (not the assistant) as the confabulation source.

**Resolution — split intent from record even for own work:**
- The user IS authoritative about **intent, priority, opinion, focus** ("I'm working on gate sweeps of Tip5", "852 was a bad cooldown") → Mem0 trusts these.
- The user is NOT the authority on **verifiable records** (which run #, which params, which date) — those defer to Cognee/registry **even for their own work**. On any retrieval of a verifiable value, **Cognee wins on conflict** and the divergence is flagged.

This preserves "user owns the framing" while blocking self-poisoning. Note it **directly contradicts the currently-deployed SOUL guard** ("memory is NOT a source for objective lab records… never launder a previous answer through memory"). Adopting first-party own-work facts means **relaxing that guard for first-party intent/opinion only** — a deliberate, scoped policy change, not a loophole. Call it out before deploying.

### The other contradictions & edge cases

1. **"Objective ⊄ Mem0" breaks.** Own-work facts are objective lab facts, so the subjective/objective taxonomy fails → use first-party/third-party (already reframed above).
3. **Interests point at non-owned work.** "I follow the group's BLG results" concerns work that isn't the user's, yet is a real preference. The naive "non-owned → not Mem0" would wrongly drop it. → Store the **interest-pointer** (user cares about T), never the payload (T's results stay in Cognee). Separate the *object* of interest from the *fact* of interest.
4. **Relevance ≠ ownership.** Others' work is often essential context to mine ("my project extends Peio's measurement"). → Mem0 stores *my dependence/interest as a pointer*; the borrowed fact stays in Cognee. Don't copy Peio's data into my Mem0 just because it's relevant to me.
5. **Ownership is graded and shifting.** Co-authorship, supervision, *sample*-owner vs *measurement*-runner, project hand-offs. "Directly involved" is a spectrum, not a bit. → Define an **ownership predicate** (proposal: *active contributor* — performed it, or owns the sample/device, or is a named project member — not mere awareness) and attach **temporal decay** (own-work focus goes stale when the user moves on).
6. **"We/our" is ambiguous** — "our project" = my subteam (owned) or the whole group (not owned)? → Disambiguate with the user's **profile/team** (they're routed to a subteam already): "our" scoped to their subteam = first-party; group-wide "we have…" = third-party.
7. **Visibility couples to ownership.** An in-flight Mem0 fact ("my unpublished result") is **private**; promoting it to Cognee makes it **group-visible**. So the ownership line is also a **privacy line** — and Cognee itself needs per-subteam ACLs (we already hit the CavityQED ACL gap). Promotion from Mem0→Cognee is a visibility decision, not automatic.
8. **The corpus lags reality.** Mem0 legitimately holds the user's work *before* ingestion (write-ahead), then must **reconcile**: once Cognee ingests the real record, the Mem0 copy is redundant and risks going stale → expire/verify Mem0 own-work records against Cognee on a schedule (the nightly audit from [[MEM0_HYGIENE_OPTIONS]] Option 4, now with a source of truth to check against).
9. **Write-time classification is itself the hard problem.** Deciding "is this the user's own work?" at write time needs a classifier. Signals: user identity/team + first-person framing ("I/my" + own-setup nouns) vs third-person ("the group's", a colleague's name, "what do we have"). Deterministic-first (the pattern that has worked here), LLM-gate only as backstop.

---

## Part 4 — Synthesis / reconciliation rules

- **Dual-representation is fine; roles differ.** Own-work facts may live in both stores: Cognee as the grounded record, Mem0 as *"this is mine / my current focus."* On a verifiable value, Cognee wins; Mem0 supplies framing and priority.
- **Pointer in Mem0, payload in Cognee** (the load-bearing principle).
- **Cognee wins on conflict for verifiable records; Mem0 wins for intent/opinion/preference.**
- **Write-ahead + expiry:** Mem0 may lead the corpus for in-flight work, but reconciles when Cognee ingests it.
- **Ownership = visibility:** Mem0 private; promotion to Cognee = group-visible under ACLs.
- **Grounding validator unchanged** — still the output backstop for anything either layer gets wrong.

---

## Part 5 — Decisions (resolved 2026-07-17)

1. **Ownership predicate = union.** First-party if the user satisfies **any one** of: performed the measurement, owns the sample/device, or is a named project member. Mere awareness or supervision alone does **not** qualify unless it also meets one of these (e.g. a supervisor who is a named project member counts).
2. **Soft rule + in-conversation contradiction alert.** Store the user's own-work claim with `source: user-claim` + provenance; on divergence from the authoritative record, the record (Cognee/registry) wins **and the agent tells the user in the same turn** ("you said X, but the record shows Y") — the contradiction is surfaced, not silently overridden.
3. **Ask when "we/our" is ambiguous** (don't silently assume subteam).
4. **Mem0 TTL = timer AND factual change.** Own-work "focus" facts expire on a timer *or* when a superseding fact arrives, whichever first.
5. **SOUL guard relaxation only after Cognee ships.** Until then the deployed "memory is not a source for verifiable lab records" guard stays as-is.

---

## Part 6 — Interim: de-risk Mem0 NOW, before Cognee

**The binding constraint (from decision #5 + contradiction #2):** Cognee is the oracle that makes own-work facts safe — it's the authoritative store the soft rule defers to, and its existence is the precondition for relaxing the SOUL guard. **Until Cognee ships there is no fallback source of truth**, so we must **NOT** start storing own-work *verifiable* facts in Mem0 yet — that would be self-poisoning with nothing to catch it. Interim, Mem0 stays scoped to **personal preferences + interest pointers** (today's SOUL scope) — but *enforced at write time* instead of hoped-for via prompt.

**Do now — durable, survives into Cognee (this IS the Mem0 half of the final design, not throwaway):**

1. **Provenance metadata on every `add()`** — `source` (user-stated vs assistant-distilled), verbatim source message, session. Required by the soft rule (#2) regardless; ends the nuclear-wipe failure mode immediately (purge becomes a `qdrant delete` by filter); a metadata dict on the existing call, near-zero cost. *(= MEM0_HYGIENE_OPTIONS Option 3.)*
   - **Implemented 2026-07-17** in `qnoe_rag.sync_turn`: `metadata={source:"user-stated", src_msg:<verbatim ≤500ch>, session, prov_v:1}`. mem0 2.0.11 `add()` confirmed to accept + persist `metadata` (and natively supports `expiration_date` in metadata — reserved for the decision-#4 TTL). DEPLOY + live-verify (a real turn → stored fact carries the tags; purge-by-filter works) deferred until the gateway/LLM is back up.
2. **Deterministic first-party / third-party write gate in `sync_turn`** — before `add()`, drop third-party turns (queries/"what do we have"/run-id lookups/colleague-named talk) and lab-fact-looking content; keep only personal prefs + interest pointers. Kills the M55 query-log class at the source. Reuse the existing run-id / find_file intent regexes. *(= Option 2, deterministic tier, now with the first-party definition from Part 2.)*
   - **Resolved 2026-07-17: LLM-extract → deterministic fact-level POST-filter (a hook), not prompt-only, and not a pre-gate.** Refined from the user's insight that classifying a raw multi-intent *turn* deterministically is hard, but classifying an *extracted, atomic fact* is easy. Flow: let Mem0's built-in extraction (the prompt) surface candidate facts — it's good at the messy NLU of "is there a durable fact here?"; then a **deterministic classifier inspects each returned fact and deletes the third-party ones** before they can be retrieved. `add(infer=True, metadata=…)` returns `{"results":[{"id","memory","event"}]}`; for each `event=="ADD"` whose `memory` text the classifier flags third-party (query-log phrasing "user asked/wants to know about…", a colleague/roster name, "the group/lab", a run/sample the user doesn't own), call `delete(id)`. Runs in the existing daemon write thread, off the reply path, before any retrieval — the transient store→delete is invisible.
     - **Why this beats the pre-gate:** the LLM does the hard NLU; the deterministic part shrinks to scoring a short canonical string (unit-testable — feed fact strings, assert keep/drop, like the grounding suite). **Reliability is fail-safe:** if extraction over-surfaces, the deterministic filter catches the junk; if it under-surfaces, we only lose a nicety. The safety guarantee (no third-party/lab-fact stored) stays deterministic — so it honors "never prompt-only for the guarantee" while offloading understanding to the prompt. It's also finer-grained: keeps the good fact from a mixed turn while dropping the bad one.
     - **Caveats:** only auto-delete `event=="ADD"`; an `UPDATE`/`DELETE` means Mem0 merged the new bit into an existing fact — deleting would take the good one too, so route those to the nightly audit instead. The classifier must keep "user is interested in X" (a wanted pointer) while dropping "user asked what X is" (a query) — distilled phrasing makes this separable but it's the part needing care. Optionally tune `custom_fact_extraction_prompt` toward durable prefs/interests later to reduce what the filter must catch; start with Mem0's default extraction. *(Status: approach chosen; implementation pending go-ahead — deterministic classifier + offline unit tests.)*
3. **Grounding validator stays the output oracle** (already shipped) — its misattribution/mistyped footer already *is* the interim "contradiction alert" (registry as oracle). Optionally add a light nightly audit that reuses the validator's registry helpers (`_run_in_db`, etc.) to flag any Mem0 fact that looks like a lab-record or query-log.

**Defer to Cognee:** storing own-work verifiable facts in Mem0; the SOUL-guard relaxation; the full oracle-audit (registry → KG); temporal/graph reasoning. The in-conversation contradiction alert graduates from "registry footer" to "KG-backed, per decision #2" once Cognee is the oracle.

**Why this isn't wasted work:** Mem0 survives the Cognee migration as the *personalization layer*. Provenance tags + the first-party write gate are exactly its final-state properties — we're building the Mem0 half of the target architecture now, and letting Cognee fill in the corpus half later.

---

*Next step per project workflow: the interim (Part 6, items 1–2) is small and Cognee-independent — a candidate execute-task on its own. The Cognee migration (Part 1 Phase A) stays a design doc until direction is approved; then add a TODO execute-task — do not auto-build.*
