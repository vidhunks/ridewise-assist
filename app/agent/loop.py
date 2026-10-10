"""The assistant: one Session per conversation, one tool-calling loop per user message.

Turn flow:
  input guard -> [system prompt + recent history + question] -> LLM <-> tools (max MAX_STEPS)
  -> reply text -> output guard -> AssistantResponse (sources/SQL/calculations come from the
  tool calls actually made, not from the model's claims) -> log.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone

from app.agent.prompts import SUPPORT_CONTACT, system_prompt
from app.agent.schema import AssistantResponse, Calculation, Source, ToolCall
from app.agent.tools import Evidence, call_tool, tool_schemas
from app.config import ROOT
from app.data.db import open_derived
from app.llm import LLMUnavailable, chat
from app.safety.guards import check_input, clean_output
from app.safety.masking import mask_text
from app.tools.db_tools import NotFound, Session, normalise_customer_id

log = logging.getLogger(__name__)

MAX_STEPS = 8            # LLM calls per user message

# Messages that may need a human. If the model answers one without escalating, it is asked once to
# reconsider (the model decides; this only makes sure it considered it).
NEEDS_HUMAN_RE = re.compile(
    r"\b(cancel|reschedul|modify|change my|extend|refund|waive|waiver|dispute|not my fault|overcharg|wrong(ly)? "
    r"charg|complain|unfair|broke ?down|breakdown|accident|crash|injur|stolen|theft|punctur|flat tyre|"
    r"ran out|died|stuck|emergency|unsafe|delete|remove)", re.I)
_ESCALATION_CHECK = ("(Internal check, not from the user: does the user's last message ask for something you "
                     "cannot do yourself - a cancellation or change, a refund or waiver, a dispute or complaint, or "
                     "a breakdown/accident/safety issue? If yes, call request_human_followup now, then give your "
                     "final answer to the user with the ticket number. If it was only a question, reply 'NO'.)")
HISTORY_MESSAGES = 12    # last 6 exchanges are replayed for context
LOG_FILE = ROOT / "logs" / "conversations.jsonl"


_FINAL_JSON_RE = re.compile(r'\{\s*"answer"\s*:')


def _final_from_text(text: str) -> dict:
    """The model occasionally wraps its reply in a JSON object ({"answer": ...}).
    Use the last such object if there is one; otherwise the text is the answer."""
    decoder = json.JSONDecoder()
    for m in reversed(list(_FINAL_JSON_RE.finditer(text))):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
            if isinstance(obj, dict) and isinstance(obj.get("answer"), str):
                return obj
        except json.JSONDecodeError:
            continue
    return {"answer": text.strip(), "escalate": False}


class Assistant:
    def __init__(self, role: str, customer_id: str | None = None):
        if role not in ("customer", "agent"):
            raise ValueError("role must be 'customer' or 'agent'")
        self.conn = open_derived()
        self.profile = None
        if role == "customer":
            try:
                customer_id = normalise_customer_id(customer_id or "")
            except NotFound as e:
                raise ValueError(str(e)) from e
            row = self.conn.execute("SELECT * FROM customers WHERE customer_id = ?", (customer_id,)).fetchone()
            if row is None:
                raise ValueError(f"No customer with ID {customer_id}.")
            self.profile = dict(row)
        self.session = Session(role=role, customer_id=customer_id)
        self.session_id = uuid.uuid4().hex[:12]

    # ------------------------------------------------------------------ public

    def ask(self, question: str) -> AssistantResponse:
        t0 = time.perf_counter()
        refusal = check_input(question)
        if refusal:
            resp = AssistantResponse(answer=refusal, status="refused")
        else:
            try:
                resp = self._run(question.strip())
            except LLMUnavailable as e:
                log.warning("LLM unavailable: %s", e)
                resp = AssistantResponse(
                    status="error",
                    answer="Sorry, I'm having trouble reaching the assistant service right now. Please try again "
                           f"in a minute. For urgent help, contact RideWise Support: {SUPPORT_CONTACT}.")
        resp.latency_ms = int((time.perf_counter() - t0) * 1000)
        if resp.status != "error":
            self.session.history += [{"role": "user", "content": question},
                                     {"role": "assistant", "content": resp.answer}]
        self._log(question, resp)
        return resp

    # ------------------------------------------------------------------ internals

    def _run(self, question: str) -> AssistantResponse:
        ev = Evidence()
        messages = [{"role": "system", "content": system_prompt(self.session.role, self.session.last_booking_id,
                                                                self.profile)}]
        messages += self.session.history[-HISTORY_MESSAGES:]
        messages.append({"role": "user", "content": question})
        tools = tool_schemas(self.session.role)

        answer: str | None = None
        before_check: str | None = None
        nudged = checked = False
        needs_check = bool(NEEDS_HUMAN_RE.search(question))
        for _ in range(MAX_STEPS):
            msg = chat(messages, tools=tools)
            calls = msg.tool_calls or []
            if calls:
                messages.append({"role": "assistant", "content": msg.content,
                                 "tool_calls": [{"id": c.id, "type": "function",
                                                 "function": {"name": c.function.name,
                                                              "arguments": c.function.arguments}} for c in calls]})
                for c in calls:
                    messages.append({"role": "tool", "tool_call_id": c.id,
                                     "content": call_tool(c.function.name, c.function.arguments,
                                                          self.conn, self.session, ev)})
                continue

            text = _final_from_text(msg.content or "")["answer"].strip()
            if not text:
                # The hosted model occasionally returns an empty message (e.g. a dropped tool call).
                if nudged:
                    break
                nudged = True
                messages.append({"role": "user", "content": "(Please answer my last question now, using the "
                                                            "tool results above.)"})
                continue
            if needs_check and not checked and not ev.escalations:
                checked, before_check = True, text
                messages += [{"role": "assistant", "content": msg.content},
                             {"role": "user", "content": _ESCALATION_CHECK}]
                continue
            answer = text
            break

        if checked:
            # The re-check only matters if it raised a ticket; its reply otherwise isn't meant for the user.
            if not ev.escalations or not answer or answer.strip().upper().startswith("NO"):
                answer = before_check + (self._ticket_note(ev) if ev.escalations else "")
        if not answer:
            answer = ("Sorry, I couldn't complete that request. Could you rephrase it, or include the booking ID? "
                      f"You can also reach RideWise Support: {SUPPORT_CONTACT}.")
        return self._build_response(answer, ev)

    @staticmethod
    def _ticket_note(ev: Evidence) -> str:
        tickets = ", ".join(e["ticket"] for e in ev.escalations)
        return f"\n\nI've passed this to a RideWise support agent (ticket {tickets}); nothing has been changed yet."

    def _build_response(self, answer: str, ev: Evidence) -> AssistantResponse:
        sources: list[Source] = [Source(type="database", ref=r) for r in ev.records]
        sources += [Source(type="database", ref="SQL query", detail=s) for s in ev.sql]
        # Policy sources: the strongest passages actually retrieved this turn (weak matches excluded).
        sources += [Source(type="policy", ref=h["citation"], detail=h["text"][:600]) for h in ev.policy_hits[:4]]

        calcs = [Calculation(name=c.get("calculation", "calculation"), total=c.get("total"),
                             steps=c.get("steps", []), sources=c.get("sources", []), notes=c.get("notes", []))
                 for c in ev.calculations]
        sources += [Source(type="calculation", ref=c.name, detail=" → ".join(c.steps)) for c in calcs]

        sources += [Source(type="database", ref=f"escalation:{e['ticket']}", detail=e["reason"])
                    for e in ev.escalations]
        return AssistantResponse(
            answer=clean_output(answer, self.session.role, self.session.customer_id),
            sources=sources, sql=ev.sql, calculations=calcs,
            # Escalation is what actually happened (a ticket was queued), not what the model says.
            escalate=bool(ev.escalations),
            escalation_reason="; ".join(f"{e['ticket']} ({e['category']}): {e['reason']}"
                                        for e in ev.escalations) or None,
            tool_calls=[ToolCall(name=c.name, args=c.args, ok=c.ok, ms=c.ms, error=c.error) for c in ev.calls],
        )

    def _log(self, question: str, resp: AssistantResponse) -> None:
        try:
            LOG_FILE.parent.mkdir(exist_ok=True)
            entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "session": self.session_id,
                     "role": self.session.role, "customer_id": self.session.customer_id,
                     "question": mask_text(question), "answer": resp.answer, "status": resp.status,
                     "escalate": resp.escalate, "latency_ms": resp.latency_ms,
                     "tools": [t.model_dump() for t in resp.tool_calls], "sql": resp.sql}
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError as e:
            log.warning("Could not write conversation log: %s", e)

    def close(self) -> None:
        self.conn.close()
