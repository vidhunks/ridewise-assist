"""Policy search pipeline:

  1. Hybrid retrieval: dense (meaning) + BM25 (keywords) in Qdrant, fused with Reciprocal Rank Fusion.
  2. Re-ranking: a cross-encoder reads the question and each candidate together and re-orders them
     (catches paraphrases like "I didn't turn up" -> "No-shows" that both retrievers rank too low).
  3. Cross-references ("reproduced in Annexure 1") are followed so the referenced table is returned too.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from functools import lru_cache

import numpy as np
from qdrant_client import QdrantClient, models

from app.rag.index import (COLLECTION, MODEL_CACHE, QUERY_INSTRUCTION, dense_model, get_client,
                           sparse_model)

log = logging.getLogger(__name__)

CANDIDATES = 20         # per retriever, before fusion
RERANK_POOL = 15        # fused candidates the cross-encoder re-orders
WEAK_MATCH = 0.5        # cosine below this: the policies probably don't cover the question
ANNEX_REF_RE = re.compile(r"\bAnnexure\s+([A-Z0-9]+)\b")
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "").strip() or "jinaai/jina-reranker-v1-tiny-en"
# Embedded Qdrant is a single-process store; the UI serves each browser session on its own thread.
_SEARCH_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def reranker():
    """Cross-encoder, or None if disabled (RERANKER_MODEL=none) or it fails to load."""
    if RERANKER_MODEL.lower() == "none":
        return None
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        return TextCrossEncoder(RERANKER_MODEL, cache_dir=str(MODEL_CACHE))
    except Exception as e:      # degrade to hybrid-only rather than fail the search
        log.warning("Re-ranker %s unavailable (%s); using hybrid ranking only.", RERANKER_MODEL, e)
        return None


def _result(pl: dict, rank: int, relevance: float | None, linked_from: str | None = None) -> dict:
    return {
        "rank": rank, "citation": pl["citation"],
        "relevance": None if relevance is None else round(relevance, 3),
        "rerank_score": None,
        "weak_match": relevance is not None and relevance < WEAK_MATCH,
        "linked_from": linked_from,
        "doc_id": pl["doc_id"], "doc_title": pl["doc_title"], "version": pl["version"],
        "effective": pl["effective"], "applies_to": pl["applies_to"], "section": pl["section"],
        "pages": pl["pages"], "chunk_type": pl["chunk_type"], "ocr_verified": pl["ocr_verified"],
        "text": pl["text"],
    }


def _annexures(client: QdrantClient) -> list[dict]:
    points, _ = client.scroll(COLLECTION, limit=1000, with_payload=True,
                              scroll_filter=models.Filter(must=[models.FieldCondition(
                                  key="chunk_type", match=models.MatchAny(any=["image", "table", "text"]))]))
    return [p.payload for p in points if p.payload["section"].lower().startswith("annexure")]


def _follow_references(results: list[dict], client: QdrantClient) -> list[dict]:
    present = {r["citation"] for r in results}
    annexures = None
    added = []
    for r in results:
        if r["weak_match"]:
            continue
        for ref in dict.fromkeys(ANNEX_REF_RE.findall(r["text"])):
            if r["section"].lower().startswith(f"annexure {ref.lower()}"):
                continue                                   # an annexure mentioning itself
            annexures = annexures if annexures is not None else _annexures(client)
            # Prefer the annexure in the same document; otherwise any document that has it.
            matches = sorted((a for a in annexures if a["section"].lower().startswith(f"annexure {ref.lower()}")),
                             key=lambda a: a["doc_id"] != r["doc_id"])
            if matches and matches[0]["citation"] not in present:
                present.add(matches[0]["citation"])
                added.append(_result(matches[0], len(results) + len(added) + 1, None, linked_from=r["citation"]))
    return results + added


def search_policy(query: str, k: int = 5, doc_ids: list[str] | None = None,
                  client: QdrantClient | None = None, rerank: bool = True) -> list[dict]:
    """Return the top-k policy chunks for `query`, each with its citation, plus any annexure
    those chunks refer to (marked `linked_from`).

    `relevance` is the cosine similarity between the query and the chunk (0-1, higher is closer);
    `weak_match` flags results that probably don't answer the question.
    """
    client = client or get_client()
    qd = next(iter(dense_model().embed([QUERY_INSTRUCTION + query])))
    qs = next(iter(sparse_model().query_embed(query)))
    flt = models.Filter(must=[models.FieldCondition(key="doc_id", match=models.MatchAny(any=doc_ids))]) \
        if doc_ids else None

    with _SEARCH_LOCK:
        return _search(query, k, flt, qd, qs, client, rerank)


def _search(query, k, flt, qd, qs, client, rerank) -> list[dict]:
    res = client.query_points(
        COLLECTION,
        prefetch=[
            models.Prefetch(query=qd.tolist(), using="dense", limit=CANDIDATES, filter=flt),
            models.Prefetch(query=models.SparseVector(indices=qs.indices.tolist(), values=qs.values.tolist()),
                            using="bm25", limit=CANDIDATES, filter=flt),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=max(k, RERANK_POOL),
        with_payload=True,
        with_vectors=["dense"],
    )
    qn = qd / np.linalg.norm(qd)
    out = []
    for rank, p in enumerate(res.points, start=1):
        v = np.asarray(p.vector["dense"])
        out.append(_result(p.payload, rank, float(qn @ (v / np.linalg.norm(v)))))

    model = reranker() if rerank else None
    if model and len(out) > 1:
        scores = list(model.rerank(query, [f"{r['section']}\n{r['text']}" for r in out]))
        for r, s in zip(out, scores):
            r["rerank_score"] = round(float(s), 3)
        out.sort(key=lambda r: -r["rerank_score"])
    out = out[:k]
    for i, r in enumerate(out, start=1):
        r["rank"] = i
    return _follow_references(out, client)
