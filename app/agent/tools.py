"""Tool registry: the functions the LLM may call, their JSON schemas, which roles get them,
and the evidence (sources, SQL, calculations) each call contributes to the answer.

Access control lives in the tool implementations (app/tools/db_tools.py); this module only
decides which tools each role is offered.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from app.config import ROOT
from app.rag.retriever import search_policy
from app.safety.masking import mask_text
from app.tools import calculators as calc
from app.tools import db_tools as db
from app.tools.db_tools import AccessDenied, NotFound, Session
from app.tools.sql_guard import SQLRejected

MAX_TOOL_RESULT_CHARS = 8000
POLICY_DOCS = ["RW-POL-01", "RW-POL-02", "RW-POL-03", "RW-POL-04", "RW-POL-05"]


@dataclass
class ToolCallRecord:
    name: str
    args: dict
    ok: bool
    ms: int
    result: Any = None
    error: str | None = None


@dataclass
class Evidence:
    calls: list[ToolCallRecord] = field(default_factory=list)
    sql: list[str] = field(default_factory=list)
    calculations: list[dict] = field(default_factory=list)
    records: list[str] = field(default_factory=list)          # e.g. "booking_facts:B-0001"
    policy_hits: list[dict] = field(default_factory=list)    # retrieved chunks (citation, text, ...)
    escalations: list[dict] = field(default_factory=list)    # tickets queued for a human


# ------------------------------------------------------------------ implementations

def _calc_for_booking(fn: Callable) -> Callable:
    def run(conn, session: Session, booking_id: str) -> dict:
        bid = db.authorize_booking(conn, session, booking_id)
        res = fn(conn, bid)
        return res if isinstance(res, dict) else res.to_dict()
    return run


def _quote_rental(conn, session: Session, model: str, hours: float) -> dict:
    row = conn.execute("""SELECT model, vehicle_type, hourly_rate, daily_rate FROM vehicles
                          WHERE lower(model) LIKE ? ORDER BY status = 'ACTIVE' DESC LIMIT 1""",
                       (f"%{model.lower()}%",)).fetchone()
    if not row:
        raise NotFound(f"No vehicle model matching '{model}'.")
    res = calc.rental_charge(row["hourly_rate"], row["daily_rate"], float(hours)).to_dict()
    dep = calc.load_rules()["deposit"].get(row["vehicle_type"])
    if dep and float(hours) >= dep["min_rental_hours"]:
        res["notes"].append(f"A refundable security deposit of ₹{dep['amount']:,} may be collected "
                            f"(no GST; RW-POL-01 §5.1).")
    res["inputs"] |= {"model": row["model"], "vehicle_type": row["vehicle_type"]}
    return res


def _estimate_late_fee(conn, session: Session, late_minutes: int, booking_id: str | None = None,
                       model: str | None = None, customer_tier: str | None = None) -> dict:
    if booking_id:
        bid = db.authorize_booking(conn, session, booking_id)
        f = conn.execute("SELECT hourly_rate, customer_tier, model FROM booking_facts WHERE booking_id = ?",
                         (bid,)).fetchone()
        rate, tier, model_name = f["hourly_rate"], f["customer_tier"], f["model"]
    elif model:
        v = conn.execute("SELECT model, hourly_rate FROM vehicles WHERE lower(model) LIKE ? LIMIT 1",
                         (f"%{model.lower()}%",)).fetchone()
        if not v:
            raise NotFound(f"No vehicle model matching '{model}'.")
        rate, model_name = v["hourly_rate"], v["model"]
        tier = customer_tier or "STANDARD"
    else:
        raise NotFound("Give a booking_id or a vehicle model.")
    if session.is_customer:   # a customer's own tier always applies
        tier = conn.execute("SELECT customer_tier FROM customers WHERE customer_id = ?",
                            (session.customer_id,)).fetchone()[0]
    res = calc.late_fee(int(late_minutes), rate, tier).to_dict()
    res["calculation"] = "late_fee_estimate"
    res["inputs"]["model"] = model_name
    return res


ESCALATIONS_FILE = ROOT / "logs" / "escalations.jsonl"


def _request_human_followup(conn, session: Session, reason: str, category: str,
                            booking_id: str | None = None) -> dict:
    """Queue a request for a human support agent (the assistant itself can never change data)."""
    bid = db.authorize_booking(conn, session, booking_id) if booking_id else None
    ticket = f"ESC-{uuid.uuid4().hex[:8].upper()}"
    entry = {"ticket": ticket, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "role": session.role, "customer_id": session.customer_id, "booking_id": bid,
             "category": category, "reason": mask_text(reason), "status": "OPEN"}
    ESCALATIONS_FILE.parent.mkdir(exist_ok=True)
    with ESCALATIONS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"ticket": ticket, "status": "queued for a RideWise support agent",
            "tell_user": "A support agent will follow up; nothing has been changed yet.", "source": f"escalation:{ticket}"}


def _search_policy(conn, session: Session, query: str, documents: list[str] | None = None) -> dict:
    hits = search_policy(query, k=4, doc_ids=[d for d in (documents or []) if d in POLICY_DOCS] or None)
    return {"results": [{k: h[k] for k in ("citation", "relevance", "weak_match", "linked_from",
                                            "applies_to", "ocr_verified", "text")} for h in hits],
            "guidance": "Answer only from these passages. If every result is weak_match or none answers "
                        "the question, say the policies don't cover it."}


# ------------------------------------------------------------------ schemas

def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required,
                       "additionalProperties": False}}}


_BOOKING = {"booking_id": {"type": "string", "description": "Booking ID, e.g. B-0123"}}

TOOLS: dict[str, dict] = {
    # name: {impl, roles, schema}
    "search_policy": {
        "impl": _search_policy, "roles": {"customer", "agent"},
        "schema": _fn("search_policy",
                      "Search RideWise policy documents (rental terms, KYC, deposits, cancellation & refund v1/v2, "
                      "late return & damage penalties, tier benefits, EV guide & FAQ). Write the query in RideWise "
                      "policy terms (e.g. 'no-show refund', 'late return fee grace period', 'security deposit return', "
                      "'KYC pending at pickup'), not the user's exact words. Returns passages with citations.",
                      {"query": {"type": "string"},
                       "documents": {"type": "array", "items": {"type": "string", "enum": POLICY_DOCS},
                                     "description": "Optional filter: 01 rental terms, 02 refund v1, 03 refund v2, "
                                                    "04 penalties, 05 EV guide"}},
                      ["query"])},
    "get_booking": {
        "impl": db.get_booking, "roles": {"customer", "agent"},
        "schema": _fn("get_booking", "Full details of one booking: vehicle, hub, times (UTC and IST), status, "
                      "lateness, refund policy version, amounts paid/refunded, damage claims.", _BOOKING, ["booking_id"])},
    "get_payments": {
        "impl": db.get_payments, "roles": {"customer", "agent"},
        "schema": _fn("get_payments", "All payment records of one booking (rental, deposit, penalties, refunds).",
                      _BOOKING, ["booking_id"])},
    "get_damage_reports": {
        "impl": db.get_damage_reports, "roles": {"customer", "agent"},
        "schema": _fn("get_damage_reports", "Damage reports for a booking (customers: omit booking_id to see all "
                      "of their own).", {"booking_id": {"type": "string"}}, [])},
    "list_vehicles": {
        "impl": db.list_vehicles, "roles": {"customer", "agent"},
        "schema": _fn("list_vehicles", "Fleet by hub with hourly/daily rates. Filters are optional.",
                      {"hub": {"type": "string"},
                       "vehicle_type": {"type": "string", "enum": ["BIKE", "SCOOTER", "EV_SCOOTER", "EV_BIKE"]},
                       "model": {"type": "string"}}, [])},
    "calculate_refund": {
        "impl": _calc_for_booking(calc.refund_for_booking), "roles": {"customer", "agent"},
        "schema": _fn("calculate_refund", "Refund due for a cancelled/no-show booking per the correct policy "
                      "version, compared with what was recorded. For an upcoming booking: refund if cancelled now.",
                      _BOOKING, ["booking_id"])},
    "calculate_late_fee": {
        "impl": _calc_for_booking(calc.late_fee_for_booking), "roles": {"customer", "agent"},
        "schema": _fn("calculate_late_fee", "Late return fee for a booking (tier grace period applied). "
                      "For an ongoing rental past its end time: fee accrued so far.", _BOOKING, ["booking_id"])},
    "calculate_battery_fee": {
        "impl": _calc_for_booking(calc.battery_fee_for_booking), "roles": {"customer", "agent"},
        "schema": _fn("calculate_battery_fee", "EV low-battery charging fee for a returned booking.",
                      _BOOKING, ["booking_id"])},
    "booking_charges_summary": {
        "impl": _calc_for_booking(calc.booking_charges_summary), "roles": {"customer", "agent"},
        "schema": _fn("booking_charges_summary", "Every charge/refund for a booking by policy next to what was "
                      "actually recorded, with mismatches explained. Use for 'why was I charged X' questions.",
                      _BOOKING, ["booking_id"])},
    "quote_rental": {
        "impl": _quote_rental, "roles": {"customer", "agent"},
        "schema": _fn("quote_rental", "Rental charge for a vehicle model and duration (daily-rate cap applied).",
                      {"model": {"type": "string"}, "hours": {"type": "number"}}, ["model", "hours"])},
    "estimate_late_fee": {
        "impl": _estimate_late_fee, "roles": {"customer", "agent"},
        "schema": _fn("estimate_late_fee", "Hypothetical late fee, e.g. 'what if I return 2 hours late'. "
                      "Give booking_id (preferred) or vehicle model.",
                      {"late_minutes": {"type": "integer"}, "booking_id": {"type": "string"},
                       "model": {"type": "string"},
                       "customer_tier": {"type": "string", "enum": ["STANDARD", "SILVER", "GOLD"]}},
                      ["late_minutes"])},
    "request_human_followup": {
        "impl": _request_human_followup, "roles": {"customer", "agent"},
        "schema": _fn("request_human_followup",
                      "Queue a ticket for a human support agent. Call this whenever the user wants something "
                      "changed or decided that you cannot do (cancel/modify a booking, refund, waiver, dispute a "
                      "charge, KYC update), complains, reports an accident/breakdown/safety issue, or you cannot "
                      "resolve the problem. Returns a ticket number to give the user.",
                      {"reason": {"type": "string", "description": "What the user needs, in one sentence."},
                       "category": {"type": "string", "enum": ["cancellation", "refund", "charge_dispute",
                                                               "damage_dispute", "kyc", "safety", "complaint",
                                                               "booking_change", "other"]},
                       "booking_id": {"type": "string"}},
                      ["reason", "category"])},
    # customer only
    "get_my_profile": {
        "impl": db.get_my_profile, "roles": {"customer"},
        "schema": _fn("get_my_profile", "The customer's own profile: tier, KYC status, masked contact details, "
                      "booking counts.", {}, [])},
    "list_my_bookings": {
        "impl": db.list_my_bookings, "roles": {"customer"},
        "schema": _fn("list_my_bookings", "The customer's own bookings, newest first.",
                      {"status": {"type": "string", "enum": ["upcoming", "ongoing", "completed", "cancelled", "no-show"]},
                       "limit": {"type": "integer"}}, [])},
    # agent only
    "find_customer": {
        "impl": db.find_customer, "roles": {"agent"},
        "schema": _fn("find_customer", "Find customers by ID, name (partial) or exact phone/email.",
                      {"query_text": {"type": "string"}}, ["query_text"])},
    "get_customer_profile": {
        "impl": db.get_customer_profile, "roles": {"agent"},
        "schema": _fn("get_customer_profile", "A customer's profile (masked).",
                      {"customer_id": {"type": "string"}}, ["customer_id"])},
    "list_customer_bookings": {
        "impl": db.list_customer_bookings, "roles": {"agent"},
        "schema": _fn("list_customer_bookings", "A customer's bookings, newest first.",
                      {"customer_id": {"type": "string"},
                       "status": {"type": "string", "enum": ["upcoming", "ongoing", "completed", "cancelled", "no-show"]},
                       "limit": {"type": "integer"}}, ["customer_id"])},
    "run_sql": {
        "impl": db.run_sql, "roles": {"agent"},
        "schema": _fn("run_sql", "Read-only SQLite SELECT over the masked tables (schema in the system prompt) "
                      "for counts, totals, lists and comparisons across bookings. Max 200 rows.",
                      {"sql": {"type": "string"}}, ["sql"])},
}

FINAL_ANSWER = _fn(
    "final_answer",
    "Deliver the reply to the user. Always end your turn by calling this.",
    {"answer": {"type": "string", "description": "The reply, in plain language (markdown allowed)."},
     "cited_policies": {"type": "array", "items": {"type": "string"},
                        "description": "Citations of the policy passages you relied on, copied exactly."},
     "escalate": {"type": "boolean", "description": "True if you called request_human_followup this turn."}},
    ["answer"],
)


def tool_schemas(role: str) -> list[dict]:
    return [t["schema"] for t in TOOLS.values() if role in t["roles"]] + [FINAL_ANSWER]


# ------------------------------------------------------------------ dispatch

def _truncate(payload: dict) -> str:
    """Serialise for the model; if too long, shorten the longest lists (keeps valid JSON)."""
    dump = lambda p: json.dumps(p, ensure_ascii=False, default=str)  # noqa: E731
    text = dump(payload)
    if len(text) <= MAX_TOOL_RESULT_CHARS or not isinstance(payload, dict):
        return text
    payload = dict(payload)
    lists = [k for k, v in payload.items() if isinstance(v, list) and len(v) > 1]
    original = {k: len(payload[k]) for k in lists}
    while len(text) > MAX_TOOL_RESULT_CHARS and any(len(payload[k]) > 1 for k in lists):
        k = max(lists, key=lambda key: len(dump(payload[key])))
        payload[k] = payload[k][: max(1, len(payload[k]) // 2)]
        payload["truncated_note"] = (f"Showing {', '.join(f'{len(payload[x])} of {original[x]} {x}' for x in lists)}. "
                                     "Narrow the query or aggregate for the rest.")
        text = dump(payload)
    return text if len(text) <= MAX_TOOL_RESULT_CHARS else dump(
        {"error": "Result too large to show. Narrow the query or aggregate."})


def call_tool(name: str, raw_args: str, conn, session: Session, ev: Evidence) -> str:
    """Run one tool call and return the JSON string handed back to the model."""
    t0 = time.perf_counter()
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        args = {}
    spec = TOOLS.get(name)
    try:
        if spec is None:
            raise NotFound(f"Unknown tool '{name}'.")
        if session.role not in spec["roles"]:
            raise AccessDenied("This information is only available to RideWise support staff."
                               if session.is_customer else f"{name} is for customer sessions only.")
        result = spec["impl"](conn, session, **args)
        _collect(name, args, result, ev)
        ev.calls.append(ToolCallRecord(name, args, True, int((time.perf_counter() - t0) * 1000), result))
        return _truncate(result)
    except (AccessDenied, NotFound, SQLRejected, LookupError, TypeError, ValueError) as e:
        msg = str(e) if not isinstance(e, TypeError) else f"Bad arguments for {name}: {e}"
        ev.calls.append(ToolCallRecord(name, args, False, int((time.perf_counter() - t0) * 1000), error=msg))
        return json.dumps({"error": msg, "error_type": type(e).__name__})
    except Exception as e:     # never crash the turn on a tool bug; let the model recover
        ev.calls.append(ToolCallRecord(name, args, False, int((time.perf_counter() - t0) * 1000),
                                       error=f"{type(e).__name__}: {e}"))
        return json.dumps({"error": f"The {name} tool failed ({type(e).__name__}). Try another approach "
                                    f"or tell the user the information is temporarily unavailable."})


def _collect(name: str, args: dict, result: dict, ev: Evidence) -> None:
    if name == "run_sql":
        ev.sql.append(result["sql"])
    elif name == "search_policy":
        for h in result["results"]:
            if not h["weak_match"] and h["citation"] not in {p["citation"] for p in ev.policy_hits}:
                ev.policy_hits.append(h)
    elif name in ("calculate_refund", "calculate_late_fee", "calculate_battery_fee", "quote_rental",
                  "estimate_late_fee"):
        ev.calculations.append(result)
    elif name == "booking_charges_summary":
        ev.calculations.extend(v for v in result["policy_amounts"].values() if "steps" in v)
        ev.records.append(f"booking_facts:{result['booking_id']}")
    elif name == "request_human_followup":
        ev.escalations.append({"ticket": result["ticket"], "category": args.get("category"),
                               "reason": args.get("reason")})
    src = result.get("source") if isinstance(result, dict) else None
    if src and src != "sql" and src not in ev.records:
        ev.records.append(src)
