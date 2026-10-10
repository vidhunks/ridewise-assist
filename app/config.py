"""Central configuration. All secrets come from environment variables (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing environment variable {name}. Copy .env.example to .env and fill it in."
        )
    return value


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    embedding_model: str | None
    llm_timeout_seconds: float
    llm_max_retries: int

    # Paths
    source_db: Path = ROOT / "data" / "ridewise.db"
    derived_db: Path = ROOT / "data" / "derived.db"
    policy_dir: Path = ROOT / "docs" / "policies"
    policy_chunks: Path = ROOT / "data" / "policy_chunks.jsonl"
    policy_rules: Path = ROOT / "config" / "policy_rules.yaml"
    log_dir: Path = ROOT / "logs"


def load_settings() -> Settings:
    return Settings(
        llm_base_url=_require("LLM_BASE_URL"),
        llm_api_key=_require("LLM_API_KEY"),
        llm_model=_require("LLM_MODEL"),
        embedding_model=os.getenv("EMBEDDING_MODEL", "").strip() or None,
        llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
        llm_max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
    )
