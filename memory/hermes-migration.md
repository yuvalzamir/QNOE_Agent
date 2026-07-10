# Hermes Agent Migration
*Last updated: 2026-07-08 — Mem0 per-user memory designed (see [[MEM0_INTEGRATION]])*

> Tracking migration from LangGraph to Hermes Agent v0.17.0.
> Full plan: [[MIGRATION_PLAN]] · Comparison: [[HERMES_AGENT_COMPARISON]] · Decision: [[memory/decisions#D4 — Replace LangGraph with Hermes Agent]]

## Status

| Phase | Description | Status |
|---|---|---|
| M1 | Install & configure | DONE |
| M2 | Create profiles | DONE |
| M3 | RAG plugin | DONE |
| M4 | QCoDeS tool | DONE |
| M5 | Teams polling adapter | DONE |
| M6 | Multi-agent routing | DONE |
| M7 | Deployment & cutover | DONE |
| M7.5 | Per-user profile routing | DONE |
| M8 | Cleanup & docs | TODO |

## Key Facts

- **Module name:** `hermes_cli` (not `hermes_agent`)
- **Entry points:** `hermes` (CLI), `hermes-agent` (headless)
- **Hermes home:** `/opt/qnoe-agent/hermes/` (set via `HERMES_HOME`)
- **Venv:** `/opt/qnoe-agent/hermes-venv/` (separate — openai version conflict)
- **Patch applied:** `MINIMUM_CONTEXT_LENGTH` 64K → 16K in `hermes-venv/.../agent/model_metadata.py`
- **Old LangGraph agent:** Killed (PID 306945), `qnoe-agent.service` disabled (2026-07-03)

## Profiles Created

- `qnoe-orchestrator` — routes to sub-agents, group-wide knowledge
- `qnoe-qtm` — Quantum Twisting Microscope team
- `qnoe-photocurrent` — Photocurrent team

Each has `SOUL.md` + `MEMORY.md` in `hermes/profiles/<name>/`.

## Gotchas

- CLI `-z` mode does NOT load profile SOUL.md. Gateway mode does (via `set_hermes_home_override`).
- MEMORY.md + USER.md are per-profile (shared within sub-team), not per-user. **Decision D13: drop both** (`memory_enabled: false`, `user_profile_enabled: false`).
- True per-user facts: **Mem0 library called inside `qnoe_rag`** (NOT as a provider — slot is taken by RAG). Designed in [[MEM0_INTEGRATION]], see [[memory/decisions#D13]]. `qnoe_rag` already has both hooks: `prefetch()` (inject) + `sync_turn()` (currently no-op → becomes `mem0.add()`). `user_id` available in `initialize()` kwargs.
- Sessions (conversation history) are per-user via gateway.
- **`tool_use_enforcement: auto` doesn't cover Hermes 3** — the hardcoded model list (`TOOL_USE_ENFORCEMENT_MODELS`) only includes GPT/Codex/Gemini/Qwen/etc. Must set `tool_use_enforcement: true` for Hermes 3 to produce structured tool calls instead of text.
- **Context bloat degrades tool calling** — at 19.5K tokens, Hermes 3 stops producing structured tool_calls even with enforcement. At 359 tokens it works perfectly. Keep context lean.

## Deployed Plugins

| Plugin | Type | Path | Notes |
|---|---|---|---|
| `qnoe_rag` | memory provider (exclusive) | `hermes/plugins/qnoe_rag/` | `memory.provider: qnoe_rag` in config. Needs `einops` in hermes-venv. |
| `qnoe_qcodes` | standalone tool | `hermes/plugins/qnoe_qcodes/` | Needs `plugins.enabled` + `qnoe-lab` toolset. DB: `$AGENT_DATA_DIR/episodic.db`. 75,994 runs. |
| `teams_polling` | platform adapter | `hermes/plugins/teams_polling/` | User plugin (loaded via symlink from profiles). Also in site-packages. |

## Plugin Architecture Notes

- User plugins go FLAT under `$HERMES_HOME/plugins/<name>/`, NOT nested
- Standalone plugins need explicit `plugins.enabled` in config.yaml
- Memory providers are "exclusive" — activated via `memory.provider` config, not `plugins.enabled`
- Plugin discovery scans `get_hermes_home() / "plugins"` — which is the PROFILE dir at runtime
- Each profile needs `plugins/` symlink → `hermes/plugins/` for discovery to work
- Discovery runs once at startup (cached via `_discovered = True`) — not re-run under `_profile_runtime_scope`

## Per-User Profile Routing (DONE — 2026-07-03)

**Approach:** Adapter-side profile stamping + gateway credential dedup.

**How it works:**
1. `multiplex_profiles: true` at top level of config.yaml (NOT under `gateway:`)
2. `teams_polling` adapter loads `user_profiles.yaml` mapping
3. For each message, `_resolve_profile()` looks up sender by ID then by display name
4. `source.profile` set on `SessionSource` → gateway routes to correct profile
5. `self.bot_token = self._username` exposes credential for `_adapter_credential_fingerprint()` — prevents multiplexer from creating duplicate adapters

**Duplicate response fix (the hard part):**
Three independent causes had to be resolved:
1. **Old LangGraph agent** polling same Teams bot → killed + disabled
2. **Plugin auto-enable** in `gateway/config.py` overrides `enabled: false` → handled by credential dedup (no gateway patch needed)
3. **"default" profile** in `profiles_to_serve()` creates extra adapter → handled by same credential dedup

The `bot_token` approach is the ONLY fix that survives Hermes upgrades. All gateway patches were reverted.

**Sub-profile config:** Each sub-profile has its own `config.yaml` (standalone, NOT symlinked) with `gateway.platforms.teams_polling.enabled: false` and `memory.provider: qnoe_rag`.

**Mapped users (2026-07-03):**
- `ef6f38c9-...` Alexander Rothstein → qnoe-qtm
- Default (unmapped) → qnoe-orchestrator
- Yuval Zamir temporarily removed from mapping for testing

**Verified (2026-07-03):**
- Single response per message: PASS
- QTM personality for mapped user: PASS
- RAG collections correct (qtm, group-wide, qcodes-runs): PASS
- RAG prefetch injects context: PASS
- Unmapped user → orchestrator: PASS
- Tool calling: FAIL (Hermes 3 70B outputs tool calls as text — known issue, in TODO)

## Context Optimisation (I1) — 2026-07-03

**Goal:** reduce fixed per-turn token overhead to allow more turns before compaction.

**Applied (all 3 profiles):**
- `compression.threshold: 0.75` — compacts at ~24K not ~16K
- `tool_use_enforcement: true` — structured tool calls for Hermes 3
- `tools.tool_search.enabled: 'on'` — defers non-core plugin tools
- `disabled_toolsets: [tts, session_search, todo, cronjob, delegation, image_gen]` — saves ~3,351 tokens
- Orchestrator SOUL.md: 817→423 words (removed delegation context blocks, code examples, failure handling section)
- RAG `TOP_K`: 5→3 — saves ~1,200 tokens/turn

**Token floor (QTM, fresh session, after changes):**
Tools ~6,905 · RAG ~3,600 · SOUL ~720 · framing ~500 = **~11,725 tokens**
(Was ~17,015 before — saved ~5,290 tokens)

**Architecture clarification — SOUL.md scope:**
- Only ONE SOUL.md is ever in context per turn — the active profile's
- Orchestrator and sub-profile SOULs are mutually exclusive
- Sub-profiles (qtm, photocurrent) do NOT need the orchestrator's directory tree — they already have their own file access section

**RAG architecture:**
- 3 chunks returned across ALL collections combined (not per collection)
- QTM collections: `qtm`, `group-wide`, `qcodes-runs`
- Cross-encoder picks best 3 regardless of source collection

**Next options:**
- Tool Slimmer (alias8818/hermes-tool-slimmer v0.6.5) — last tested Hermes v0.15.0, we're on v0.17.0, compatibility unconfirmed. Cannot run alongside native Tool Search.
- Dedicated embedding microservice — prevent nomic-embed eviction under memory pressure

## Known Issues

- **Tool calling as text:** FIXED — set `tool_use_enforcement: true` (see M23 in mistakes.md)
- **Duplicate response errors at startup:** Hermes v0.17.0 now refuses duplicate adapter credentials with ERROR log instead of starting them. Service still runs with 1 platform. Errors are harmless — log noise only.
