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
import sys

os.environ.setdefault("ENABLE_BACKEND_ACCESS_CONTROL", "false")
os.environ.setdefault("COGNEE_SKIP_CONNECTION_TEST", "true")  # cognee's 30s test is over-eager

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
    # reasoning_effort:high — best-effort (0.5.6 forwards extra llm config to litellm)
    try:
        cognee.config.set_llm_config({"reasoning_effort": EFFORT, "max_tokens": 8192})
    except Exception as e:
        logger.warning("set_llm_config(reasoning_effort) not accepted: %s", e)
    # point graph extraction at OUR ontology prompt (mutate the config singleton
    # directly — there is no set_graph_prompt_path helper)
    from cognee.infrastructure.llm.config import get_llm_config
    get_llm_config().graph_prompt_path = GRAPH_PROMPT
    logger.info("graph_prompt_path -> %s", GRAPH_PROMPT)


async def export_graph(out_prefix: str):
    """Best-effort dump of nodes+edges for judging (API varies across 0.5.x)."""
    from cognee.infrastructure.databases.graph import get_graph_engine
    engine = await get_graph_engine()
    nodes, edges = await engine.get_graph_data()
    node_rows = []
    for nid, props in nodes:
        node_rows.append({"id": str(nid), "name": props.get("name"),
                          "type": props.get("type"), "description": props.get("description", "")})
    edge_rows = [{"source": str(s), "target": str(t), "rel": props.get("relationship_name", key)}
                 for (s, t, key, props) in edges]
    with open(out_prefix + ".json", "w", encoding="utf-8") as fh:
        json.dump({"nodes": node_rows, "edges": edge_rows}, fh, ensure_ascii=False, indent=2)
    # readable markdown
    lines = [f"# QTOM pilot graph — {len(node_rows)} nodes, {len(edge_rows)} edges\n"]
    from collections import Counter
    lines.append("## Node types\n" + ", ".join(f"{k}={v}" for k, v in
                 Counter(n["type"] for n in node_rows).most_common()) + "\n")
    lines.append("## Nodes\n")
    for n in sorted(node_rows, key=lambda x: (x["type"] or "", x["name"] or "")):
        lines.append(f"- **[{n['type']}] {n['name']}** — {n['description']}")
    lines.append("\n## Edges\n")
    byname = {r["id"]: r["name"] for r in node_rows}
    for e in edge_rows:
        lines.append(f"- {byname.get(e['source'], e['source'])} —{e['rel']}→ {byname.get(e['target'], e['target'])}")
    with open(out_prefix + ".md", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    logger.info("exported %d nodes / %d edges → %s.{json,md}", len(node_rows), len(edge_rows), out_prefix)


async def run(args):
    import cognee
    from cognee.shared.data_models import KnowledgeGraph
    if args.prune:
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)
        logger.info("pruned prior data/system")
    docs = [json.loads(l) for l in open(args.docs, encoding="utf-8")]
    if args.contains:
        docs = [d for d in docs if args.contains.lower() in d.get("item_path", "").lower()]
    if args.limit:
        docs = sorted(docs, key=lambda d: d["chars"])[: args.limit] if args.smallest else docs[: args.limit]
    logger.info("adding %d docs (dataset=%s)", len(docs), args.dataset)
    for d in docs:
        await cognee.add(d["text"], dataset_name=args.dataset)
    logger.info("cognify (graph_model=KnowledgeGraph, our graph_prompt, effort=%s) …", EFFORT)
    await cognee.cognify(datasets=[args.dataset], graph_model=KnowledgeGraph)
    await export_graph(args.out)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--docs", default="output/qtm_docs.jsonl")
    ap.add_argument("--dataset", default="qtom_pilot")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--contains", default="", help="only docs whose item_path contains this substring")
    ap.add_argument("--smallest", action="store_true", help="pick the smallest docs (fast smoke)")
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--out", default="output/qtm_graph")
    args = ap.parse_args(argv)
    configure()
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
