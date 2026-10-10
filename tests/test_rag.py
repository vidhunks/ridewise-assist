"""Phase 3: policy ingestion, chunking and hybrid retrieval.

Builds its own Qdrant index in a temp folder, so it never clashes with a running app.
"""
import pytest

from app.rag import index as idx
from app.rag import ingest
from app.rag.retriever import search_policy


@pytest.fixture(scope="module")
def chunks():
    return ingest.build_all_chunks()


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    c = idx.get_client(tmp_path_factory.mktemp("qdrant"))
    idx.build_index(client=c, force=True)
    return c


def cites(results):
    return [r["citation"] for r in results]


# ---------------------------------------------------------------- ingestion / chunking

def test_every_document_is_ingested_with_metadata(chunks):
    assert {c.doc_id for c in chunks} == {f"RW-POL-0{i}" for i in range(1, 6)}
    for c in chunks:
        assert c.version and c.section and c.pages and c.text and c.citation


def test_headers_footers_and_logos_are_dropped(chunks):
    for c in chunks:
        assert "Customer policy document" not in c.text
        assert not any(line.strip().startswith("Page ") for line in c.text.splitlines())


def test_image_only_tables_use_verified_transcriptions(chunks):
    images = [c for c in chunks if c.chunk_type == "image"]
    assert images and all(c.ocr_verified for c in images)
    v2 = next(c for c in chunks if c.doc_id == "RW-POL-03" and c.section.startswith("Annexure 1"))
    assert "| 12 hours or more, but less than 48 hours | 75% |" in v2.text
    tiers = next(c for c in chunks if c.section.startswith("Annexure A"))
    assert "| GOLD | 30 minutes (up to and including 30 min) |" in tiers.text
    battery = next(c for c in chunks if "Figure 2" in c.text)
    assert "₹150" in battery.text and "Exactly 10% is not below 10%" in battery.text


def test_table_split_across_pages_is_rejoined(chunks):
    t = next(c for c in chunks if "Royal Enfield Classic 350" in c.text)
    assert "Oben Rorr" in t.text and t.text.count("|Model|") == 1 and t.pages == [1, 2]


def test_chunks_fit_the_embedding_window(chunks):
    assert max(len(c.embed_text) for c in chunks) < 2000      # ~500 tokens for bge-small


def test_refund_policies_say_which_bookings_they_apply_to(chunks):
    v1 = next(c for c in chunks if c.doc_id == "RW-POL-02")
    v2 = next(c for c in chunks if c.doc_id == "RW-POL-03")
    assert "until before 2026-07-01 00:00 IST" in v1.embed_text
    assert "from 2026-07-01 00:00 IST onwards" in v2.embed_text


def test_faq_is_one_chunk_per_question(chunks):
    faqs = [c for c in chunks if c.chunk_type == "faq"]
    assert len(faqs) == 6 and all(c.text.startswith("Q.") for c in faqs)


def test_unverified_image_falls_back_to_ocr_and_is_flagged(monkeypatch):
    monkeypatch.setattr(ingest, "_load_transcriptions", lambda: {})
    pdf = ingest.POLICY_DIR / "03_Cancellation_and_Refund_Policy_v2.pdf"
    annex = next(c for c in ingest.build_chunks(pdf) if c.chunk_type == "image")
    assert annex.ocr_verified is False and annex.text.count("[Unverified OCR") == 1
    assert "75%" in annex.text


# ---------------------------------------------------------------- retrieval

@pytest.mark.parametrize("query,expected", [
    ("GOLD grace period", "Annexure A: Tier benefits"),
    ("do gold members get extra time to return the bike", "Annexure A: Tier benefits"),
    ("what if my KYC is still pending when I come to pick up", "2. KYC verification"),
    ("when do I get my security deposit back", "5. Security deposit"),
    ("I didn't turn up for my booking, will I get a refund", "No-shows"),
    ("what happens if the battery dies during my ride", "battery runs out"),
    ("late fee per hour for Ultraviolette F77", "1. Late return fee (continued)"),
    ("returned the EV at exactly 10 percent battery", "low-battery"),
    ("which hubs can I pick up from", "3. Hubs, pickup and return"),
])
def test_retrieval_finds_the_right_clause(client, query, expected):
    top = cites(search_policy(query, k=3, client=client))
    assert any(expected.lower() in c.lower() for c in top), top


def test_refund_question_returns_both_versions_and_the_v2_table(client):
    top = cites(search_policy("how much refund if I cancel 10 hours before pickup", k=5, client=client))
    assert any(c.startswith("RW-POL-02") for c in top)
    assert any(c.startswith("RW-POL-03") and "Annexure 1" in c for c in top)


def test_cross_reference_is_followed_and_marked(client):
    res = search_policy("refund schedule for cancellation by the customer", k=3, doc_ids=["RW-POL-03"], client=client)
    annex = [r for r in res if "Annexure 1" in r["citation"]]
    assert annex and all(r["doc_id"] == "RW-POL-03" for r in res)


def test_off_topic_question_is_flagged_weak(client):
    res = search_policy("what is the capital of France", k=3, client=client)
    assert all(r["weak_match"] for r in res)


def test_doc_filter(client):
    assert {r["doc_id"] for r in search_policy("refund", k=5, doc_ids=["RW-POL-02"], client=client)} == {"RW-POL-02"}


def test_policy_terms_find_no_show_rules(client):
    # Very loose phrasing ("forgot to collect the scooter") is out of reach of retrieval alone;
    # the agent rewrites questions into policy terms before searching (Phase 4).
    top = cites(search_policy("no-show refund for a booking that was never picked up", k=3, client=client))
    assert any("No-shows" in c for c in top)


def test_search_still_works_without_reranker(client):
    res = search_policy("GOLD grace period", k=3, client=client, rerank=False)
    assert res[0]["rerank_score"] is None and any("Annexure A" in r["citation"] for r in res)


def test_index_is_not_rebuilt_when_inputs_are_unchanged(client):
    assert idx.is_current(client)
    assert idx.build_index(client=client) == client.count(idx.COLLECTION).count
