"""Deterministic money calculators. The LLM never does arithmetic: it calls these.

Every number comes from config/policy_rules.yaml, and every result carries its
working (`steps`) and the clauses it relied on (`sources`) so answers can be checked.

Pure functions (numbers in, CalcResult out) are at the top; booking-level helpers
that read data/derived.db are at the bottom.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from app.rules import load_rules

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def rupees(x: float) -> str:
    return f"₹{x:,.0f}" if float(x).is_integer() else f"₹{x:,.2f}"


def _r2(x: float) -> float:
    return round(x + 0.0, 2)


@dataclass
class CalcResult:
    calculation: str
    amount: float                      # before GST
    gst: float
    total: float                       # amount + GST
    steps: list[str]
    sources: list[str]
    inputs: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _with_gst(amount: float, rules: dict) -> tuple[float, float]:
    gst = _r2(amount * rules["gst_rate"])
    return gst, _r2(amount + gst)


# --------------------------------------------------------------------------- pure functions

def rental_charge(hourly_rate: float, daily_rate: float, hours: float, rules: dict | None = None) -> CalcResult:
    """RW-POL-01 §4.1: full 24h blocks at the daily rate; remaining hours at the hourly rate,
    capped at one daily rate."""
    rules = rules or load_rules()
    r = rules["rental"]
    notes = []
    billable_hours = math.ceil(hours)
    if billable_hours != hours:
        notes.append(f"{hours} h rounded up to {billable_hours} h (rentals are booked in whole hours, RW-POL-01 §3.2).")
    if billable_hours < r["minimum_hours"]:
        notes.append(f"Minimum rental is {r['minimum_hours']} hours (RW-POL-01 §3.2); charged as {r['minimum_hours']} h.")
        billable_hours = r["minimum_hours"]

    days, rem = divmod(billable_hours, r["hours_per_day"])
    rem_charge_raw = rem * hourly_rate
    rem_charge = min(rem_charge_raw, daily_rate)
    amount = _r2(days * daily_rate + rem_charge)

    steps = []
    if days:
        steps.append(f"{days} full day(s) × {rupees(daily_rate)} = {rupees(days * daily_rate)}")
    if rem:
        line = f"{rem} remaining hour(s) × {rupees(hourly_rate)} = {rupees(rem_charge_raw)}"
        if rem_charge < rem_charge_raw:
            line += f", capped at one daily rate {rupees(daily_rate)}"
        steps.append(line)
    steps.append(f"Rental charge = {rupees(amount)} (+18% GST)")
    gst, total = _with_gst(amount, rules)
    return CalcResult("rental_charge", amount, gst, total, steps, ["RW-POL-01 §4.1", "RW-POL-01 §4.3"],
                      {"hourly_rate": hourly_rate, "daily_rate": daily_rate, "hours": hours}, notes)


def late_fee(late_minutes: int, hourly_rate: float, customer_tier: str, rules: dict | None = None) -> CalcResult:
    """RW-POL-04 §1.2-1.3 + Annexure A."""
    rules = rules or load_rules()
    lf = rules["late_fee"]
    tier = (customer_tier or "STANDARD").upper()
    grace = lf["grace_minutes_by_tier"].get(tier, 0)
    per_hour = _r2(hourly_rate * lf["multiplier"])
    inputs = {"late_minutes": late_minutes, "hourly_rate": hourly_rate, "customer_tier": tier}
    sources = ["RW-POL-04 §1.2", "RW-POL-04 §1.3"] + (["RW-POL-04 Annexure A"] if grace else [])

    if late_minutes <= 0:
        return CalcResult("late_fee", 0, 0, 0, ["Returned on time: no late fee."], sources, inputs)
    if late_minutes <= grace:
        return CalcResult("late_fee", 0, 0, 0, [
            f"Returned {late_minutes} min late; {tier} tier has a {grace}-minute grace period "
            f"(up to and including {grace} min), so no late fee."], sources, inputs)

    hours = math.ceil(late_minutes / lf["billing_unit_minutes"])
    amount = _r2(hours * per_hour)
    steps = [f"Returned {_fmt_duration(late_minutes)} late; each hour or part-hour counts → {hours} hour(s)"]
    if grace:
        steps.append(f"Lateness is over the {tier} grace period of {grace} min, so the full lateness is charged")
    steps += [f"Late fee per hour = {lf['multiplier']} × {rupees(hourly_rate)} = {rupees(per_hour)}",
              f"{hours} × {rupees(per_hour)} = {rupees(amount)} (+18% GST)"]
    gst, total = _with_gst(amount, rules)
    return CalcResult("late_fee", amount, gst, total, steps, sources, inputs)


def low_battery_fee(return_battery_pct: int | None, is_ev: bool, rules: dict | None = None) -> CalcResult:
    """RW-POL-04 §2 + RW-POL-05 Figure 2 (exactly 10% is not below 10%)."""
    rules = rules or load_rules()
    b = rules["low_battery_fee"]
    inputs = {"return_battery_pct": return_battery_pct, "is_ev": is_ev}
    src = ["RW-POL-04 §2", "RW-POL-05 §4 (Figure 2)"]
    if not is_ev:
        return CalcResult("low_battery_fee", 0, 0, 0, ["Not an EV: battery fee does not apply."], src, inputs)
    if return_battery_pct is None:
        return CalcResult("low_battery_fee", 0, 0, 0, ["No return battery level recorded."], src, inputs,
                          ["Battery level is only recorded once an EV is returned."])
    if return_battery_pct < b["below_pct"]:
        gst, total = _with_gst(b["fee"], rules)
        return CalcResult("low_battery_fee", b["fee"], gst, total, [
            f"Returned at {return_battery_pct}%, which is below {b['below_pct']}% → flat fee {rupees(b['fee'])} (+18% GST)"],
            src, inputs)
    return CalcResult("low_battery_fee", 0, 0, 0, [
        f"Returned at {return_battery_pct}%, which is not below {b['below_pct']}% → no fee"], src, inputs)


def refund_policy_version_for(booked_at_utc: str, rules: dict | None = None) -> dict:
    """Which refund policy version applies, based on when the booking was made (in IST)."""
    rules = rules or load_rules()
    booked_ist = _to_ist(_parse(booked_at_utc), rules).strftime(TS_FORMAT)
    versions = sorted(rules["refund_policy_versions"], key=lambda v: v["effective_from_ist"])
    chosen = versions[0]
    for v in versions:
        if booked_ist >= v["effective_from_ist"]:
            chosen = v
    return chosen


def refund(version: dict, booking_status: str, notice_minutes: int | None,
           rental_paid: float, rental_gst_paid: float, rules: dict | None = None) -> CalcResult:
    """Refund of the rental charge (deposits are separate and returned in full).

    `notice_minutes` = scheduled pickup - cancellation time.
    """
    rules = rules or load_rules()
    special = rules["refund_special_cases"]
    doc = f"{version['document']} ({version['id']})"
    inputs = {"policy_version": version["id"], "booking_status": booking_status,
              "notice_minutes": notice_minutes, "rental_paid": rental_paid, "rental_gst_paid": rental_gst_paid}
    sources = [f"{version['document']} {version['clause']}", f"{version['document']} §1.2"]
    notes = ["Security deposits are returned in full on cancellation and are not part of this refund (RW-POL-02 §4.2).",
             special["processing"]]

    if booking_status == "CNL_S":
        pct, steps = special["cancelled_by_ridewise_pct"], ["Cancelled by RideWise → full refund whatever the timing"]
        sources = [f"{version['document']} §2.1"]
    elif booking_status == "NSH":
        pct, steps = special["no_show_pct"], ["No-show is not a cancellation → no refund (and no extra fee)"]
        sources = [f"{version['document']} §3.1", "RW-POL-01 §7"]
    elif notice_minutes is None:
        return CalcResult("refund", 0, 0, 0, ["Booking is not cancelled, so no refund is due."], sources, inputs)
    elif notice_minutes < 0:
        pct, steps = 0, [f"Cancelled {_fmt_duration(-notice_minutes)} after the scheduled pickup → no refund"]
    else:
        slab = next(s for s in version["slabs"] if notice_minutes >= s["min_hours_before"] * 60)
        pct = slab["refund_pct"]
        steps = [f"Booking made under {doc}",
                 f"Cancelled {_fmt_duration(notice_minutes)} before scheduled pickup",
                 f"Slab: {_slab_label(version['slabs'], slab)} → {pct}% refund"]

    amount = _r2(rental_paid * pct / 100)
    gst = _r2(rental_gst_paid * pct / 100) if special["refund_gst_proportionally"] else 0.0
    steps.append(f"{pct}% of rental paid {rupees(rental_paid)} = {rupees(amount)}"
                 + (f", plus GST refunded {rupees(gst)} → total {rupees(_r2(amount + gst))}" if gst else ""))
    return CalcResult("refund", amount, gst, _r2(amount + gst), steps, sources, inputs | {"refund_pct": pct}, notes)


# --------------------------------------------------------------------------- helpers

def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, TS_FORMAT)


def _to_ist(dt: datetime, rules: dict) -> datetime:
    return dt + timedelta(minutes=rules["timezone"]["policy_utc_offset_minutes"])


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _fmt_duration(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h} h {m} min"
    return f"{h} h" if h else f"{m} min"


def _slab_label(slabs: list[dict], slab: dict) -> str:
    ordered = sorted(slabs, key=lambda s: s["min_hours_before"], reverse=True)
    i = ordered.index(slab)
    lo = slab["min_hours_before"]
    if i == 0:
        return f"{lo} h or more"
    hi = ordered[i - 1]["min_hours_before"]
    return f"less than {hi} h" if lo == 0 else f"{lo} h or more but less than {hi} h"


# --------------------------------------------------------------------------- booking-level helpers

def _facts(conn: sqlite3.Connection, booking_id: str) -> dict:
    row = conn.execute("SELECT * FROM booking_facts WHERE booking_id = ?", (booking_id,)).fetchone()
    if row is None:
        raise LookupError(f"Booking {booking_id} not found.")
    return dict(row)


def refund_for_booking(conn: sqlite3.Connection, booking_id: str, now_utc: datetime | None = None) -> CalcResult:
    """Refund due for a booking. For an upcoming booking, answers "what if I cancel now?"."""
    rules = load_rules()
    f = _facts(conn, booking_id)
    version = next(v for v in rules["refund_policy_versions"] if v["id"] == f["refund_policy_version"])
    status, notice = f["booking_status"], f["cancel_notice_minutes"]
    hypothetical = False
    if status == "UPC":
        now = now_utc or _now_utc()
        notice = int((_parse(f["scheduled_start_utc"]) - now).total_seconds() // 60)
        status, hypothetical = "CNL_U", True

    res = refund(version, status, notice, f["rental_paid"], f["rental_gst_paid"], rules)
    res.inputs["booking_id"] = booking_id
    if hypothetical:
        res.calculation = "refund_if_cancelled_now"
        res.steps.insert(0, "Booking is upcoming: this is what the refund would be if cancelled now")
    elif status in ("CNL_U", "CNL_S", "NSH"):
        recorded = f["refunded"]
        if abs(recorded - res.amount) <= 0.01:
            res.notes.append(f"Matches the refund recorded in payments ({rupees(recorded)}).")
        elif recorded == 0 and f["cancelled_at_utc"]:
            res.notes.append(f"No refund is recorded yet. The booking was cancelled on {f['cancelled_at_ist']} IST; "
                             "refunds are initiated within 3 working days of cancellation (RW-POL-02 §4.1).")
        else:
            res.notes.append(f"Recorded refunds for this booking total {rupees(recorded)}, "
                             f"which differs from the policy amount {rupees(res.amount)}.")
    return res


def late_fee_for_booking(conn: sqlite3.Connection, booking_id: str, now_utc: datetime | None = None) -> CalcResult:
    f = _facts(conn, booking_id)
    late, estimate = f["late_minutes"], False
    if late is None and f["booking_status"] == "ONG":
        now = now_utc or _now_utc()
        late = max(0, int((now - _parse(f["scheduled_end_utc"])).total_seconds() // 60))
        estimate = True
    if late is None:
        return CalcResult("late_fee", 0, 0, 0, [f"Booking status is {f['status_label']}: no return, so no late fee."],
                          ["RW-POL-04 §1.1"], {"booking_id": booking_id})
    res = late_fee(late, f["hourly_rate"], f["customer_tier"])
    res.inputs["booking_id"] = booking_id
    res.notes.append("Uses the customer's current tier; the database does not keep tier history.")
    if estimate:
        res.calculation = "late_fee_so_far"
        res.steps.insert(0, "Vehicle not yet returned: late fee accrued so far, if returned now")
    return res


def battery_fee_for_booking(conn: sqlite3.Connection, booking_id: str) -> CalcResult:
    f = _facts(conn, booking_id)
    res = low_battery_fee(f["return_battery_pct"], bool(f["is_ev"]))
    res.inputs["booking_id"] = booking_id
    return res


def rental_charge_for_booking(conn: sqlite3.Connection, booking_id: str) -> CalcResult:
    f = _facts(conn, booking_id)
    res = rental_charge(f["hourly_rate"], f["daily_rate"], f["booked_minutes"] / 60)
    res.inputs["booking_id"] = booking_id
    if abs(f["rental_paid"] - res.amount) > 0.01:
        res.notes.append(f"Rental actually paid (successful payments) is {rupees(f['rental_paid'])}.")
        if f["pending_payments"]:
            res.notes.append("A payment for this booking is still pending gateway confirmation.")
    return res


def _recorded_penalties(conn: sqlite3.Connection, booking_id: str) -> dict:
    """Penalty payments grouped by kind. Kind is inferred from the payment remarks."""
    rows = conn.execute("""
        SELECT CASE WHEN remarks LIKE '%late%' THEN 'late'
                    WHEN remarks LIKE '%battery%' THEN 'battery'
                    WHEN remarks LIKE 'Damage%' THEN 'damage' ELSE 'other' END AS kind,
               payment_status, ROUND(SUM(amount), 2) AS amount
        FROM payments WHERE booking_id = ? AND payment_type = 'PENALTY' GROUP BY 1, 2""",
                        (booking_id,)).fetchall()
    out: dict = {}
    for kind, status, amount in rows:
        out.setdefault(kind, {})[status] = amount
    return out


def booking_charges_summary(conn: sqlite3.Connection, booking_id: str, now_utc: datetime | None = None) -> dict:
    """Policy amounts next to what was actually recorded, with mismatches flagged."""
    f = _facts(conn, booking_id)
    recorded = _recorded_penalties(conn, booking_id)
    items, flags = {}, []

    items["rental"] = rental_charge_for_booking(conn, booking_id).to_dict()
    if f["booking_status"] in ("CNL_U", "CNL_S", "NSH", "UPC"):
        items["refund"] = refund_for_booking(conn, booking_id, now_utc).to_dict()
    if f["booking_status"] in ("CMP", "ONG"):
        late = late_fee_for_booking(conn, booking_id, now_utc)
        items["late_fee"] = late.to_dict()
        rec_late = recorded.get("late", {})
        if f["booking_status"] == "CMP":
            charged = rec_late.get("SUCCESS", 0) + rec_late.get("PENDING", 0)
            if abs(charged - late.amount) > 0.01:
                flags.append(f"Late fee by policy is {rupees(late.amount)} but {rupees(charged)} is recorded"
                             + (" (none recorded)." if not charged else "."))
            if rec_late.get("PENDING"):
                flags.append(f"Late fee of {rupees(rec_late['PENDING'])} is still pending payment.")
        if f["is_ev"]:
            batt = battery_fee_for_booking(conn, booking_id)
            items["low_battery_fee"] = batt.to_dict()
            charged = sum(recorded.get("battery", {}).values())
            if f["booking_status"] == "CMP" and abs(charged - batt.amount) > 0.01:
                flags.append(f"Battery fee by policy is {rupees(batt.amount)} but {rupees(charged)} is recorded.")
    if f["damage_reports"]:
        items["damage"] = {"claims": f["damage_claims"], "charged": f["damage_charged"],
                           "rule": "Only CHARGED claims are payable; OPEN/DISPUTED are not charged until resolved; "
                                   "WAIVED are never charged (RW-POL-04 §3.1)."}
    return {"booking_id": booking_id, "status": f["status_label"], "policy_amounts": items,
            "recorded": {"rental_paid": f["rental_paid"], "penalties": recorded, "refunded": f["refunded"],
                         "deposit_paid": f["deposit_paid"]},
            "mismatches": flags,
            "notes": ["Deposit returns are handled by the payment gateway and are not recorded in RideWise "
                      "records (RW-POL-01 §5.3)."] if f["deposit_paid"] else []}
