"""QNOE RAG memory provider plugin for Hermes Agent.

Provides Qdrant-based retrieval-augmented generation with nomic-embed
embeddings and cross-encoder reranking. Integrates as a Hermes
MemoryProvider so RAG context is injected automatically every turn,
and also exposes an explicit ``rag_search`` tool the agent can call.

Collection routing per profile:
  qnoe-orchestrator  -> all collections
  qnoe-qtm           -> qtm, group-wide, qcodes-runs
  qnoe-photocurrent  -> photocurrent, group-wide, qcodes-runs
  (other)             -> group-wide, qcodes-runs
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
from functools import lru_cache
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment / paths
# ---------------------------------------------------------------------------

os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
EMBED_MODEL_PATH = os.environ.get(
    "EMBED_MODEL_PATH", "/opt/qnoe-agent/models/nomic-embed"
)
RERANK_MODEL_PATH = os.environ.get(
    "RERANK_MODEL_PATH", "/opt/qnoe-agent/models/cross-encoder-msmarco"
)

# 3 while the 32K window forced a tight budget; 5 since the 64K upgrade
# (2026-07-10). Env-overridable for quick experiments.
TOP_K = int(os.environ.get("RAG_TOP_K", "5"))
TOP_K_PER_COLLECTION = 20
RERANK_POOL = 20
RERANK_THRESHOLD = 0.5

# Profile name -> list of Qdrant collections to search
ALL_COLLECTIONS = [
    "group-wide", "qtm", "photocurrent", "qed",
    "superconductivity", "qsim", "xchiral", "qcodes-runs",
]

PROFILE_COLLECTIONS: Dict[str, List[str]] = {
    "qnoe-orchestrator": ALL_COLLECTIONS,
    "qnoe-qtm": ["qtm", "group-wide", "qcodes-runs"],
    "qnoe-photocurrent": ["photocurrent", "group-wide", "qcodes-runs"],
    "qnoe-qed": ["qed", "group-wide", "qcodes-runs"],
    "qnoe-superconductivity": ["superconductivity", "group-wide", "qcodes-runs"],
    "qnoe-qsim": ["qsim", "group-wide", "qcodes-runs"],
    "qnoe-xchiral": ["xchiral", "group-wide", "qcodes-runs"],
}

DEFAULT_COLLECTIONS = ["group-wide", "qcodes-runs"]

# ---------------------------------------------------------------------------
# Mem0 per-user memory (library-in-provider; see MEM0_INTEGRATION.md / D13)
# ---------------------------------------------------------------------------
# Distilled per-user facts, keyed on the platform user_id, stored in a
# dedicated Qdrant collection. RAG stays the single injector: prefetch()
# emits these facts ahead of RAG chunks; sync_turn() writes new ones.

MEM0_ENABLED = os.environ.get("MEM0_ENABLED", "1") == "1"   # kill-switch
MEM0_TOP_K = 3                                              # facts injected per turn
MEM0_COLLECTION = "episodic_memory"
# vLLM served model id used by Mem0 for fact extraction — confirm via
# `curl localhost:8000/v1/models`; override with MEM0_LLM_MODEL if it differs.
MEM0_LLM_MODEL = os.environ.get("MEM0_LLM_MODEL", "hermes-3-70b")
VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")

# Derive Mem0's Qdrant target from QDRANT_URL instead of hardcoding localhost:
# inside the OpenShell sandbox (B7-OS) Qdrant is only reachable via
# host.openshell.internal through the L7 proxy — a hardcoded localhost bypasses
# the proxy (NO_PROXY) and dies with ECONNREFUSED (Mem0 add/search failed).
from urllib.parse import urlparse as _urlparse
_QDRANT_PARSED = _urlparse(QDRANT_URL)

MEM0_CONFIG = {
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "collection_name": MEM0_COLLECTION,
            "host": _QDRANT_PARSED.hostname or "localhost",
            "port": _QDRANT_PARSED.port or 6333,
            "embedding_model_dims": 768,
        },
    },
    "llm": {
        "provider": "openai",                     # vLLM is OpenAI-compatible
        "config": {
            "model": MEM0_LLM_MODEL,
            "openai_base_url": VLLM_BASE_URL,
            "api_key": "not-needed",
            "temperature": 0.1,
            # 1536, not 512: gpt-oss spends output tokens on reasoning before
            # the JSON; at 512 the JSON gets truncated ("Error parsing
            # extraction response", 2026-07-10).
            "max_tokens": 1536,
        },
    },
    "embedder": {
        "provider": "huggingface",
        "config": {
            # Local path + offline-safe; matches qnoe_rag's own nomic loader.
            "model": EMBED_MODEL_PATH,
            "model_kwargs": {"trust_remote_code": True, "device": "cpu"},
        },
    },
}

# ---------------------------------------------------------------------------
# Model loading (cached singletons)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_embed_model():
    from sentence_transformers import SentenceTransformer

    logger.info("Loading nomic-embed from %s", EMBED_MODEL_PATH)
    return SentenceTransformer(
        EMBED_MODEL_PATH, trust_remote_code=True, device="cpu"
    )


@lru_cache(maxsize=1)
def _load_reranker():
    from sentence_transformers import CrossEncoder

    logger.info("Loading cross-encoder from %s", RERANK_MODEL_PATH)
    return CrossEncoder(RERANK_MODEL_PATH, device="cpu")


@lru_cache(maxsize=1)
def _get_qdrant():
    from qdrant_client import AsyncQdrantClient

    return AsyncQdrantClient(url=QDRANT_URL)


@lru_cache(maxsize=1)
def _load_sparse_model():
    from fastembed import SparseTextEmbedding

    return SparseTextEmbedding(model_name="Qdrant/bm25")


@lru_cache(maxsize=1)
def _get_mem0():
    """Lazy Mem0 singleton. Lazy + cached so import cost is paid once and a
    Mem0/Qdrant failure cannot crash plugin discovery at startup."""
    from mem0 import Memory

    logger.info("Initializing Mem0 (%s)", MEM0_COLLECTION)
    return Memory.from_config(MEM0_CONFIG)


def _embed_sparse_query(text: str):
    return next(iter(_load_sparse_model().embed([text])))


# ---------------------------------------------------------------------------
# Retrieval helpers
# ---------------------------------------------------------------------------


def _embed_query(text: str) -> list[float]:
    model = _load_embed_model()
    return model.encode(
        f"search_query: {text}", normalize_embeddings=True
    ).tolist()


def _rerank(query: str, chunks: list[dict], top_k: int = TOP_K) -> list[dict]:
    if not chunks:
        return []
    reranker = _load_reranker()
    pairs = [(query, c["text"]) for c in chunks]
    scores = reranker.predict(pairs)
    for chunk, score in zip(chunks, scores):
        chunk["rerank_score"] = float(score)
    ranked = sorted(chunks, key=lambda c: c["rerank_score"], reverse=True)
    return ranked[:top_k]


def _score_to_chunk(point, collection: str) -> dict:
    payload = point.payload or {}
    return {
        "score": point.score,
        "collection": collection,
        "text": payload.get("text", ""),
        "source": payload.get("source", ""),
        "repo": payload.get("repo", ""),
        "chunk_type": payload.get("chunk_type", "prose"),
    }


async def _retrieve(query: str, collections: list[str]) -> list[dict]:
    """Hybrid (dense + BM25 sparse) retrieval across collections with RRF and reranking."""
    if not collections:
        return []

    from qdrant_client.models import Prefetch, FusionQuery, Fusion, SparseVector

    loop = asyncio.get_running_loop()
    dense_vec, sparse_emb = await asyncio.gather(
        loop.run_in_executor(None, _embed_query, query),
        loop.run_in_executor(None, _embed_sparse_query, query),
    )
    qdrant = _get_qdrant()

    async def _query_one(coll: str) -> list[dict]:
        try:
            result = await qdrant.query_points(
                collection_name=coll,
                prefetch=[
                    Prefetch(query=dense_vec, limit=TOP_K_PER_COLLECTION),
                    Prefetch(
                        query=SparseVector(
                            indices=sparse_emb.indices.tolist(),
                            values=sparse_emb.values.tolist(),
                        ),
                        using="text-sparse",
                        limit=TOP_K_PER_COLLECTION,
                    ),
                ],
                query=FusionQuery(fusion=Fusion.RRF),
                limit=TOP_K_PER_COLLECTION,
                with_payload=True,
            )
            return [_score_to_chunk(h, coll) for h in result.points]
        except Exception as exc:
            logger.warning("Qdrant hybrid search failed for %s: %s", coll, exc)
            return []

    per_collection = await asyncio.gather(
        *(_query_one(c) for c in collections)
    )
    all_results = [chunk for batch in per_collection for chunk in batch]

    if not all_results:
        return []

    # Deduplicate by content only — the same document often exists under
    # several sources (server path, SharePoint URL variants, backup copies),
    # and source-keyed dedup let 3 copies of one paragraph fill the top-5
    # (2026-07-10 QTM band-structure failure).
    seen: set[str] = set()
    deduped: list[dict] = []
    for chunk in sorted(all_results, key=lambda c: c["score"], reverse=True):
        key = " ".join(chunk["text"][:200].split())
        if key not in seen:
            seen.add(key)
            deduped.append(chunk)

    pool = deduped[:RERANK_POOL]
    top = await loop.run_in_executor(None, _rerank, query, pool, TOP_K)

    if not top or top[0].get("rerank_score", 0) < RERANK_THRESHOLD:
        return []

    # Anti-lost-in-middle ordering
    if len(top) >= 2:
        return [top[0]] + top[2:] + [top[1]]
    return top


def _run_retrieve(query: str, collections: list[str]) -> list[dict]:
    """Synchronous wrapper around the async retrieval."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Already in an async context — run in a new thread with its own loop
        result: list[dict] = []

        def _worker():
            nonlocal result
            result = asyncio.run(_retrieve(query, collections))

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(timeout=30)
        return result
    else:
        return asyncio.run(_retrieve(query, collections))


