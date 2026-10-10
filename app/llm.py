"""Thin wrapper around the NeoStats-hosted, OpenAI-compatible LLM.

Handles timeouts and retries so a slow or rate-limited model degrades gracefully
instead of crashing the app.
"""
from __future__ import annotations

from functools import lru_cache

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI, RateLimitError

from app.config import load_settings


class LLMUnavailable(RuntimeError):
    """Raised when the model cannot be reached after retries."""


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    s = load_settings()
    # The SDK retries 429/5xx/connection errors with exponential backoff.
    return OpenAI(
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        timeout=s.llm_timeout_seconds,
        max_retries=s.llm_max_retries,
    )


def chat(messages: list[dict], **kwargs):
    """Call chat.completions and return the first choice's message."""
    s = load_settings()
    try:
        resp = get_client().chat.completions.create(model=s.llm_model, messages=messages, **kwargs)
    except (APITimeoutError, APIConnectionError, RateLimitError) as e:
        raise LLMUnavailable(f"The assistant model is busy or unreachable ({type(e).__name__}).") from e
    except APIError as e:
        raise LLMUnavailable(f"The assistant model returned an error ({e.__class__.__name__}).") from e
    return resp.choices[0].message
