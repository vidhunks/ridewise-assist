"""Qdrant vector index for the policy chunks.

Each chunk is stored with two vectors:
  * dense  - BAAI/bge-small-en-v1.5 (384-d, cosine): matches meaning ("money back" ~ "refund")
  * bm25   - sparse keyword vector with IDF: matches exact terms ("GOLD", "no-show", "Annexure A")
and its citation metadata as payload (doc_id, version, section, pages, ...), which is filterable.

Runs embedded (a folder under data/qdrant) by default; set QDRANT_URL (and QDRANT_API_KEY) to use
a Qdrant server instead - the code is identical.
"""
from __future__ import annotations

import atexit
import hashlib
import logging
import os
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")   # model cache on Windows

from fastembed import SparseTextEmbedding, TextEmbedding  # noqa: E402
from qdrant_client import QdrantClient, models  # noqa: E402

from app.config import ROOT  # noqa: E402
from app.rag import ingest  # noqa: E402
from app.rules import RULES_FILE  # noqa: E402

log = logging.getLogger(__name__)

COLLECTION = "ridewise_policies"
SCHEMA_VERSION = "1"
DENSE_MODEL = os.getenv("EMBEDDING_MODEL", "").strip() or "BAAI/bge-small-en-v1.5"
SPARSE_MODEL = "Qdrant/bm25"
LOCAL_PATH = ROOT / "data" / "qdrant"
MODEL_CACHE = ROOT / ".cache" / "fastembed"
# bge models retrieve better when queries (not documents) carry this instruction.
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "


@lru_cache(maxsize=1)
def dense_model() -> TextEmbedding:
    return TextEmbedding(DENSE_MODEL, cache_dir=str(MODEL_CACHE))


@lru_cache(maxsize=1)
def sparse_model() -> SparseTextEmbedding:
    return SparseTextEmbedding(SPARSE_MODEL, cache_dir=str(MODEL_CACHE))


_clients: dict[str, QdrantClient] = {}


def get_client(path: Path | None = None) -> QdrantClient:
    """One client per location (embedded Qdrant allows a single client per folder)."""
    url = os.getenv("QDRANT_URL", "").strip()
    key = url or str(path or LOCAL_PATH)
    if key not in _clients:
        if url:
            _clients[key] = QdrantClient(url=url, api_key=os.getenv("QDRANT_API_KEY") or None)
        else:
            Path(key).mkdir(parents=True, exist_ok=True)
            _clients[key] = QdrantClient(path=key)
    return _clients[key]


@atexit.register
def _close_clients() -> None:
    for c in _clients.values():
        try:
            c.close()
        except Exception:
            pass
    _clients.clear()


def fingerprint() -> str:
    """Changes whenever anything that affects the index changes."""
    h = hashlib.sha256(f"{SCHEMA_VERSION}|{DENSE_MODEL}|{SPARSE_MODEL}".encode())
    inputs = sorted(ingest.POLICY_DIR.glob("*.pdf")) + [ingest.TRANSCRIPTIONS, RULES_FILE,
                                                        Path(ingest.__file__)]
    for p in inputs:
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _indexed_fingerprint(client: QdrantClient) -> str | None:
    if not client.collection_exists(COLLECTION):
        return None
    points, _ = client.scroll(COLLECTION, limit=1, with_payload=["index_version"])
    return points[0].payload.get("index_version") if points else None


def is_current(client: QdrantClient | None = None) -> bool:
    return _indexed_fingerprint(client or get_client()) == fingerprint()


def build_index(client: QdrantClient | None = None, force: bool = False) -> int:
    """(Re)build the collection if its inputs changed. Returns the number of chunks indexed."""
    client = client or get_client()
    fp = fingerprint()
    if not force and _indexed_fingerprint(client) == fp:
        return client.count(COLLECTION).count

    chunks = ingest.build_all_chunks()
    ingest.write_chunks(chunks)
    unverified = [c.citation for c in chunks if c.ocr_verified is False]
    if unverified:
        log.warning("Indexed %d chunk(s) from unverified OCR: %s", len(unverified), unverified)

    texts = [c.embed_text for c in chunks]
    dense = list(dense_model().embed(texts))
    sparse = list(sparse_model().embed(texts))

    if client.collection_exists(COLLECTION):
        client.delete_collection(COLLECTION)
    client.create_collection(
        COLLECTION,
        vectors_config={"dense": models.VectorParams(size=len(dense[0]), distance=models.Distance.COSINE)},
        sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )
    if os.getenv("QDRANT_URL"):     # payload indexes only exist on a Qdrant server, not embedded mode
        for field_name in ("doc_id", "version", "chunk_type"):
            client.create_payload_index(COLLECTION, field_name, models.PayloadSchemaType.KEYWORD)

    client.upsert(COLLECTION, [
        models.PointStruct(
            id=c.point_id(),
            vector={"dense": d.tolist(),
                    "bm25": models.SparseVector(indices=s.indices.tolist(), values=s.values.tolist())},
            payload=asdict(c) | {"citation": c.citation, "index_version": fp},
        )
        for c, d, s in zip(chunks, dense, sparse)
    ])
    log.info("Indexed %d policy chunks into %s (index_version %s)", len(chunks), COLLECTION, fp)
    return len(chunks)


def ensure_index() -> int:
    return build_index(force=False)
