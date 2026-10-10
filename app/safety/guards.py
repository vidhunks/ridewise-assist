"""Input and output guards around the LLM.

Input: cap length; refuse clear attempts to extract the system prompt or switch roles.
       (Role and customer_id come from the session; the chat can never change them, and the
       tools enforce access regardless - this guard just answers such attempts consistently.)
Output: mask any PII patterns; in customer sessions, redact other customers' IDs.
"""
from __future__ import annotations

import re

from app.safety.masking import mask_text

MAX_INPUT_CHARS = 2000

_INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|rules|prompts?)",
    r"disregard (all |the )?(previous|prior|above|system) (instructions|rules|prompt)",
    r"(reveal|show|print|repeat|tell me) (me )?(your|the) (system|hidden|initial) (prompt|instructions)",
    r"\byou are now (an? )?(admin|agent|developer|support agent|dan)\b",
    r"\b(developer|god|jailbreak|dan) mode\b",
    r"\bact as (an? )?(admin|administrator|support agent|ridewise (staff|employee))\b",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.I)
_CUSTOMER_ID_RE = re.compile(r"\bCUST\d{4}\b", re.I)


def check_input(text: str) -> str | None:
    """Return a refusal message if the input must not go to the model, else None."""
    if not text or not text.strip():
        return "Please type a question."
    if len(text) > MAX_INPUT_CHARS:
        return f"That message is too long ({len(text)} characters). Please keep it under {MAX_INPUT_CHARS}."
    if _INJECTION_RE.search(text):
        return ("I can't change how I work or share my internal instructions. "
                "I'm happy to help with your bookings, payments, charges or RideWise policies.")
    return None


def normalise_typography(text: str) -> str:
    """The model sometimes uses look-alike Unicode: 'B‑1581' (non-breaking hyphen) breaks copy-paste of IDs,
    and narrow spaces inside numbers render as '1 64'."""
    text = re.sub(r"[‐‑]", "-", text)
    text = re.sub(r"(?<=\d)[   ](?=\d)", "", text)
    return re.sub(r"[  ]", " ", text)


def clean_output(text: str, role: str, customer_id: str | None) -> str:
    text = mask_text(normalise_typography(text)) or ""
    text = re.sub(r"【([^】]*)】", r" (\1)", text)      # model-style citation brackets -> plain text
    if role == "customer":
        own = (customer_id or "").upper()
        text = _CUSTOMER_ID_RE.sub(lambda m: m.group(0) if m.group(0).upper() == own else "[another customer]", text)
    return text