def _format_chunks(chunks: list[dict]) -> str:
    """Format retrieved chunks as context text."""
    if not chunks:
        return ""
    lines = []
    for i, c in enumerate(chunks, 1):
        source = c.get("source", "unknown")
        coll = c.get("collection", "")
        score = c.get("rerank_score", c.get("score", 0))
        lines.append(f"[{i}] ({coll}) {source} (score: {score:.2f})")
        lines.append(c["text"])
        lines.append("")
    return "\n".join(lines)


def _format_facts(facts) -> str:
    """Format Mem0 user-facts, tagged distinctly from RAG chunks so the
    model never confuses a user preference with a retrieved document."""
    items = facts.get("results", facts) if isinstance(facts, dict) else facts
    if not items:
        return ""
    lines = [f"- {m.get('memory', '')}" for m in items[:MEM0_TOP_K] if m.get("memory")]
    if not lines:
        return ""
    return "## What I remember about you\n" + "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# QCoDeS registry hook
# ---------------------------------------------------------------------------
# Deterministic: when the user asks about a specific QCoDeS run id, look it up
# in the registry SQLite directly and inject the authoritative answer —
# including an explicit "does not exist" — so the model cannot confabulate run
# details from semantically similar RAG chunks (memory/mistakes.md M38).

QCODES_REGISTRY_DBS = [
    os.path.join(
        os.environ.get("AGENT_DATA_DIR", "/home/yzamir/qnoe_server_data"),
        "episodic.db",
    ),
    "/opt/qnoe-agent/memory/episodic.db",
]
# Matches run 159 / run_159 / run#159 / run159 AND "run with ID 159",
# "run id 159", "run number 159", "run no. 159" (red-team R5, 2026-07-14 —
# the narrow form missed "run with ID N" so the hook didn't fire and the
# agent answered from RAG with a wrong count).
_RUN_ID_RE = re.compile(
    r"\brun[\s_]*(?:with[\s_]+)?(?:id|number|no\.?|#)?[\s_#:]*(\d{1,7})\b",
    re.IGNORECASE,
)
_QCODES_HINT_RE = re.compile(
    r"qcodes|measur|dataset|\brun\b", re.IGNORECASE
)


