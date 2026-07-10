"""One-time backfill script — adds BM25 sparse vectors to all existing Qdrant points.

Run once after deploying BM25 hybrid search to backfill all existing collections.
Progress is tracked in a `sparse_backfill` SQLite table and is resumable.

Usage:
  cd /opt/qnoe-agent
  AGENT_DATA_DIR=/home/yzamir/qnoe_server_data QDRANT_URL=http://localhost:6333 \\
    venv/bin/python -m agent.indexing.backfill_sparse

The script is a no-op once all collections show a completed_at timestamp.
"""
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import SparseVector, SparseVectorConfig, SparseVectorNameConfig

from agent.ingest.embed import embed_sparse

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
AGENT_DATA_DIR = os.environ.get("AGENT_DATA_DIR", "/opt/qnoe-agent/memory")
MANIFEST_DB = os.path.join(AGENT_DATA_DIR, "episodic.db")
SCROLL_BATCH = 50  # points per scroll page (sparse vectors are large — 200 exceeds Qdrant's 32MB limit)


def _get_backfill_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(MANIFEST_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sparse_backfill (
            collection   TEXT PRIMARY KEY,
            last_offset  TEXT,
            completed_at TEXT
        )
    """)
    conn.commit()
    return conn


def _add_sparse_config(client: QdrantClient, collection: str) -> None:
    """Add text-sparse vector field to collection (idempotent)."""
    info = client.get_collection(collection)
    if info.config.params.sparse_vectors and "text-sparse" in info.config.params.sparse_vectors:
        logger.info("[%s] Sparse field already present, skipping schema update", collection)
        return
    try:
        client.create_vector_name(
            collection_name=collection,
            vector_name="text-sparse",
            vector_name_config=SparseVectorNameConfig(sparse=SparseVectorConfig()),
        )
        logger.info("[%s] Sparse vector field added", collection)
    except Exception as exc:
        logger.warning("[%s] create_vector_name failed: %s", collection, exc)


def _backfill_collection(
    client: QdrantClient,
    conn: sqlite3.Connection,
    collection: str,
) -> None:
    # Check if already done
    row = conn.execute(
        "SELECT last_offset, completed_at FROM sparse_backfill WHERE collection = ?",
        (collection,),
    ).fetchone()

    if row and row[1] is not None:
        logger.info("[%s] Already complete, skipping", collection)
        return

    offset = row[0] if row else None
    total_updated = 0

    logger.info("[%s] Starting backfill (offset=%s)", collection, offset)

    while True:
        scroll_result = client.scroll(
            collection_name=collection,
            limit=SCROLL_BATCH,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        points, next_offset = scroll_result

        if not points:
            break

        # Extract text from payload
        texts = [p.payload.get("text", "") if p.payload else "" for p in points]
        point_ids = [p.id for p in points]

        # Skip points with empty text
        valid = [(pid, txt) for pid, txt in zip(point_ids, texts) if txt]
        if valid:
            valid_ids, valid_texts = zip(*valid)
            sparse_embs = embed_sparse(list(valid_texts))

            # Build update vectors list — skip points where sparse embedding is empty
            update_vectors = [
                {
                    "id": pid,
                    "vector": {
                        "text-sparse": SparseVector(
                            indices=sv.indices.tolist(),
                            values=sv.values.tolist(),
                        )
                    },
                }
                for pid, sv in zip(valid_ids, sparse_embs)
                if len(sv.indices) > 0
            ]

            client.update_vectors(
                collection_name=collection,
                points=update_vectors,
            )
            total_updated += len(valid_ids)

        # Persist progress
        conn.execute(
            """INSERT OR REPLACE INTO sparse_backfill (collection, last_offset, completed_at)
               VALUES (?, ?, NULL)""",
            (collection, str(next_offset) if next_offset else None),
        )
        conn.commit()

        logger.info(
            "[%s] Progress: %d updated, offset=%s",
            collection, total_updated, next_offset,
        )

        if next_offset is None:
            break
        offset = next_offset

    # Mark complete
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT OR REPLACE INTO sparse_backfill (collection, last_offset, completed_at)
           VALUES (?, NULL, ?)""",
        (collection, now),
    )
    conn.commit()
    logger.info("[%s] Backfill complete — %d points updated", collection, total_updated)


def main() -> None:
    client = QdrantClient(url=QDRANT_URL)
    conn = _get_backfill_conn()

    collections = [c.name for c in client.get_collections().collections]
    logger.info("Collections to backfill: %s", collections)

    for collection in collections:
        _add_sparse_config(client, collection)
        _backfill_collection(client, conn, collection)

    conn.close()
    logger.info("All collections backfilled.")


if __name__ == "__main__":
    main()
