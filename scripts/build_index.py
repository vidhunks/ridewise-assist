"""Build (or refresh) the Qdrant policy index and data/policy_chunks.jsonl.

Run from the project root:  python -m scripts.build_index [--force] ["test query"]
"""
from __future__ import annotations

import logging
import sys

from app.rag.index import build_index, fingerprint
from app.rag.retriever import search_policy


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for noisy in ("httpx", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    args = [a for a in sys.argv[1:] if a != "--force"]
    n = build_index(force="--force" in sys.argv)
    print(f"Index ready: {n} chunks (index_version {fingerprint()})")
    for q in args:
        print(f"\nQ: {q}")
        for r in search_policy(q, k=3):
            score = "linked" if r["linked_from"] else f"{r['relevance']:.2f}" + (" weak" if r["weak_match"] else "")
            print(f"  {r['rank']}. [{score}] {r['citation']}")


if __name__ == "__main__":
    main()
