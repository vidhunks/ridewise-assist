"""Loads config/policy_rules.yaml - the single source of truth for policy numbers."""
from __future__ import annotations

from functools import lru_cache

import yaml

from app.config import ROOT

RULES_FILE = ROOT / "config" / "policy_rules.yaml"


@lru_cache(maxsize=1)
def load_rules() -> dict:
    return yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))
