"""Policy PDFs -> clause-level chunks with citation metadata.

Pipeline per PDF:
  1. Walk each page in reading order: text blocks, tables (rebuilt as markdown), and images.
     Page headers/footers and the logo are dropped.
  2. Images: use the human-verified transcription matching the image's SHA-256
     (data/ocr_verified/transcriptions.yaml); otherwise fall back to RapidOCR and flag
     the chunk `ocr_verified: false`.
  3. Tables continued across a page break (same header row) are merged back into one.
  4. Split into sections at top-level headings ("2. KYC verification", "Annexure A: ..."),
     FAQs into one chunk per question, and long sections at sub-clause boundaries.
     Tables are never split.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pymupdf
import yaml

from app.config import ROOT
from app.rules import load_rules

log = logging.getLogger(__name__)

POLICY_DIR = ROOT / "docs" / "policies"
TRANSCRIPTIONS = ROOT / "data" / "ocr_verified" / "transcriptions.yaml"
CHUNKS_FILE = ROOT / "data" / "policy_chunks.jsonl"

HEADER_BOTTOM, FOOTER_TOP = 60, 800          # PDF points; header band and footer band
MAX_CHUNK_CHARS = 1400                         # ~350 tokens: well inside bge-small's 512-token window
MIN_OCR_CHARS = 15                             # images with less recognisable text are decorative

HEADING_RE = re.compile(r"^(\d+)\.\s+[A-Z][^\n]{0,70}$")       # "2. KYC verification"
ANNEX_RE = re.compile(r"^(Annexure|Appendix)\s+\w+:", re.I)     # "Annexure A: Tier benefits"
SUBCLAUSE_RE = re.compile(r"^\d+\.\d+\s")                       # "1.3 Customers in some tiers..."
DOC_LINE_RE = re.compile(r"Document\s+(RW-POL-\d+)\s+·\s+Version\s+([\d.]+)(?:\s+·\s+Effective\s+([^·\n]+))?")


@dataclass
class Element:
    page: int
    y: float
    kind: str                     # text | table | image
    text: str
    ocr_verified: bool | None = None
    end_page: int | None = None   # set when a table continues onto a later page


@dataclass
class Chunk:
    id: str
    doc_id: str
    doc_title: str
    version: str
    effective: str | None
    applies_to: str | None
    source_file: str
    section: str
    pages: list[int]
    chunk_type: str               # text | table | faq | image
    ocr_verified: bool | None
    text: str
    embed_text: str = field(default="")

    @property
    def citation(self) -> str:
        pages = ", ".join(map(str, self.pages))
        return f"{self.doc_id} v{self.version}, {self.section} (p. {pages})"

    def point_id(self) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, self.id))


# --------------------------------------------------------------------------- page walking

def _load_transcriptions() -> dict:
    if not TRANSCRIPTIONS.exists():
        return {}
    return yaml.safe_load(TRANSCRIPTIONS.read_text(encoding="utf-8")) or {}


_ocr_engine = None


def _ocr(image_bytes: bytes) -> str:
    """Raw OCR fallback: rebuild lines by grouping boxes with similar y, left to right."""
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
    result, _ = _ocr_engine(image_bytes)
    rows: list[list] = []
    for box, text, _conf in sorted(result or [], key=lambda r: r[0][0][1]):
        y, x = box[0][1], box[0][0]
        if rows and abs(rows[-1][0] - y) < 30:
            rows[-1][1].append((x, text))
        else:
            rows.append([y, [(x, text)]])
    return "\n".join(" | ".join(t for _, t in sorted(cells)) for _, cells in rows)


def _image_element(doc: pymupdf.Document, xref: int, page_no: int, y: float, transcriptions: dict,
                   source: str) -> Element | None:
    data = doc.extract_image(xref)["image"]
    key = hashlib.sha256(data).hexdigest()[:16]
    entry = transcriptions.get(key)
    if entry:
        return None if entry.get("skip") else Element(page_no, y, "image", entry["text"].strip(), True)
    text = _ocr(data).strip()
    if len(text) < MIN_OCR_CHARS:
        return None
    log.warning("No verified transcription for image %s on %s p.%d - using raw OCR; please verify.",
                key, source, page_no)
    return Element(page_no, y, "image", f"[Unverified OCR of an image]\n{text}", False)


def _is_header_or_footer(y0: float) -> bool:
    return y0 < HEADER_BOTTOM or y0 > FOOTER_TOP


def _inside(bbox, rect) -> bool:
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return rect[0] <= cx <= rect[2] and rect[1] <= cy <= rect[3]


def extract_elements(pdf: Path) -> tuple[dict, list[Element]]:
    doc = pymupdf.open(pdf)
    transcriptions = _load_transcriptions()
    elements: list[Element] = []
    meta = {"source_file": pdf.name}
    for page_index, page in enumerate(doc):
        page_no = page_index + 1
        tables = page.find_tables().tables
        items: list[Element] = []
        for t in tables:
            items.append(Element(page_no, t.bbox[1], "table", t.to_markdown().strip().replace("<br>", " ")))
        for x0, y0, x1, y1, text, _n, block_type in page.get_text("blocks"):
            if block_type != 0 or _is_header_or_footer(y0) or any(_inside((x0, y0, x1, y1), t.bbox) for t in tables):
                continue
            text = " ".join(line.strip() for line in text.strip().splitlines())
            if text:
                items.append(Element(page_no, y0, "text", text))
        for info in page.get_image_info(xrefs=True):
            if info["bbox"][3] < HEADER_BOTTOM or not info["xref"]:
                continue
            el = _image_element(doc, info["xref"], page_no, info["bbox"][1], transcriptions, pdf.name)
            if el:
                items.append(el)
        items.sort(key=lambda e: e.y)

        for el in items:
            if page_index == 0 and "doc_id" not in meta:
                m = DOC_LINE_RE.search(el.text)
                if m:
                    meta |= {"doc_id": m.group(1), "version": m.group(2),
                             "effective": (m.group(3) or "").strip() or None,
                             "doc_title": elements[-1].text if elements else pdf.stem}
                    elements.pop()          # title line is metadata, not content
                    continue
            prev = elements[-1] if elements else None
            if (el.kind == "table" and prev and prev.kind == "table"
                    and el.text.splitlines()[0] == prev.text.splitlines()[0]):
                prev.text += "\n" + "\n".join(el.text.splitlines()[2:])      # continued table
                prev.end_page = el.page
                continue
            elements.append(el)
    if "doc_id" not in meta:
        raise ValueError(f"Could not find the 'Document RW-POL-xx · Version y' line in {pdf.name}")
    return meta, elements


# --------------------------------------------------------------------------- chunking

def _applies_to(doc_id: str) -> str | None:
    """Refund policy documents: which bookings they govern (from config/policy_rules.yaml)."""
    versions = sorted(load_rules()["refund_policy_versions"], key=lambda v: v["effective_from_ist"])
    for i, v in enumerate(versions):
        if v["document"] == doc_id:
            nxt = versions[i + 1]["effective_from_ist"] if i + 1 < len(versions) else None
            frm = v["effective_from_ist"][:16]
            return (f"bookings made from {frm} IST" + (f" until before {nxt[:16]} IST" if nxt else " onwards")
                    + " (decided by when the booking was made)")
    return None


def _split_long(els: list[Element]) -> list[list[Element]]:
    """Split a long section: group elements into sub-clauses (a clause keeps its tables and
    images), then pack whole clauses into chunks of at most MAX_CHUNK_CHARS where possible."""
    clauses: list[list[Element]] = []
    for el in els:
        if not clauses or (el.kind == "text" and SUBCLAUSE_RE.match(el.text)):
            clauses.append([])
        clauses[-1].append(el)
    parts: list[list[Element]] = []
    for clause in clauses:
        size = sum(len(e.text) for e in clause)
        if parts and sum(len(e.text) for e in parts[-1]) + size <= MAX_CHUNK_CHARS:
            parts[-1].extend(clause)
        else:
            parts.append(list(clause))
    return parts


def _sections(elements: list[Element]) -> list[tuple[str, list[Element]]]:
    sections: list[tuple[str, list[Element]]] = [("Introduction and scope", [])]
    for el in elements:
        is_heading = el.kind == "text" and (HEADING_RE.match(el.text) or ANNEX_RE.match(el.text))
        if is_heading:
            sections.append((el.text.rstrip("."), [el]))
        else:
            sections[-1][1].append(el)
    return [(name, els) for name, els in sections if els]


def build_chunks(pdf: Path) -> list[Chunk]:
    meta, elements = extract_elements(pdf)
    applies_to = _applies_to(meta["doc_id"])
    chunks: list[Chunk] = []

    def add(section: str, els: list[Element], chunk_type: str | None = None):
        text = "\n\n".join(e.text for e in els).strip()
        if not text:
            return
        kinds = {e.kind for e in els}
        ctype = chunk_type or ("image" if "image" in kinds else "table" if "table" in kinds else "text")
        verified = [e.ocr_verified for e in els if e.kind == "image"]
        slug = re.sub(r"[^a-z0-9]+", "-", section.lower()).strip("-")[:60]
        cid = f"{meta['doc_id']}#{slug}"
        n = sum(1 for c in chunks if c.id.split("~")[0] == cid)
        chunks.append(Chunk(
            id=f"{cid}~{n}" if n else cid, doc_id=meta["doc_id"], doc_title=meta["doc_title"],
            version=meta["version"], effective=meta["effective"], applies_to=applies_to,
            source_file=meta["source_file"], section=section,
            pages=sorted({e.page for e in els} | {e.end_page for e in els if e.end_page}),
            chunk_type=ctype, ocr_verified=all(verified) if verified else None, text=text))

    for section, els in _sections(elements):
        if "frequently asked questions" in section.lower():
            q: list[Element] = []
            for el in els[1:]:
                if el.kind == "text" and el.text.startswith("Q.") and q:
                    add(f"{section}: {q[0].text}", q, "faq")
                    q = []
                q.append(el)
            if q:
                add(f"{section}: {q[0].text}", q, "faq")
            continue
        parts = _split_long(els) if sum(len(e.text) for e in els) > MAX_CHUNK_CHARS else [els]
        for i, part in enumerate(parts):
            add(section if i == 0 else f"{section} (continued)", part)

    for c in chunks:
        header = [f"{c.doc_id} {c.doc_title}, version {c.version}"]
        if c.effective:
            header.append(f"effective {c.effective}")
        if c.applies_to:
            header.append(f"applies to {c.applies_to}")
        c.embed_text = " · ".join(header) + f"\nSection: {c.section}\n\n{c.text}"
    return chunks


def build_all_chunks(policy_dir: Path = POLICY_DIR) -> list[Chunk]:
    chunks: list[Chunk] = []
    for pdf in sorted(policy_dir.glob("*.pdf")):
        chunks.extend(build_chunks(pdf))
    return chunks


def write_chunks(chunks: list[Chunk], path: Path = CHUNKS_FILE) -> None:
    with path.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(asdict(c) | {"citation": c.citation}, ensure_ascii=False) + "\n")