def _qcodes_registry_block(message: str) -> str:
    if not message or not _QCODES_HINT_RE.search(message):
        return ""
    m = _RUN_ID_RE.search(message)
    if not m:
        return ""
    run_id = int(m.group(1))
    rows: list[tuple] = []
    total = 0
    searched = False
    for db in QCODES_REGISTRY_DBS:
        if not os.path.exists(db):
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            try:
                total += con.execute(
                    "SELECT COUNT(*) FROM qcodes_registry WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
                cur = con.execute(
                    "SELECT db_path, run_id, run_name, sample_name, "
                    "parameters, completed_timestamp FROM qcodes_registry "
                    "WHERE run_id=? LIMIT 5",
                    (run_id,),
                )
                rows.extend(cur.fetchall())
                searched = True
            finally:
                con.close()
        except Exception as exc:
            logger.warning("QCoDeS registry lookup failed for %s: %s", db, exc)
    if not searched:
        return ""
    header = (
        "## QCoDeS registry lookup (authoritative — trust this over RAG chunks)\n"
    )
    if not rows:
        return header + (
            f"No run with run_id {run_id} exists in the QCoDeS registry. "
            "Tell the user this run does not exist; do NOT invent run details. "
            "Run ids are per-database — if the user means a specific database, "
            "ask which one or use the QCoDeS tools (find them via tool_search).\n\n"
        )
    shown = rows[:8]
    lines = [
        header,
        f"Run id {run_id} exists in {total} indexed database(s) "
        f"(run ids are per-database, so these are unrelated measurements). "
        f"Showing {len(shown)} of {total} — tell the user the TOTAL count and "
        "that the list below is a sample; offer to narrow by project or "
        "database. Do not present this sample as the complete list, and do "
        "not add databases from RAG chunks. When reporting any run, ALWAYS "
        "state its run name and its recorded parameters explicitly in your "
        "reply — never refer the user to 'the parameters field'.",
    ]
    for db_path, rid, run_name, sample, params, ts in shown:
        lines.append(
            f"- Run {rid} in {db_path}: name={run_name!r}, sample={sample!r}, "
            f"completed={ts}, parameters={params}"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Deterministic file-location hook (find_file)
# ---------------------------------------------------------------------------
# The model reliably prefers Hermes's local `search_files` over our `find_file`
# for "where is X" questions, so it never reaches SharePoint (which is not on the
# filesystem, only in the manifest index). This hook runs our find_file search in
# code and injects the results, exactly like the run-id registry block — so the
# answer (incl. SharePoint web links) is in context regardless of tool choice.

_FIND_INTENT_RE = re.compile(
    r"\b(find|locate|where\s+(?:is|are|can|to)|search\s+for|look\s+for|"
    r"path\s+to|which\s+(?:file|doc|document|notebook)|give\s+me|show\s+me|"
    r"get\s+me|do\s+(?:we|you)\s+have|is\s+there|pull\s+up|provide)\b",
    re.IGNORECASE,
)
_FILE_NOUN_RE = re.compile(
    r"\b(files?|documents?|documentation|docs?|notebooks?|scripts?|papers?|"
    r"manuals?|reports?|presentations?|slides?|readmes?|spreadsheets?|"
    r"datasheets?|specs?|guides?|"
    r"\.(?:py|ipynb|docx?|pdf|pptx?|xlsx?|md|db|ya?ml|txt|csv|json|h5))\b",
    re.IGNORECASE,
)
# A bare filename/identifier STEM the user clearly wants located, carrying no
# file-noun word and no extension: a separator-joined, digit-bearing token such
# as 'photocurrent_SLG_240206', 'L208_Opticool', 'SLG09-C2-PhQH'. Without this,
# "where is photocurrent_SLG_240206" failed the noun gate and never fired the
# hook (adding '.pptx' was needed to trip it) — the R11 find_file bare-name gap.
# The digit requirement keeps it off ordinary hyphenated words ('back-gate') and
# dates ('2026-07-16', no letter); the length/separator requirement keeps it off
# short codes ('L110').
_FILENAME_STEM_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:[_-][A-Za-z0-9]+)+\b")


def _stem_terms(message: str) -> list:
    """Digit-bearing, separator-joined filename stems in the message (order-
    preserving, deduped). These are strong 'locate this exact name' signals."""
    out = []
    for s in _FILENAME_STEM_RE.findall(message):
        if any(c.isdigit() for c in s) and s.lower() not in _FIND_STOP and s not in out:
            out.append(s)
    return out
_FIND_STOP = {
    "the", "a", "an", "of", "about", "for", "on", "in", "to", "setup", "document",
    "documentation", "file", "files", "find", "locate", "where", "is", "are",
    "can", "which", "notebook", "notebooks", "script", "scripts", "paper",
    "papers", "manual", "manuals", "doc", "docs", "report", "reports",
    "presentation", "presentations", "slide", "slides", "spec", "specs", "guide",
    "guides", "datasheet", "please", "me", "give", "show", "get", "do", "we",
    "you", "have", "there", "pull", "up", "provide", "and", "or", "that",
    "contains", "containing", "named", "called", "any", "our", "group", "lab",
    "with", "sharepoint", "cifs", "server", "use", "find_file", "search", "need",
    "want", "related",
}


@lru_cache(maxsize=1)
def _files_mod():
    """Load the sibling qnoe_files plugin (stdlib-only) to reuse its search."""
    import importlib.util
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "qnoe_files", "__init__.py",
    )
    spec = importlib.util.spec_from_file_location("qnoe_files_hook", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _extract_terms(message: str) -> list:
    q = re.findall(r'["“‘\']([^"”’\']{2,60})["”’\']', message)
    if q:
        return [t.strip() for t in q[:2]]
    terms = re.findall(
        r'\b([\w.-]+\.(?:py|ipynb|docx?|pdf|pptx?|xlsx?|md|db|ya?ml|txt|csv|json|h5))\b',
        message, re.IGNORECASE,
    )
    # full separator-joined stems first (e.g. 'photocurrent_SLG_240206') so the
    # whole identifier is preferred over its trailing digit-run ('240206')
    terms += _stem_terms(message)
    # distinctive codes / CamelCase / ALLCAPS / alphanumeric device IDs
    terms += re.findall(r'\b([A-Za-z]*\d[\w-]*|[A-Z][a-z]+[A-Z]\w*|[A-Z]{2,}\w*)\b', message)
    out = []
    for t in terms:
        if t.lower() in _FIND_STOP or len(t) < 3:
            continue
        if t not in out:
            out.append(t)
    if out:
        return out[:2]
    # Fallback: the distinctive lowercase subject word(s) — e.g. "give me the
    # documentation of spectromag" -> "spectromag" (users type lowercase).
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", message)
             if w.lower() not in _FIND_STOP]
    words.sort(key=len, reverse=True)
    return words[:2]


