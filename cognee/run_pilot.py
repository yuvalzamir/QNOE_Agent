#!/usr/bin/env python3
"""QTOM Cognee Tier-2 pilot harness: configure -> add -> cognify(ontology,
effort:high) -> export the graph for human judging (COGNEE_PLAN §D).

Env (with pilot defaults):
  COGNEE_DATA=/home/yzamir/cognee-pilot/data
  LLM_ENDPOINT=http://localhost:8000/v1  LLM_MODEL=gpt-oss-120b
  VECTOR_DB_PROVIDER=lancedb  (smoke default; set 'qdrant' for the real run)
  REASONING_EFFORT=high

Usage (DGX):
  ENABLE_BACKEND_ACCESS_CONTROL=false /home/yzamir/cognee-pilot/venv/bin/python \
    run_pilot.py --docs output/qtm_docs.jsonl --limit 2 --prune
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys

# cognee's ingestion auto-fetches any URL found in the text (web_scraper) — an
# air-gap violation AND a stall (part-number links time out). Strip URLs before
# feeding cognee so it never reaches the internet.
_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)


def _strip_urls(text: str) -> str:
    return _URL_RE.sub(" ", text or "")

os.environ.setdefault("ENABLE_BACKEND_ACCESS_CONTROL", "false")
os.environ.setdefault("COGNEE_SKIP_CONNECTION_TEST", "true")  # cognee's 30s test is over-eager
# THREAD CAPS — critical. Without these, fastembed/onnxruntime spawns a thread
# per core PER concurrent embed call across cognee's async tasks -> thread
# oversubscription meltdown (observed: loadavg ~350 on a 20-core box, sshd
# unresponsive). Cap every math/threadpool lib. (memory: full-ingest lesson.)
for _tv in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS", "ONNXRUNTIME_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_tv, "4")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
# cognee 1.4.0 ontology: the RDFLib fuzzy resolver canonicalizes extracted
# entities/relations onto OUR schema (qnoe_ontology.ttl). No-op on 0.5.6.
os.environ.setdefault("WEB_SCRAPER_TIMEOUT", "2")  # belt: fail fast if any URL slips the strip
os.environ.setdefault("ONTOLOGY_RESOLVER", "rdflib")
os.environ.setdefault(
    "ONTOLOGY_FILE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "qnoe_ontology.ttl"),
)

DATA = os.environ.get("COGNEE_DATA", "/home/yzamir/cognee-pilot/data")
LLM_ENDPOINT = os.environ.get("LLM_ENDPOINT", "http://localhost:8000/v1")
# litellm needs the provider prefix so it routes to the OpenAI provider + our api_base
LLM_MODEL = os.environ.get("LLM_MODEL", "openai/gpt-oss-120b")
VECTOR_PROVIDER = os.environ.get("VECTOR_DB_PROVIDER", "lancedb")
EFFORT = os.environ.get("REASONING_EFFORT", "high")
# Our ontology graph-extraction prompt (overrides cognee's generic default —
# cognify does NOT thread custom_prompt, so graph_prompt_path is the real lever)
GRAPH_PROMPT = os.environ.get(
    "GRAPH_PROMPT_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "qnoe_graph_prompt.txt"),
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("run_pilot")


def configure():
    import cognee
    cognee.config.system_root_directory(os.path.join(DATA, "system"))
    cognee.config.data_root_directory(os.path.join(DATA, "data"))
    # LLM — local llama.cpp (OpenAI-compatible). Use the CUSTOM provider ->
    # GenericAPIAdapter, which defaults to instructor json_mode. gpt-oss +
    # llama.cpp emit multiple tool_calls under the openai adapter's
    # json_schema_mode ("Instructor does not support multiple tool calls");
    # plain json_mode avoids that.
    cognee.config.set_llm_provider(os.environ.get("LLM_PROVIDER", "custom"))
    cognee.config.set_llm_model(LLM_MODEL)
    cognee.config.set_llm_endpoint(LLM_ENDPOINT)
    cognee.config.set_llm_api_key("sk-local")
    # Embeddings — local FastEmbed nomic (dim 768), no endpoint
    cognee.config.set_embedding_provider("fastembed")
    cognee.config.set_embedding_model("nomic-ai/nomic-embed-text-v1.5")
    cognee.config.set_embedding_dimensions(768)
    # Graph = Kùzu (embedded); vector per env
    cognee.config.set_graph_database_provider("kuzu")
    cognee.config.set_vector_db_provider(VECTOR_PROVIDER)
    if VECTOR_PROVIDER == "qdrant":
        try:
            import cognee_community_vector_adapter_qdrant  # noqa: F401 (self-register on import)
        except Exception as e:  # pragma: no cover
            logger.warning("qdrant adapter import: %s", e)
        cognee.config.set_vector_db_url(os.environ.get("QDRANT_URL", "http://localhost:6333"))
    # NOTE effort:high is NOT wired — the llama.cpp server does not honor
    # per-request reasoning_effort (baked low) and cognee 1.4.0 rejects it in
    # config. Getting high effort needs a server-side restart (user decision).
    # point graph extraction at OUR ontology prompt (mutate the config singleton
    # directly — there is no set_graph_prompt_path helper)
    from cognee.infrastructure.llm.config import get_llm_config
    llm_cfg = get_llm_config()
    llm_cfg.graph_prompt_path = GRAPH_PROMPT
    # CLIENT TIMEOUT — critical at high reasoning effort. litellm's default is
    # 600s; at effort:high a single extraction generates 10-11K reasoning
    # tokens ≈ 9-10+ min under 4-slot concurrency, so EVERY call straddles the
    # timeout: the client aborts + retries, llama keeps generating into the
    # void, and the run makes zero progress while pinning the LLM (observed
    # 2026-07-20: 50 completed server-side generations, 0 rows in
    # session_model_usage/nodes/edges after 50 min). llm_args is plumbed
    # straight into the litellm completion kwargs.
    llm_cfg.llm_args = {**(llm_cfg.llm_args or {}),
                        "timeout": int(os.environ.get("LLM_CLIENT_TIMEOUT", "3600"))}
    logger.info("graph_prompt_path -> %s ; llm client timeout -> %ss",
                GRAPH_PROMPT, llm_cfg.llm_args["timeout"])


STRUCT_TYPES = {"DocumentChunk", "TextSummary", "TextDocument", "EntityType", "TextChunk", "NodeSet"}
STRUCT_RELS = {"contains", "made_from", "is_part_of", "is_a", "exists_in", "has_summary"}


async def export_graph(out_prefix: str):
    """Dump a CLEAN ontology graph for judging: entities resolved to their
    ontology type (via the is_a->EntityType edge), cognee's structural wrapper
    nodes/edges dropped, only semantic entity->entity edges kept."""
    from collections import Counter
    from cognee.infrastructure.databases.graph import get_graph_engine
    engine = await get_graph_engine()
    nodes, edges = await engine.get_graph_data()
    nprops = {str(nid): props for nid, props in nodes}

    # entity id -> ontology type name, from its is_a edge to an EntityType node
    ent_type = {}
    for (s, t, key, props) in edges:
        if (props.get("relationship_name", key) == "is_a"
                and nprops.get(str(t), {}).get("type") == "EntityType"):
            ent_type[str(s)] = (nprops[str(t)].get("name") or "").strip()

    ent = [{"id": str(nid), "name": props.get("name"),
            "type": ent_type.get(str(nid), "untyped"),
            "description": props.get("description", "")}
           for nid, props in nodes if props.get("type") == "Entity"]
    ent_ids = {n["id"] for n in ent}
    byname = {n["id"]: n["name"] for n in ent}

    sem = [{"source": str(s), "target": str(t), "rel": props.get("relationship_name", key)}
           for (s, t, key, props) in edges
           if props.get("relationship_name", key) not in STRUCT_RELS
           and str(s) in ent_ids and str(t) in ent_ids]

    with open(out_prefix + ".json", "w", encoding="utf-8") as fh:
        json.dump({"nodes": ent, "edges": sem}, fh, ensure_ascii=False, indent=2)

    lines = [f"# QTOM pilot graph — {len(ent)} entities, {len(sem)} relations "
             f"(cognee structural nodes/edges removed)\n"]
    lines.append("## Node types (our ontology)\n" + ", ".join(
        f"{k}={v}" for k, v in Counter(n["type"] for n in ent).most_common()) + "\n")
    lines.append("## Entities by type\n")
    for typ in sorted(set(n["type"] for n in ent)):
        lines.append(f"\n### {typ}")
        for n in sorted([x for x in ent if x["type"] == typ], key=lambda x: x["name"] or ""):
            lines.append(f"- **{n['name']}** — {n['description']}")
    lines.append("\n## Relations\n")
    for e in sem:
        lines.append(f"- {byname.get(e['source'], e['source'])} —{e['rel']}→ {byname.get(e['target'], e['target'])}")
    with open(out_prefix + ".md", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    logger.info("exported %d entities / %d relations (clean) → %s.{json,md}", len(ent), len(sem), out_prefix)


def _avail_gb() -> float:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except Exception:
        pass
    return 999.0


def _start_memory_watchdog(floor_gb: float) -> None:
    """Intra-batch OOM protection. The between-batch guard cannot catch a
    STEP-allocation: three kernel OOM kills on 2026-07-20 all hit ~40GB
    anon-rss, the last going 1.3GB -> 40.8GB in ~60s (8K-token auto
    chunk_size -> ONNX embedding attention buffers). Exit resumably at the
    floor instead of letting the kernel pick a victim (llama-server and the
    gateway share this box)."""
    import threading
    import time as _t

    def _watch():
        while True:
            avail = _avail_gb()
            if avail < floor_gb:
                logger.error("MEMORY WATCHDOG: %.0fGB available < %.0fGB floor — "
                             "exiting resumably (rc=3)", avail, floor_gb)
                os._exit(3)
            _t.sleep(5)

    threading.Thread(target=_watch, daemon=True, name="mem-watchdog").start()


async def run(args):
    import cognee
    from cognee.shared.data_models import KnowledgeGraph
    if args.export_only:
        await export_graph(args.out)
        return
    if args.prune:
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)
        logger.info("pruned prior data/system")
    docs = [json.loads(l) for l in open(args.docs, encoding="utf-8")]
    if args.contains:
        docs = [d for d in docs if args.contains.lower() in d.get("item_path", "").lower()]
    if args.max_chars:
        big = [d for d in docs if d.get("chars", 0) > args.max_chars]
        docs = [d for d in docs if d.get("chars", 0) <= args.max_chars]
        if big:
            logger.info("skipping %d docs > %d chars (process separately): %s",
                        len(big), args.max_chars, [d["item_path"][:40] for d in big])
    if args.limit:
        docs = sorted(docs, key=lambda d: d["chars"])[: args.limit] if args.smallest else docs[: args.limit]

    # BATCHED add+cognify — the whole-corpus single cognify() is pathological:
    # cognee eagerly fans out a pipeline per document and holds every doc's
    # in-flight state while the LLM stage crawls, so nothing materializes and
    # RSS grows without backpressure (observed 2026-07-20: 1.3GB -> 26.5GB in
    # ~70 min, 0 nodes, box nearly OOMed; same disease behind the Jul-18 melt).
    # Small batches bound the fan-out, materialize the graph incrementally,
    # and make the run resumable (cognee dedups adds and skips already-
    # processed docs on the next cognify of the same dataset).
    bs = args.batch_size
    total = len(docs)
    nbatches = (total + bs - 1) // bs
    _start_memory_watchdog(max(args.min_free_gb - 6.0, 8.0))
    logger.info("processing %d docs in %d batches of <=%d (dataset prefix=%s, effort=%s)",
                total, nbatches, bs, args.dataset, EFFORT)
    for bi in range(nbatches):
        batch = docs[bi * bs:(bi + 1) * bs]
        # ONE DATASET PER BATCH — the load-bearing scoping. cognify(datasets=[X])
        # processes EVERY pending doc in X, not the docs just added: with a
        # single shared dataset, every "batch" cognify actually launched the
        # whole remaining corpus and ballooned to ~40GB anon-rss (two identical
        # OOM kills 2026-07-20, kern.log, caps made no difference). Per-batch
        # datasets bound the real work; the GRAPH is global across datasets, so
        # the export is unaffected. Resume: re-running fast-forwards — add()
        # dedups, cognify on a processed batch-dataset is a cheap no-op.
        ds = f"{args.dataset}_b{bi:03d}"
        avail = _avail_gb()
        if avail < args.min_free_gb:
            logger.error("ABORT before batch %d/%d: %.0fGB RAM available < %.0fGB floor "
                         "(run is resumable — rerun without --prune)",
                         bi + 1, nbatches, avail, args.min_free_gb)
            sys.exit(3)
        logger.info("batch %d/%d (%s): adding %d docs (%.0fGB RAM free) …",
                    bi + 1, nbatches, ds, len(batch), avail)
        for d in batch:
            await cognee.add(_strip_urls(d["text"]), dataset_name=ds)
        # Internal concurrency caps (cognee-native, researched 2026-07-20):
        # cognify defaults are data_per_batch=20 / chunks_per_batch=100; the
        # docs' memory-constrained guidance is 2-5 / 10-25.
        # chunk_size EXPLICIT — cognee's auto value is min(embed_max, llm_max/2)
        # ≈ 8192 tokens here, and embedding 8K-token chunks through fastembed/
        # ONNX allocates attention buffers scaling with seq² — the ~40GB
        # step-balloon behind all three 2026-07-20 kernel OOM kills. 2048 cuts
        # the quadratic 16× and shrinks extraction prompts 4× (faster, more
        # convergent medium-effort calls) at the cost of finer-grained chunks.
        await cognee.cognify(
            datasets=[ds], graph_model=KnowledgeGraph,
            chunk_size=int(os.environ.get("COGNIFY_CHUNK_SIZE", "2048")),
            data_per_batch=int(os.environ.get("COGNIFY_DATA_PER_BATCH", "2")),
            chunks_per_batch=int(os.environ.get("COGNIFY_CHUNKS_PER_BATCH", "10")),
        )
        logger.info("batch %d/%d cognified (%d/%d docs done)",
                    bi + 1, nbatches, min((bi + 1) * bs, total), total)
    await export_graph(args.out)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--docs", default="output/qtm_docs.jsonl")
    ap.add_argument("--dataset", default="qtom_pilot")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--contains", default="", help="only docs whose item_path contains this substring")
    ap.add_argument("--max-chars", type=int, default=0, help="skip docs larger than this (0=no cap)")
    ap.add_argument("--batch-size", type=int,
                    default=int(os.environ.get("COGNIFY_BATCH_SIZE", "8")),
                    help="docs per add+cognify batch (bounds memory/fan-out)")
    ap.add_argument("--min-free-gb", type=float,
                    default=float(os.environ.get("COGNIFY_MIN_FREE_GB", "12")),
                    help="abort (resumable) if available RAM drops below this before a batch")
    ap.add_argument("--smallest", action="store_true", help="pick the smallest docs (fast smoke)")
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--export-only", action="store_true", help="re-export existing graph, no cognify")
    ap.add_argument("--out", default="output/qtm_graph")
    args = ap.parse_args(argv)
    configure()
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
