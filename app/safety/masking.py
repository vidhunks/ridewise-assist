"""Masking helpers for personal data (PII).

Rule of thumb: keep just enough to recognise the value ("ends in 2345"),
never enough to reuse it.
"""
from __future__ import annotations

import re
from datetime import date

EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
# Indian mobile numbers: 10 digits starting 6-9, optionally prefixed +91 / 0.
PHONE_RE = re.compile(r"(?<!\d)(?:\+?91[\s-]?|0)?([6-9]\d)\d{6}(\d{2})(?!\d)")
# Driving licence: 2-letter state code + 13 digits (e.g. KA0120160012345), spaces/dashes allowed.
LICENSE_RE = re.compile(r"\b[A-Z]{2}[\s-]?\d{2}[\s-]?\d{4}[\s-]?\d{7}\b")


def mask_email(email: str | None) -> str | None:
    if not email:
        return email
    m = EMAIL_RE.fullmatch(email.strip())
    return f"{m.group(1)}***@{m.group(2)}" if m else "***"


def mask_phone(phone: str | None) -> str | None:
    if not phone:
        return phone
    digits = re.sub(r"\D", "", phone)[-10:]
    return f"{digits[:2]}XXXXXX{digits[-2:]}" if len(digits) == 10 else "XXXXXXXXXX"


def mask_license(license_no: str | None) -> str | None:
    """Show only the last 4 characters: KA0120160012345 -> XXXXXXXXXXX2345."""
    if not license_no:
        return license_no
    s = re.sub(r"[\s-]", "", license_no)
    return "X" * max(len(s) - 4, 0) + s[-4:]


def mask_dob(dob: str | None) -> str | None:
    """Keep the year only: 1998-04-12 -> 1998-XX-XX."""
    if not dob:
        return dob
    return f"{dob[:4]}-XX-XX" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", dob) else "XXXX-XX-XX"


def age_on(dob: str | None, today: date) -> int | None:
    if not dob or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dob):
        return None
    y, m, d = map(int, dob.split("-"))
    return today.year - y - ((today.month, today.day) < (m, d))


PUBLIC_EMAIL_DOMAINS = {"ridewise.example"}   # the company's own published contact addresses


def mask_text(text: str | None) -> str | None:
    """Mask emails, phone numbers and licence numbers inside free text."""
    if not text:
        return text
    text = EMAIL_RE.sub(lambda m: m.group(0) if m.group(2).lower() in PUBLIC_EMAIL_DOMAINS
                        else f"{m.group(1)}***@{m.group(2)}", text)
    text = LICENSE_RE.sub(lambda m: mask_license(m.group(0)), text)
    text = PHONE_RE.sub(lambda m: f"{m.group(1)}XXXXXX{m.group(2)}", text)
    return text


def contains_pii(text: str | None) -> bool:
    """True if unmasked PII patterns remain (used by tests and the output guard)."""
    if not text:
        return False
    emails = [m for m in EMAIL_RE.finditer(text) if m.group(2).lower() not in PUBLIC_EMAIL_DOMAINS]
    return bool(emails or PHONE_RE.search(text) or LICENSE_RE.search(text))