def _find_file_block(message: str) -> str:
    if not message or not _FIND_INTENT_RE.search(message):
        return ""
    # Fire on an explicit file noun / extension OR a bare filename stem — the
    # latter closes the "where is photocurrent_SLG_240206" gap.
    if not (_FILE_NOUN_RE.search(message) or _stem_terms(message)):
        return ""
    terms = _extract_terms(message)
    if not terms:
        return ""
    try:
        fm = _files_mod()
    except Exception as exc:
        logger.warning("find_file hook: module load failed: %s", exc)
        return ""
    results = []
    for term in terms:
        try:
            results.extend(fm._search_sharepoint(term, 8))
            results.extend(fm._search_cifs(term, 8))
        except Exception as exc:
            logger.warning("find_file hook: search failed for %r: %s", term, exc)
    seen, uniq = set(), []
    for r in results:
        link = r.get("link", "")
        if link and link not in seen:
            seen.add(link)
            uniq.append(r)
    label = " / ".join(terms)
    if not uniq:
        return (
            f"## File-location lookup (authoritative — find_file index, covers "
            f"SharePoint) for '{label}': no indexed file matches. Tell the user no "
            f"indexed file matches that name; do NOT claim SharePoint is "
            f"inaccessible.\n\n"
        )
    sp = [r for r in uniq if r["source"] == "sharepoint"]
    cifs = [r for r in uniq if r["source"] == "cifs"]
    out = [
        f"## File-location lookup (authoritative — find_file index, covers "
        f"SharePoint) for '{label}':",
        "Report these to the user; SharePoint web links are valid even though "
        "SharePoint is not on the filesystem.",
    ]
    for r in (sp + cifs)[:10]:
        tag = "SharePoint" if r["source"] == "sharepoint" else "CIFS"
        out.append(f"- [{tag}] {r['link']}")
    if len(uniq) > 10:
        out.append(f"- …and {len(uniq) - 10} more — ask the user to narrow.")
    return "\n".join(out) + "\n\n"


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

