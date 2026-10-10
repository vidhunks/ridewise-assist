"""Terminal chat for testing the assistant without the UI.

  python -m scripts.chat_cli customer CUST0001 "show my last 3 bookings" "what about the first one?"
  python -m scripts.chat_cli agent - "which hub had the most late returns?"
With no questions given, starts an interactive prompt.
"""
from __future__ import annotations

import sys

from app.agent.loop import Assistant
from app.rag.index import ensure_index
from scripts.build_derived import ensure_built


def show(resp) -> None:
    print(f"\nASSISTANT [{resp.status}, {resp.latency_ms} ms{', ESCALATE: ' + str(resp.escalation_reason) if resp.escalate else ''}]")
    print(resp.answer)
    print("  tools:", ", ".join(f"{t.name}{'' if t.ok else '(ERR: ' + str(t.error) + ')'}" for t in resp.tool_calls) or "-")
    for s in resp.sources:
        print(f"  source [{s.type}] {s.ref}" + (f" :: {s.detail[:120]}" if s.type != "policy" and s.detail else ""))


def main() -> None:
    role, cid, *questions = sys.argv[1:] + [None] * (2 - len(sys.argv[1:]))
    ensure_built()
    ensure_index()
    bot = Assistant(role or "agent", None if cid in (None, "-") else cid)
    if questions and questions[0]:
        for q in questions:
            print(f"\nUSER: {q}")
            show(bot.ask(q))
        return
    while True:
        try:
            q = input("\nYou: ")
        except (EOFError, KeyboardInterrupt):
            break
        if q.strip().lower() in ("exit", "quit"):
            break
        show(bot.ask(q))


if __name__ == "__main__":
    main()
