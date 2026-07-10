# Mem0 Integration — Per-User Memory via `qnoe_rag`

*Created: 2026-07-08*

> **Design in one line:** Drop Hermes's per-profile built-in memory (`MEMORY.md` / `USER.md`) and add **per-user** memory by calling the **Mem0 library** *inside* the existing `qnoe_rag` memory provider — so `qnoe_rag` stays the single injector and emits both RAG chunks and Mem0 user-facts.
>
> Related: [[INFERENCE_MEMORY#L3.5 — Mem0 user memory]] · [[memory/hermes-migration]] · [[memory/agent-code]]

---

## Verification status (2026-07-08) — branch `feature/mem0-per-user`

Code committed (not deployed). Validated on the DGX **without vLLM** (down for the SharePoint full sync):

| Check | Result |
|---|---|
| `mem0ai` install into `hermes-venv` | ✅ **2.0.11**, purely additive per `pip check`. **BUT** downgraded `protobuf` 7.35.1→6.33.6 (mem0 chain needs protobuf<7). No declared conflicts; `qnoe_rag` uses Qdrant over HTTP not gRPC. **Watch-item on agent restart.** |
| `episodic_memory` Qdrant collection (768-dim) + `user_id` keyword index | ✅ created, status green |
| `Memory.from_config(MEM0_CONFIG)` schema | ✅ accepted by 2.0.11 |
| Offline embedder (local nomic, `HF_HUB_OFFLINE=1`) | ✅ loads — *the scariest unknown, now cleared* |
| Write→read round-trip (`add(infer=False)` → `search`) | ✅ fact stored + retrieved (score 0.484) |
| **Per-user isolation** | ✅ other user gets 0 results — the test `USER.md` fails |

**Deferred (needs vLLM):** `add(infer=True)` LLM distillation; in-agent deploy + `USER.md` disable + restart + tool-calling-under-context check.

> ⚠️ **mem0 2.x API change (breaking vs the original design):** `search()` no longer takes `user_id=`/`limit=` — use `search(query, filters={"user_id": uid}, top_k=N)`. `add()` **does** still take `user_id=` (keyword-only). Plugin code updated accordingly.

---

## 1. Goal & final architecture

We want **two** persistent memory scopes, not three:

| Layer | Scope | Store | Injected by |
|---|---|---|---|
| RAG (dense + BM25) | Lab / sub-team | Qdrant (`group-wide`, `qtm`, …) | `qnoe_rag` |
| **Mem0 user facts** | **Per person** | Qdrant `episodic_memory` | **`qnoe_rag` (new)** |
| ~~Built-in `MEMORY.md`~~ | ~~Per profile~~ | ~~`~/.hermes/memories/`~~ | **removed** |
| ~~Built-in `USER.md`~~ | ~~Per profile~~ | ~~`~/.hermes/memories/`~~ | **removed** |

`MEMORY.md` (agent env/diary notes) is low-value for a lab RAG assistant. `USER.md` is conceptually what we want — per-person preferences — but in our setup it is **per-profile** (shared across a whole sub-team), so it cannot give real per-user memory. Mem0 replaces it, keyed on the platform `user_id`.

### Why library-inside-provider (not a Mem0 provider plugin)

Hermes allows **only one** external `memory.provider`, and `qnoe_rag` already holds that slot. Registering Mem0 as a provider would displace RAG. Instead we `pip install mem0ai` and call `Memory.from_config(...)` **from within `qnoe_rag`**. Mem0 still owns all the hard logic (LLM fact-extraction, dedup, storage) — we only write glue. No custom memory system.

---

## 2. Ground truth (verified on DGX, 2026-07-08)

- `qnoe_rag` = `/opt/qnoe-agent/hermes/plugins/qnoe_rag/__init__.py` (429 lines), subclasses `agent.memory_provider.MemoryProvider`.
- The provider interface already exposes **both** required hooks:
  - `prefetch(query, *, session_id)` → returns the string injected each turn (currently `## RAG Context …`).
  - `sync_turn(user_content, assistant_content, *, session_id, messages)` → *"async write after each turn"*. **Currently a no-op** (`# RAG is read-only`).
- `initialize(session_id, **kwargs)` kwargs include **`user_id`** (platform user id, gateway sessions) and **`user_id_alt`** (stable alternate). This is the Mem0 key.
- Embeddings: nomic-embed loaded via `SentenceTransformer("/opt/qnoe-agent/models/nomic-embed", trust_remote_code=True, device="cpu")`; offline mode enforced (`HF_HUB_OFFLINE=1`).
- Qdrant at `http://localhost:6333`. Collections present: `group-wide, photocurrent, qcodes-runs, qed, qsim, qtm, superconductivity, xchiral`. **`episodic_memory` does NOT exist yet.**
- **`mem0ai` is NOT installed** in `hermes-venv`.
- vLLM served model id: **TODO — confirm** (`curl -s localhost:8000/v1/models`; design docs assume `hermes-3-70b`). vLLM was stopped at time of writing.

---

## 3. One-time setup (run once, in order)

### 3.1 Install Mem0 into `hermes-venv`

```bash
/opt/qnoe-agent/hermes-venv/bin/pip install mem0ai
# Pin the version once it works — record it in memory/hermes-migration.md.
```

> ⚠️ Mem0's `Memory.from_config` schema has changed across releases. After install, verify the config in §4 against the installed version (`pip show mem0ai`). If the schema differs, adjust keys — the *shape* (vector_store / llm / embedder) is stable; nested key names may drift.

### 3.2 Create the `episodic_memory` collection

Mem0 can auto-create it, but we create it explicitly to control dims and avoid a first-run race:

```bash
curl -s -X PUT http://localhost:6333/collections/episodic_memory \
  -H 'Content-Type: application/json' \
  -d '{"vectors": {"size": 768, "distance": "Cosine"}}'
```

768 = nomic-embed-text-v1.5 dims. **Do not** add a `text-sparse` vector here — Mem0 is dense-only; BM25 backfill does not apply to this collection.

### 3.3 Create the `user_id` keyword index (required by Mem0 for per-user filtering)

```bash
curl -s -X PUT http://localhost:6333/collections/episodic_memory/index \
  -H 'Content-Type: application/json' \
  -d '{"field_name": "user_id", "field_schema": "keyword"}'
```

### 3.4 Confirm vLLM model id for Mem0's extraction LLM

```bash
curl -s http://localhost:8000/v1/models   # note the "id" → use in §4 mem0_config.llm.model
```

---

## 4. Mem0 config (fully local — no cloud, no `MEM0_API_KEY`)

`MEM0_API_KEY` is **platform mode only**. We use **oss/self-hosted** mode pointed at existing DGX infra:

```python
MEM0_CONFIG = {
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "collection_name": "episodic_memory",
            "host": "localhost",
            "port": 6333,
            "embedding_model_dims": 768,
        },
    },
    "llm": {
        "provider": "openai",                     # vLLM is OpenAI-compatible
        "config": {
            "model": "hermes-3-70b",              # TODO: confirm via /v1/models
            "openai_base_url": "http://localhost:8000/v1",
            "api_key": "not-needed",
            "temperature": 0.1,                   # deterministic fact extraction
            "max_tokens": 512,
        },
    },
    "embedder": {
        "provider": "huggingface",
        "config": {
            "model": "/opt/qnoe-agent/models/nomic-embed",   # local path, offline-safe
            "model_kwargs": {"trust_remote_code": True, "device": "cpu"},
        },
    },
}
```

> ⚠️ **trust_remote_code / offline:** nomic-embed needs `trust_remote_code=True` and `einops`. `einops` is already in `hermes-venv`. If Mem0's HF embedder does not forward `model_kwargs`, it will try to fetch from the Hub and fail under `HF_HUB_OFFLINE=1`. Fallback options, in order of preference: (a) upgrade/downgrade to a Mem0 version whose embedder forwards `model_kwargs`; (b) set `SENTENCE_TRANSFORMERS_HOME` / `HF_HOME` so the local nomic resolves by name; (c) last resort, a ~10-line embedder shim (still not a "memory system" — just an adapter).

> **Note — nomic loaded twice:** Mem0 loads its own `SentenceTransformer` instance of nomic, separate from `qnoe_rag`'s `_load_embed_model()` singleton. nomic-embed is small (~0.5 GB); acceptable. Do not try to share the instance — it couples the two and buys little.

---

## 5. Code changes to `qnoe_rag/__init__.py`

All additive. Five edits.

### 5.1 Add constants (near `TOP_K`, ~line 46)

```python
# --- Mem0 per-user memory ---
MEM0_ENABLED = os.environ.get("MEM0_ENABLED", "1") == "1"   # kill-switch
MEM0_TOP_K = 3                                              # facts injected per turn (~400 tok cap)
MEM0_CONFIG = { ... }                                       # from §4
```

### 5.2 Lazy Mem0 singleton (near the other `@lru_cache` loaders, ~line 90)

```python
@lru_cache(maxsize=1)
def _get_mem0():
    from mem0 import Memory
    logger.info("Initializing Mem0 (episodic_memory)")
    return Memory.from_config(MEM0_CONFIG)
```

Lazy + cached so import cost is paid once, and a Mem0/Qdrant failure at import time cannot crash plugin discovery.

### 5.3 Facts formatter (near `_format_chunks`, ~line 235)

```python
def _format_facts(facts) -> str:
    """Format Mem0 user-facts, tagged distinctly from RAG chunks."""
    items = facts.get("results", facts) if isinstance(facts, dict) else facts
    if not items:
        return ""
    lines = [f"- {m.get('memory', '')}" for m in items[:MEM0_TOP_K] if m.get("memory")]
    if not lines:
        return ""
    return "## What I remember about you\n" + "\n".join(lines) + "\n\n"
```

Distinct heading so Hermes never confuses a *user preference* with a *retrieved document*.

### 5.4 `initialize` — capture the per-user id + a session→user map

Because one provider instance can serve multiple sessions (see `on_session_switch`), do **not** trust a single `self._user_id`. Maintain a map:

```python
def __init__(self):
    ...
    self._session_users: dict[str, str] = {}

def initialize(self, session_id: str, **kwargs) -> None:
    self._profile = kwargs.get("agent_identity", "")
    self._collections = PROFILE_COLLECTIONS.get(self._profile, DEFAULT_COLLECTIONS)
    uid = kwargs.get("user_id") or kwargs.get("user_id_alt") or ""
    if uid:
        self._session_users[session_id] = uid
    logger.info("QnoeRag init profile=%s session=%s user=%s",
                self._profile, session_id, uid or "<none>")
    ...

def _uid_for(self, session_id: str) -> str:
    # Fallback: if no platform user_id was seen, key Mem0 on session_id.
    # Degrades to per-conversation memory rather than crashing.
    return self._session_users.get(session_id) or session_id
```

> **Open item:** confirm at runtime whether `user_id` also arrives in `on_session_switch(**kwargs)`. If yes, update `self._session_users` there too. Log `kwargs.keys()` in `initialize`/`on_session_switch` on first deploy to verify.

### 5.5 `prefetch` — inject Mem0 facts *ahead of* RAG (single injector)

```python
def prefetch(self, query: str, *, session_id: str = "") -> str:
    # ... existing RAG retrieval, producing `result` ...
    rag_block = f"## RAG Context\n{result}" if result else ""

    mem_block = ""
    if MEM0_ENABLED:
        try:
            uid = self._uid_for(session_id)
            # mem0 2.x: user_id in filters, count is top_k (NOT user_id=/limit=)
            facts = _get_mem0().search(query, filters={"user_id": uid}, top_k=MEM0_TOP_K)
            mem_block = _format_facts(facts)
        except Exception as e:                     # Mem0 must never break a turn
            logger.warning("Mem0 search failed: %s", e)

    return (mem_block + rag_block) or ""
```

### 5.6 `sync_turn` — the no-op becomes the write (backgrounded)

`mem0.add()` calls the LLM to distil facts → do it off the reply path in a daemon thread (same pattern as `queue_prefetch`), so it never blocks the next turn:

```python
def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None) -> None:
    if not (MEM0_ENABLED and user_content):
        return
    uid = self._uid_for(session_id)

    def _write():
        try:
            _get_mem0().add(
                [{"role": "user", "content": user_content},
                 {"role": "assistant", "content": assistant_content}],
                user_id=uid,
            )
        except Exception as e:
            logger.warning("Mem0 add failed: %s", e)

    threading.Thread(target=_write, daemon=True, name="mem0-write").start()
```

> **Cost:** each `add()` is an extra vLLM call. At our practical concurrency of ~1 user this is fine, but it competes with the reply generation of the *next* turn. If it ever causes contention, batch writes (queue N turns, distil together) or gate `add()` to turns above a length threshold.

---

## 6. Config change — drop USER.md only, keep MEMORY.md (all 3 profiles)

**Verified 2026-07-08:** built-in memory is currently **ON**. The profile configs set only `memory: {provider: qnoe_rag}`, but `load_config` deep-merges `DEFAULT_CONFIG` (`config.py:883`) under them, and `DEFAULT_CONFIG` sets `memory_enabled: True` + `user_profile_enabled: True`. So both files are loaded and injected today. To change a flag we must set it **explicitly** in the profile config (the merge otherwise keeps the default `True`).

Decision: **drop `USER.md`** (Mem0 replaces it, properly per-user) and **keep `MEMORY.md`** (static per-team seed content; not redundant with Mem0). In each `hermes/profiles/qnoe-*/config.yaml` under `memory:`:

```yaml
memory:
  provider: qnoe_rag           # unchanged — RAG + Mem0 both ride here
  user_profile_enabled: false  # drop USER.md (Mem0 owns per-user now)
  # memory_enabled: left at default True — MEMORY.md stays.
  # To also drop MEMORY.md later, add: memory_enabled: false
```

Do **not** run `hermes tools disable memory` — that also removes the memory tool ecosystem (known Hermes footgun). Use the config key above; it leaves `qnoe_rag` and `MEMORY.md` untouched.

Profiles to edit: `qnoe-orchestrator`, `qnoe-qtm`, `qnoe-photocurrent` (and the other sub-profiles once they go live).

---

## 7. Context budget impact (keep MEMORY.md, drop USER.md)

| Change | Δ tokens/turn |
|---|---|
| Keep `MEMORY.md` | 0 (unchanged) |
| Remove `USER.md` | −~500 (cap; actual = current file size) |
| Add Mem0 facts (top-3) | +~400 |
| **Net** | **≈ −100** |

Roughly token-neutral: dropping `USER.md` frees about what Mem0's facts cost. (The earlier −900 figure assumed dropping `MEMORY.md` too — that would add another ~−800 if you later choose it.) Mem0 facts are capped at `MEM0_TOP_K=3` to keep the injection bounded. The win here is **correctness** — true per-user memory — not context savings.

---

## 8. Deployment (DGX deploy pattern)

```bash
# 1. install (once)
/opt/qnoe-agent/hermes-venv/bin/pip install mem0ai

# 2. Qdrant collection + index (once) — §3.2, §3.3

# 3. deploy edited plugin
#    local edit -> scp to /tmp -> sudo cp -> chown -> chmod
scp qnoe_rag_init.py yzamir@10.3.8.21:/tmp/
sudo cp /tmp/qnoe_rag_init.py /opt/qnoe-agent/hermes/plugins/qnoe_rag/__init__.py
sudo chown qnoe-ai:qnoe-ai /opt/qnoe-agent/hermes/plugins/qnoe_rag/__init__.py
sudo chmod g+w /opt/qnoe-agent/hermes/plugins/qnoe_rag/__init__.py

# 4. edit the 3 profile config.yaml files (§6) via same pattern

# 5. restart the agent service
sudo systemctl restart qnoe-hermes.service   # confirm exact unit name first
```

(`sudo cp/chown/chmod/systemctl` are NOPASSWD; `grep/ls/find/python3` are not — use `sudo cat | grep`.)

---

## 9. Test plan

1. **Import sanity:** service starts, logs show `Initializing Mem0`, no discovery crash.
2. **Write path:** send a preference ("I prefer Python over MATLAB"). Confirm a point lands in `episodic_memory` with the right `user_id`:
   `curl -s localhost:6333/collections/episodic_memory | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"]["points_count"])'`
3. **Recall (same session):** next turn, ask something where the fact matters; confirm the `## What I remember about you` block appears in the prompt (check logs) and the answer honors it.
4. **Cross-session recall:** new conversation, same user → the fact still surfaces.
5. **Per-user isolation:** user A's fact must **not** surface for user B. This is the acceptance test that `USER.md` failed.
6. **Tool-calling intact:** verify structured tool calls still fire with the Mem0 block present (guard the 19.5K cliff).
7. **Failure isolation:** stop Qdrant briefly → turns still complete (Mem0 errors are swallowed, RAG/dense leg degrades gracefully).

---

## 10. Rollback

- Fast: set env `MEM0_ENABLED=0` and restart — disables search+add, code stays.
- Full: remove the `user_profile_enabled: false` line from the 3 configs (default `True` restores `USER.md`), revert `__init__.py`, restart.
- Data: `episodic_memory` collection can be dropped without touching RAG collections.

---

## 11. Open items / risks

Resolved during the 2026-07-08 validation:
- ✅ ~~Verify Mem0 config schema~~ — mem0ai **2.0.11** accepts it. Note the 2.x `search()` API (`filters=`/`top_k=`).
- ✅ ~~HF offline / trust_remote_code embedder~~ — loads the local nomic offline.
- ✅ ~~Store/retrieve + per-user isolation~~ — confirmed.

Still open (at deploy / needs vLLM):
1. **Confirm vLLM model id** for `MEM0_CONFIG.llm.model` (`curl localhost:8000/v1/models`) — needed for `add(infer=True)`. Override via `MEM0_LLM_MODEL` env if not `hermes-3-70b`.
2. **`add(infer=True)` end-to-end** — LLM distillation path, untested (vLLM was down).
3. **`user_id` in `on_session_switch`** — confirm via runtime log; update `_session_users` there if present (§5.4).
4. **`add()` LLM cost** under load — acceptable at ~1 concurrent user; revisit if scaling (§5.6).
5. **protobuf 7.35.1→6.33.6 downgrade** from the mem0ai install — watch agent logs on first restart (see verification table).
6. **Service unit:** `qnoe-hermes.service` (was `failed` while vLLM down; expected to recover once vLLM is up).

---

## 12. Summary of the change set

- Install `mem0ai` in `hermes-venv`. ✅ done (2.0.11)
- Create Qdrant `episodic_memory` (768-dim) + `user_id` keyword index. ✅ done
- 6 additive edits to `qnoe_rag/__init__.py` (constants, `_get_mem0`, `_format_facts`, `__init__`/`initialize`/`_uid_for`, `prefetch`, `sync_turn`). ✅ committed on `feature/mem0-per-user`
- 1 config key (`user_profile_enabled: false`) × 3 profiles — **staged, not applied** (`scripts/deploy_mem0.sh`).
- Deploy plugin + config, start vLLM, restart `qnoe-hermes.service` — **pending vLLM window** (after SP sync).
- Net context: **≈ −100 tok/turn**; the real win is **true per-user memory**.