RAG_SEARCH_SCHEMA = {
    "name": "rag_search",
    "description": (
        "Search the QNOE lab knowledge base (papers, code, documentation, "
        "measurement data). Returns relevant chunks from Qdrant collections "
        "with cross-encoder reranking. Use when you need specific information "
        "about lab code, papers, experiments, or measurement data."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language search query.",
            },
            "collection": {
                "type": "string",
                "description": (
                    "Optional: specific collection to search. "
                    "One of: group-wide, qtm, photocurrent, qed, "
                    "superconductivity, qsim, xchiral, qcodes-runs. "
                    "If omitted, searches all collections for your profile."
                ),
            },
        },
        "required": ["query"],
    },
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------


class QnoeRagProvider(MemoryProvider):
    """Qdrant RAG retrieval as a Hermes memory provider."""

    def __init__(self):
        self._collections: list[str] = DEFAULT_COLLECTIONS
        self._profile: str = ""
        self._prefetch_result: str = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: Optional[threading.Thread] = None
        # One provider instance can serve multiple sessions (see
        # on_session_switch), so key Mem0 per session_id -> user_id rather
        # than a single self._user_id.
        self._session_users: Dict[str, str] = {}
        self._last_uid: str = ""

    @property
    def name(self) -> str:
        return "qnoe_rag"

    def is_available(self) -> bool:
        try:
            import requests

            r = requests.get(f"{QDRANT_URL}/collections", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def initialize(self, session_id: str, **kwargs) -> None:
        self._profile = kwargs.get("agent_identity", "")
        self._collections = PROFILE_COLLECTIONS.get(
            self._profile, DEFAULT_COLLECTIONS
        )
        uid = kwargs.get("user_id") or kwargs.get("user_id_alt") or ""
        if uid:
            self._session_users[session_id] = uid
            # Hermes core calls prefetch_all() WITHOUT session_id (verified
            # 2026-07-10: injection log showed session='' -> uid 'anon' ->
            # mem_facts=0 despite correct facts in Qdrant). initialize() DOES
            # get session+user each turn, so remember the last user as a
            # fallback. Caveat: with truly concurrent multi-user turns this
            # can briefly attribute a lookup to the wrong user — acceptable
            # for read-side recall, revisit if Hermes passes session_id later.
            self._last_uid = uid
        logger.info(
            "QnoeRag initialized for profile=%s, collections=%s, session=%s, user=%s",
            self._profile,
            self._collections,
            session_id,
            uid or "<none>",
        )

    def _uid_for(self, session_id: str) -> str:
        # Order: exact session mapping > last-initialized user (covers
        # Hermes core's session-less prefetch_all calls) > session_id
        # (per-conversation memory) > anon.
        uid = self._session_users.get(session_id or "")
        if uid:
            return uid
        if getattr(self, "_last_uid", ""):
            if not session_id:
                logger.info("Mem0 uid fallback -> last-initialized user %s", self._last_uid)
            return self._last_uid if not session_id else (session_id or self._last_uid)
        return session_id or "anon"

    def system_prompt_block(self) -> str:
        colls = ", ".join(self._collections)
        return (
            "# QNOE RAG Knowledge Base\n"
            f"Active collections: {colls}\n"
            "RAG context is automatically injected each turn. "
            "Use the rag_search tool for explicit targeted queries."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        # Wait for background prefetch if running
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=10.0)

        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""

        if not result:
            # No prefetch available — do synchronous retrieval. Guarded: a RAG
            # failure (e.g. embedding model unavailable) must NOT crash prefetch,
            # or it takes the Mem0/qcodes/find_file hooks down with it (observed
            # 2026-07-14 when B7's PrivateTmp hid the fastembed BM25 cache).
            try:
                chunks = _run_retrieve(query, self._collections)
                result = _format_chunks(chunks)
            except Exception as e:
                logger.warning("RAG retrieve failed (degrading to empty RAG): %s", e)
                result = ""

        rag_block = f"## RAG Context\n{result}" if result else ""

        # Deterministic QCoDeS registry lookup for run-id questions. Must
        # never break a turn.
        qcodes_block = ""
        try:
            qcodes_block = _qcodes_registry_block(query)
        except Exception as e:
            logger.warning("QCoDeS registry hook failed: %s", e)

        # Deterministic file-location hook (find_file index, covers SharePoint).
        findfile_block = ""
        try:
            findfile_block = _find_file_block(query)
        except Exception as e:
            logger.warning("find_file hook failed: %s", e)

        # Per-user Mem0 facts, injected ahead of RAG. Must never break a turn.
        mem_block = ""
        if MEM0_ENABLED:
            try:
                uid = self._uid_for(session_id)
                # mem0 2.x: user_id goes in filters, count is top_k.
                facts = _get_mem0().search(
                    query, filters={"user_id": uid}, top_k=MEM0_TOP_K
                )
                mem_block = _format_facts(facts)
            except Exception as e:
                logger.warning("Mem0 search failed: %s", e)

        # Per-turn injection observability (added 2026-07-10 after a live turn
        # denied knowledge of a fact that ranked #1 in offline Mem0 search).
        logger.info(
            "prefetch inject: mem_facts=%d qcodes_block=%s findfile_block=%s rag_chars=%d session=%s query=%r",
            mem_block.count("\n- ") + (1 if mem_block.startswith("- ") else 0),
            bool(qcodes_block),
            bool(findfile_block),
            len(rag_block),
            session_id,
            (query or "")[:80],
        )

        return (mem_block + qcodes_block + findfile_block + rag_block) or ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        def _run():
            try:
                chunks = _run_retrieve(query, self._collections)
                formatted = _format_chunks(chunks)
                with self._prefetch_lock:
                    self._prefetch_result = formatted
            except Exception as e:
                logger.warning("RAG prefetch failed: %s", e)

        self._prefetch_thread = threading.Thread(
            target=_run, daemon=True, name="rag-prefetch"
        )
        self._prefetch_thread.start()

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        # RAG itself is read-only. Per-user memory is written here: Mem0's
        # add() calls the LLM to distil facts, so run it off the reply path
        # in a daemon thread (like queue_prefetch) — never blocks the turn.
        if not (MEM0_ENABLED and user_content):
            return
        uid = self._uid_for(session_id)

        def _write():
            try:
                # USER message only. Feeding the assistant reply lets the
                # model's own confabulations become "remembered facts" that
                # get cited back as truth next session (memory poisoning —
                # observed live 2026-07-10, mistakes M46: a fabricated
                # superconductivity survey was distilled into Mem0 and then
                # cited as "(Source: persistent memory context)").
                _get_mem0().add(
                    [{"role": "user", "content": user_content}],
                    user_id=uid,
                )
            except Exception as e:
                logger.warning("Mem0 add failed: %s", e)

        threading.Thread(target=_write, daemon=True, name="mem0-write").start()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [RAG_SEARCH_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if tool_name != "rag_search":
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

        query = args.get("query", "")
        if not query:
            return json.dumps({"error": "Missing required parameter: query"})

        # Optional collection filter
        collection = args.get("collection")
        if collection:
            if collection not in ALL_COLLECTIONS:
                return json.dumps({
                    "error": f"Unknown collection: {collection}. "
                    f"Valid: {', '.join(ALL_COLLECTIONS)}"
                })
            collections = [collection]
        else:
            collections = self._collections

        chunks = _run_retrieve(query, collections)

        if not chunks:
            return json.dumps({
                "result": "No relevant results found.",
                "collections_searched": collections,
            })

        results = []
        for c in chunks:
            results.append({
                "source": c.get("source", ""),
                "collection": c.get("collection", ""),
                "score": round(c.get("rerank_score", c.get("score", 0)), 3),
                "text": c["text"][:1500],  # cap per-chunk size
            })

        return json.dumps({
            "results": results,
            "count": len(results),
            "collections_searched": collections,
        })

    def shutdown(self) -> None:
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=5.0)


@lru_cache(maxsize=1)
def _grounding_validator():
    """Load the sibling grounding_validator module by path (this plugin is
    loaded via importlib, so a normal relative import isn't reliable)."""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "grounding_validator.py")
    spec = importlib.util.spec_from_file_location("qnoe_grounding_validator", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def register(ctx) -> None:
    """Register QNOE RAG as a memory provider plugin + the post-hoc grounding
    validator (transform_llm_output hook, redteam R11)."""
    ctx.register_memory_provider(QnoeRagProvider())
    try:
        gv = _grounding_validator().validate_reply
        ctx.register_hook("transform_llm_output", gv)
        # BELT: register directly into the singleton PluginManager that the
        # agent's turn_finalizer invoke_hook() reads. ctx.register_hook alone
        # did NOT make the hook dispatch (R11 debugging) — this guarantees the
        # callback is on the manager the agent actually consults. Idempotent.
        try:
            from hermes_cli.plugins import get_plugin_manager
            mgr = get_plugin_manager()
            hooks = mgr._hooks.setdefault("transform_llm_output", [])
            if gv not in hooks:
                hooks.append(gv)
            logger.info(
                "qnoe_rag: grounding validator registered "
                "(transform_llm_output; %d callback(s) on the agent's manager)",
                len(hooks),
            )
        except Exception as belt_exc:
            logger.warning("qnoe_rag: singleton hook belt failed: %s", belt_exc)
    except Exception as exc:  # never let the validator break plugin load
        logger.warning("qnoe_rag: grounding validator NOT registered: %s", exc)
