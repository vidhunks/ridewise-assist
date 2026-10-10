"""Sign-in for the two kinds of user.

* Customer: customer ID + the phone number registered on the account (checked against the original
  database - the only place the unmasked number exists).
* Support agent: username + password, checked against a salted PBKDF2 hash in config/users.yaml.

Every failure returns the same message, so the form cannot be used to find out which customer IDs or
usernames exist. Repeated failures lock the ID/username for a while. All attempts are audited.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import yaml

from app.config import ROOT
from app.data.db import open_source
from app.safety.masking import mask_phone
from app.tools.db_tools import NotFound, normalise_customer_id

USERS_FILE = ROOT / "config" / "users.yaml"
AUTH_LOG = ROOT / "logs" / "auth.jsonl"
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60

CUSTOMER_FAIL = "Customer ID or phone number is incorrect."
AGENT_FAIL = "Username or password is incorrect."


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    role: str                 # "customer" | "agent"
    customer_id: str | None = None
    username: str | None = None


# ------------------------------------------------------------------ lockout

_failures: dict[str, list[float]] = {}
_lock = threading.Lock()


def _locked(key: str) -> bool:
    with _lock:
        recent = [t for t in _failures.get(key, []) if time.time() - t < LOCK_SECONDS]
        _failures[key] = recent
        return len(recent) >= MAX_FAILURES


def _record_failure(key: str) -> None:
    with _lock:
        _failures.setdefault(key, []).append(time.time())


def _clear(key: str) -> None:
    with _lock:
        _failures.pop(key, None)


def _audit(role: str, who: str, ok: bool, reason: str = "") -> None:
    try:
        AUTH_LOG.parent.mkdir(exist_ok=True)
        with AUTH_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "role": role,
                                "who": who, "success": ok, "reason": reason}) + "\n")
    except OSError:
        pass


LOCKED_MSG = "Too many failed attempts. Please wait 15 minutes and try again, or contact RideWise Support."


# ------------------------------------------------------------------ customers

def _digits10(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")[-10:]


def authenticate_customer(customer_id: str, phone: str) -> Principal:
    try:
        cid = normalise_customer_id(customer_id or "")
    except NotFound:
        _audit("customer", str(customer_id)[:20], False, "bad id format")
        raise AuthError(CUSTOMER_FAIL)
    key = f"customer:{cid}"
    if _locked(key):
        _audit("customer", cid, False, "locked")
        raise AuthError(LOCKED_MSG)

    src = open_source()
    try:
        row = src.execute("SELECT phone FROM customers WHERE customer_id = ?", (cid,)).fetchone()
    finally:
        src.close()
    given = _digits10(phone)
    ok = row is not None and len(given) == 10 and hmac.compare_digest(given, _digits10(row["phone"]))
    if not ok:
        _record_failure(key)
        _audit("customer", cid, False, f"phone {mask_phone(given) or 'empty'}")
        raise AuthError(CUSTOMER_FAIL)
    _clear(key)
    _audit("customer", cid, True)
    return Principal(role="customer", customer_id=cid)


# ------------------------------------------------------------------ agents

def _load_users() -> dict:
    return yaml.safe_load(USERS_FILE.read_text(encoding="utf-8"))


def hash_password(password: str, salt_hex: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), iterations).hex()


def authenticate_agent(username: str, password: str) -> Principal:
    user = (username or "").strip()
    key = f"agent:{user.lower()}"
    if _locked(key):
        _audit("agent", user[:40], False, "locked")
        raise AuthError(LOCKED_MSG)
    cfg = _load_users()
    entry = (cfg.get("agents") or {}).get(user)
    # Hash even for unknown users so response time doesn't reveal which usernames exist.
    salt = entry["salt"] if entry else "00" * 16
    computed = hash_password(password or "", salt, int(cfg.get("pbkdf2_iterations", 310000)))
    ok = entry is not None and hmac.compare_digest(computed, entry["hash"])
    if not ok:
        _record_failure(key)
        _audit("agent", user[:40], False, "bad credentials")
        raise AuthError(AGENT_FAIL)
    _clear(key)
    _audit("agent", user, True)
    return Principal(role="agent", username=user)
