"""Smoke test: confirms .env is set up and the LLM (and tool calling) responds.

Run from the project root:  python -m scripts.check_llm
"""
from __future__ import annotations

import json
import time

from app.config import load_settings
from app.llm import chat


def main() -> None:
    s = load_settings()
    print(f"Endpoint: {s.llm_base_url}\nModel:    {s.llm_model}")

    t = time.perf_counter()
    msg = chat([{"role": "user", "content": "Reply with exactly: pong"}])
    print(f"Plain call  ({time.perf_counter() - t:.1f}s): {msg.content!r}")

    tools = [{
        "type": "function",
        "function": {
            "name": "get_booking",
            "description": "Look up a booking by its ID.",
            "parameters": {
                "type": "object",
                "properties": {"booking_id": {"type": "string"}},
                "required": ["booking_id"],
            },
        },
    }]
    t = time.perf_counter()
    msg = chat([{"role": "user", "content": "What is the status of booking B-0001?"}], tools=tools)
    calls = [(c.function.name, json.loads(c.function.arguments)) for c in (msg.tool_calls or [])]
    print(f"Tool call   ({time.perf_counter() - t:.1f}s): {calls or 'NONE - model did not call the tool'}")


if __name__ == "__main__":
    main()
