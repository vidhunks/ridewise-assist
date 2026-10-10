"""Data tools with access control enforced in code (never by the prompt).

* Customer sessions: every query is bound to the session's customer_id. A booking that belongs to
  someone else is reported exactly like one that does not exist, so its existence is not leaked.
* Agent sessions: may look up any customer/booking and run validated read-only SQL over the
  masked derived database.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from app.data.db import open_source, query
from app.tools.sql_guard import MAX_ROWS, validate_select


class AccessDenied(PermissionError):
    pass


class NotFound(LookupError):
    pass


@dataclass
class Session:
    role: str                              # "customer" | "agent"
    customer_id: str | None = None
    last_booking_id: str | None = None
    history: list[dict] = field(default_factory=list)

    @property
    def is_customer(self) -> bool:
        return self.role == "customer"


def normalise_booking_id(raw: str) -> str:
    """'b12', 'B0012', 'booking 12', 'B-0012' -> 'B-0012'."""
    m = re.search(r"(?i)\bb(?:ooking)?[\s#:-]*0*(\d{1,4})\b", str(raw)) or re.fullmatch(r"\s*0*(\d{1,4})\s*", str(raw))
    if not m:
        raise NotFound(f"'{raw}' is not a valid booking ID (format B-0001).")
    return f"B-{int(m.group(1)):04d}"


def normalise_customer_id(raw: str) -> str:
    m = re.search(r"(?i)\bcust\s*-?0*(\d{1,4})\b", str(raw))
    if not m:
        raise NotFound(f"'{raw}' is not a valid customer ID (format CUST0001).")
    return f"CUST{int(m.group(1)):04d}"


def _rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _booking_owner(conn: sqlite3.Connection, booking_id: str) -> str | None:
    r = conn.execute("SELECT customer_id FROM bookings WHERE booking_id = ?", (booking_id,)).fetchone()
    return r[0] if r else None


def authorize_booking(conn: sqlite3.Connection, session: Session, booking_ref: str) -> str:
    """Return the canonical booking ID if this session may see it; otherwise raise NotFound."""
    booking_id = normalise_booking_id(booking_ref)
    owner = _booking_owner(conn, booking_id)
    if owner is None or (session.is_customer and owner != session.customer_id):
        # Same message whether it doesn't exist or isn't theirs.
        raise NotFound(f"Booking {booking_id} was not found" + (" on your account." if session.is_customer else "."))
    session.last_booking_id = booking_id
    return booking_id


# ------------------------------------------------------------------ shared (scoped for customers)

def get_booking(conn, session: Session, booking_id: str) -> dict:
    bid = authorize_booking(conn, session, booking_id)
    row = _rows(conn, "SELECT * FROM booking_facts WHERE booking_id = ?", (bid,))[0]
    return {"booking": row, "source": f"booking_facts:{bid}"}


def get_payments(conn, session: Session, booking_id: str) -> dict:
    bid = authorize_booking(conn, session, booking_id)
    rows = _rows(conn, """SELECT payment_id, payment_type, amount, gst_amount, payment_method, payment_status,
                                 paid_at AS paid_at_utc, datetime(paid_at, '+330 minutes') AS paid_at_ist, remarks
                          FROM payments WHERE booking_id = ? ORDER BY paid_at""", (bid,))
    return {"booking_id": bid, "payments": rows,
            "notes": ["Only SUCCESS payments count as paid; FAILED attempts are never charged (RW-POL-01 §4.4).",
                      "Refunds are stored as negative amounts.",
                      "Security deposit returns are not recorded in RideWise records (RW-POL-01 §5.3)."],
            "source": f"payments:{bid}"}


def get_damage_reports(conn, session: Session, booking_id: str | None = None) -> dict:
    if booking_id:
        bid = authorize_booking(conn, session, booking_id)
        rows = _rows(conn, "SELECT * FROM damage_reports WHERE booking_id = ?", (bid,))
        src = f"damage_reports:{bid}"
    elif session.is_customer:
        rows = _rows(conn, """SELECT d.* FROM damage_reports d JOIN bookings b USING (booking_id)
                              WHERE b.customer_id = ? ORDER BY d.reported_at DESC""", (session.customer_id,))
        src = f"damage_reports:{session.customer_id}"
    else:
        raise NotFound("Give a booking ID, or use run_sql for damage reports across bookings.")
    return {"damage_reports": rows,
            "rule": "CHARGED = payable; OPEN/DISPUTED = not charged until resolved; WAIVED = not charged (RW-POL-04 §3.1).",
            "source": src}


def list_vehicles(conn, session: Session, hub: str | None = None, vehicle_type: str | None = None,
                  model: str | None = None) -> dict:
    """Fleet and prices. Customers see active vehicles only and no registration numbers."""
    sql = ["SELECT model, vehicle_type, hub_name, hourly_rate, daily_rate, battery_capacity_kwh, COUNT(*) AS vehicles"
           + ("" if session.is_customer else ", GROUP_CONCAT(vehicle_id) AS vehicle_ids, status")
           + " FROM vehicles WHERE 1=1"]
    params: list = []
    if session.is_customer:
        sql.append("AND status = 'ACTIVE'")
    if hub:
        sql.append("AND lower(hub_name) LIKE ?")
        params.append(f"%{hub.lower()}%")
    if vehicle_type:
        sql.append("AND vehicle_type = ?")
        params.append(vehicle_type.upper().replace(" ", "_"))
    if model:
        sql.append("AND lower(model) LIKE ?")
        params.append(f"%{model.lower()}%")
    sql.append("GROUP BY model, hub_name" + ("" if session.is_customer else ", status") + " ORDER BY hub_name, model")
    return {"vehicles": _rows(conn, " ".join(sql), params),
            "note": "Lists the fleet assigned to each hub; real-time availability for a time slot is not in the data.",
            "source": "vehicles"}


# ------------------------------------------------------------------ customer only

def get_my_profile(conn, session: Session) -> dict:
    if not session.is_customer:
        raise AccessDenied("get_my_profile is for customer sessions.")
    prof = _rows(conn, "SELECT * FROM customers WHERE customer_id = ?", (session.customer_id,))
    if not prof:
        raise NotFound("Customer profile not found.")
    counts = _rows(conn, """SELECT status_label, COUNT(*) AS n FROM booking_facts WHERE customer_id = ?
                            GROUP BY status_label""", (session.customer_id,))
    return {"profile": prof[0], "bookings_by_status": counts,
            "note": "Phone, email, licence number and date of birth are shown masked for security.",
            "source": f"customers:{session.customer_id}"}


def list_my_bookings(conn, session: Session, status: str | None = None, limit: int = 10) -> dict:
    if not session.is_customer:
        raise AccessDenied("list_my_bookings is for customer sessions; agents use list_customer_bookings.")
    return list_customer_bookings(conn, session, session.customer_id, status, limit, _scoped=True)


# ------------------------------------------------------------------ agent only

def _require_agent(session: Session) -> None:
    if session.is_customer:
        raise AccessDenied("This information is only available to RideWise support staff.")


def list_customer_bookings(conn, session: Session, customer_id: str, status: str | None = None,
                           limit: int = 10, _scoped: bool = False) -> dict:
    if not _scoped:
        _require_agent(session)
    cid = normalise_customer_id(customer_id)
    sql = """SELECT booking_id, status_label, booking_status, model, vehicle_type, hub_name,
                    scheduled_start_ist, scheduled_end_ist, actual_return_at_ist, cancelled_at_ist,
                    late_minutes, rental_paid, penalties_paid, refunded
             FROM booking_facts WHERE customer_id = ?"""
    params: list = [cid]
    if status:
        codes = {"upcoming": "UPC", "ongoing": "ONG", "completed": "CMP", "cancelled": "CNL_%",
                 "no-show": "NSH", "noshow": "NSH"}
        sql += " AND booking_status LIKE ?"
        params.append(codes.get(status.lower(), status.upper()))
    sql += " ORDER BY scheduled_start_utc DESC LIMIT ?"
    params.append(max(1, min(int(limit), 50)))
    rows = _rows(conn, sql, params)
    total = conn.execute("SELECT COUNT(*) FROM bookings WHERE customer_id = ?", (cid,)).fetchone()[0]
    return {"customer_id": cid, "total_bookings": total, "bookings": rows, "source": f"booking_facts:{cid}"}


def find_customer(conn, session: Session, query_text: str) -> dict:
    """Agent lookup by customer ID, name (partial), or exact phone / email. Results are masked."""
    _require_agent(session)
    q = query_text.strip()
    ids: list[str] = []
    try:
        ids = [normalise_customer_id(q)]
    except NotFound:
        digits = re.sub(r"\D", "", q)
        if len(digits) >= 10 or "@" in q:
            src = open_source()     # raw contact details live only in the source DB
            try:
                ids = [r[0] for r in src.execute(
                    "SELECT customer_id FROM customers WHERE phone = ? OR lower(email) = lower(?)",
                    (digits[-10:], q))]
            finally:
                src.close()
        else:
            ids = [r[0] for r in conn.execute(
                "SELECT customer_id FROM customers WHERE lower(full_name) LIKE ? ORDER BY customer_id",
                (f"%{q.lower()}%",))]
    if not ids:
        return {"matches": [], "note": f"No customer matches '{q}'."}
    marks = ",".join("?" * len(ids[:20]))
    rows = _rows(conn, f"""SELECT c.customer_id, c.full_name, c.phone_masked, c.email_masked, c.city,
                                 c.customer_tier, c.kyc_status,
                                 (SELECT COUNT(*) FROM bookings b WHERE b.customer_id = c.customer_id) AS bookings
                          FROM customers c WHERE c.customer_id IN ({marks})""", ids[:20])
    out = {"matches": rows, "source": "customers"}
    if len(rows) > 1:
        out["note"] = f"{len(rows)} customers match; confirm which one (by customer ID) before acting."
    return out


def get_customer_profile(conn, session: Session, customer_id: str) -> dict:
    _require_agent(session)
    cid = normalise_customer_id(customer_id)
    prof = _rows(conn, "SELECT * FROM customers WHERE customer_id = ?", (cid,))
    if not prof:
        raise NotFound(f"Customer {cid} not found.")
    return {"profile": prof[0], "source": f"customers:{cid}"}


def run_sql(conn, session: Session, sql: str) -> dict:
    _require_agent(session)
    safe_sql = validate_select(sql)
    columns, rows, truncated = query(conn, safe_sql, max_rows=MAX_ROWS)
    return {"sql": safe_sql, "columns": columns, "rows": rows, "row_count": len(rows),
            "truncated": truncated, "source": "sql"}
