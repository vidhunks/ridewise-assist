"""Build data/derived.db: a cleaned, masked copy of the provided database.

The provided data/ridewise.db is opened immutable/read-only and is never modified.
Run from the project root:  python -m scripts.build_derived
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app.config import ROOT
from app.data.db import DERIVED_DB, SOURCE_DB, open_source
from app.safety.masking import age_on, mask_dob, mask_email, mask_license, mask_phone, mask_text

RULES_FILE = ROOT / "config" / "policy_rules.yaml"

# Spellings found in customers.city -> one canonical name (compared case-insensitively, trimmed).
CITY_ALIASES = {"blr": "Bengaluru", "bangalore": "Bengaluru", "bengaluru": "Bengaluru",
                "bengalooru": "Bengaluru", "mysore": "Mysuru", "mangalore": "Mangaluru"}

STATUS_LABELS = {"UPC": "Upcoming", "ONG": "Ongoing", "CMP": "Completed",
                 "CNL_U": "Cancelled by customer", "CNL_S": "Cancelled by RideWise", "NSH": "No-show"}

SCHEMA = """
CREATE TABLE customers (
    customer_id TEXT PRIMARY KEY, full_name TEXT, phone_masked TEXT, email_masked TEXT,
    city TEXT, city_raw TEXT, dob_masked TEXT, birth_year INTEGER, age_years INTEGER, is_adult INTEGER,
    license_masked TEXT, kyc_status TEXT, customer_tier TEXT, created_at TEXT);
CREATE TABLE vehicles (
    vehicle_id TEXT PRIMARY KEY, registration_no TEXT, vehicle_type TEXT, is_ev INTEGER, model TEXT,
    hub_name TEXT, hourly_rate REAL, daily_rate REAL, battery_capacity_kwh REAL, status TEXT,
    commissioned_on TEXT);
CREATE TABLE bookings (
    booking_id TEXT PRIMARY KEY, customer_id TEXT, vehicle_id TEXT, booked_at TEXT,
    scheduled_start TEXT, scheduled_end TEXT, actual_return_at TEXT, cancelled_at TEXT,
    booking_status TEXT, return_battery_pct INTEGER);
CREATE TABLE payments (
    payment_id TEXT PRIMARY KEY, booking_id TEXT, payment_type TEXT, amount REAL, gst_amount REAL,
    payment_method TEXT, payment_status TEXT, is_success INTEGER, transaction_ref TEXT, paid_at TEXT,
    remarks TEXT);
CREATE TABLE damage_reports (
    report_id TEXT PRIMARY KEY, booking_id TEXT, vehicle_id TEXT, reported_by TEXT,
    damage_category TEXT, severity TEXT, description TEXT, assessed_cost REAL, claim_status TEXT,
    reported_at TEXT);
CREATE TABLE build_info (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX ix_bookings_customer ON bookings(customer_id);
CREATE INDEX ix_payments_booking ON payments(booking_id);
CREATE INDEX ix_damage_booking ON damage_reports(booking_id);
"""


def _minutes_between(later: str, earlier: str) -> str:
    # Timestamps have whole minutes (seconds are always :00), so this is exact.
    return f"CAST(ROUND((julianday({later}) - julianday({earlier})) * 1440) AS INTEGER)"


def _policy_version_case(rules: dict) -> str:
    """SQL CASE choosing the refund policy version from booked_at (converted to IST)."""
    versions = sorted(rules["refund_policy_versions"], key=lambda v: v["effective_from_ist"], reverse=True)
    whens = []
    for v in versions:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", v["effective_from_ist"]) \
                or not re.fullmatch(r"[A-Za-z0-9_.-]+", v["id"]):
            raise ValueError(f"Bad refund_policy_versions entry in {RULES_FILE}: {v}")
        whens.append(f"WHEN booked_at_ist >= '{v['effective_from_ist']}' THEN '{v['id']}'")
    return f"CASE {' '.join(whens)} ELSE '{versions[-1]['id']}' END"


def booking_facts_sql(rules: dict) -> str:
    off = int(rules["timezone"]["policy_utc_offset_minutes"])
    ist = lambda col: f"datetime(b.{col}, '+{off} minutes')"  # noqa: E731
    labels = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in STATUS_LABELS.items())
    return f"""
CREATE VIEW booking_facts AS
WITH pay AS (
    SELECT booking_id,
        ROUND(SUM(CASE WHEN payment_type='BOOKING' AND is_success THEN amount ELSE 0 END), 2)     AS rental_paid,
        ROUND(SUM(CASE WHEN payment_type='BOOKING' AND is_success THEN gst_amount ELSE 0 END), 2) AS rental_gst_paid,
        ROUND(SUM(CASE WHEN payment_type='PENALTY' AND is_success THEN amount ELSE 0 END), 2)     AS penalties_paid,
        ROUND(SUM(CASE WHEN payment_type='PENALTY' AND payment_status='PENDING' THEN amount ELSE 0 END), 2) AS penalties_pending,
        ROUND(-SUM(CASE WHEN payment_type='REFUND' AND is_success THEN amount ELSE 0 END), 2)    AS refunded,
        ROUND(-SUM(CASE WHEN payment_type='REFUND' AND is_success THEN gst_amount ELSE 0 END), 2) AS refunded_gst,
        ROUND(SUM(CASE WHEN payment_type='DEPOSIT' AND is_success THEN amount ELSE 0 END), 2)     AS deposit_paid,
        SUM(payment_status='FAILED')  AS failed_payment_attempts,
        SUM(payment_status='PENDING') AS pending_payments
    FROM payments GROUP BY booking_id),
dmg AS (
    SELECT booking_id, COUNT(*) AS damage_reports,
        ROUND(SUM(CASE WHEN claim_status='CHARGED' THEN assessed_cost ELSE 0 END), 2) AS damage_charged,
        GROUP_CONCAT(report_id || ':' || claim_status, ', ') AS damage_claims
    FROM damage_reports GROUP BY booking_id),
base AS (
    SELECT b.*, {ist('booked_at')} AS booked_at_ist FROM bookings b)
SELECT
    b.booking_id, b.customer_id, c.customer_tier, c.kyc_status,
    b.vehicle_id, v.model, v.vehicle_type, v.is_ev, v.hub_name, v.hourly_rate, v.daily_rate,
    b.booking_status, CASE b.booking_status {labels} END AS status_label,
    b.booked_at        AS booked_at_utc,        b.booked_at_ist,
    b.scheduled_start  AS scheduled_start_utc,  {ist('scheduled_start')}  AS scheduled_start_ist,
    b.scheduled_end    AS scheduled_end_utc,    {ist('scheduled_end')}    AS scheduled_end_ist,
    b.actual_return_at AS actual_return_at_utc, {ist('actual_return_at')} AS actual_return_at_ist,
    b.cancelled_at     AS cancelled_at_utc,     {ist('cancelled_at')}     AS cancelled_at_ist,
    {_minutes_between('b.scheduled_end', 'b.scheduled_start')} AS booked_minutes,
    ROUND({_minutes_between('b.scheduled_end', 'b.scheduled_start')} / 60.0, 2) AS booked_hours,
    CASE WHEN b.actual_return_at IS NULL THEN NULL
         ELSE MAX(0, {_minutes_between('b.actual_return_at', 'b.scheduled_end')}) END AS late_minutes,
    CASE WHEN b.cancelled_at IS NULL THEN NULL
         ELSE {_minutes_between('b.scheduled_start', 'b.cancelled_at')} END AS cancel_notice_minutes,
    {_policy_version_case(rules)} AS refund_policy_version,
    b.return_battery_pct,
    COALESCE(p.rental_paid, 0) AS rental_paid, COALESCE(p.rental_gst_paid, 0) AS rental_gst_paid,
    COALESCE(p.penalties_paid, 0) AS penalties_paid, COALESCE(p.penalties_pending, 0) AS penalties_pending,
    COALESCE(p.refunded, 0) AS refunded, COALESCE(p.refunded_gst, 0) AS refunded_gst,
    COALESCE(p.deposit_paid, 0) AS deposit_paid,
    COALESCE(p.failed_payment_attempts, 0) AS failed_payment_attempts,
    COALESCE(p.pending_payments, 0) AS pending_payments,
    COALESCE(d.damage_reports, 0) AS damage_reports, COALESCE(d.damage_charged, 0) AS damage_charged,
    d.damage_claims
FROM base b
JOIN customers c ON c.customer_id = b.customer_id
JOIN vehicles  v ON v.vehicle_id  = b.vehicle_id
LEFT JOIN pay  p ON p.booking_id  = b.booking_id
LEFT JOIN dmg  d ON d.booking_id  = b.booking_id;
"""


def normalise_city(raw: str | None) -> str | None:
    if raw is None:
        return None
    key = raw.strip().casefold()
    return CITY_ALIASES.get(key, raw.strip().title())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(dest: Path = DERIVED_DB) -> Path:
    rules = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))
    today = datetime.now(timezone.utc).date()
    src = open_source()
    tmp = dest.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    out = sqlite3.connect(tmp)
    try:
        out.executescript(SCHEMA)

        out.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            (r["customer_id"], r["full_name"].strip(), mask_phone(r["phone"]), mask_email(r["email"]),
             normalise_city(r["city"]), r["city"], mask_dob(r["date_of_birth"]),
             int(r["date_of_birth"][:4]) if r["date_of_birth"] else None,
             age_on(r["date_of_birth"], today),
             None if age_on(r["date_of_birth"], today) is None else int(age_on(r["date_of_birth"], today) >= 18),
             mask_license(r["driving_license_no"]), r["kyc_status"], r["customer_tier"], r["created_at"])
            for r in src.execute("SELECT * FROM customers")])

        out.executemany("INSERT INTO vehicles VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
            (r["vehicle_id"], r["registration_no"], r["vehicle_type"], int(r["vehicle_type"].startswith("EV")),
             r["model"], r["hub_name"], r["hourly_rate"], r["daily_rate"], r["battery_capacity_kwh"],
             r["status"], r["commissioned_on"])
            for r in src.execute("SELECT * FROM vehicles")])

        out.executemany("INSERT INTO bookings VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [tuple(r) for r in src.execute(
                            "SELECT booking_id, customer_id, vehicle_id, booked_at, scheduled_start, scheduled_end,"
                            " actual_return_at, cancelled_at, booking_status, return_battery_pct FROM bookings")])

        out.executemany("INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
            (r["payment_id"], r["booking_id"], r["payment_type"], r["amount"], r["gst_amount"],
             r["payment_method"], r["payment_status"], int(r["payment_status"] == "SUCCESS"),
             r["transaction_ref"], r["paid_at"], mask_text(r["remarks"]))
            for r in src.execute("SELECT * FROM payments")])

        out.executemany("INSERT INTO damage_reports VALUES (?,?,?,?,?,?,?,?,?,?)", [
            (r["report_id"], r["booking_id"], r["vehicle_id"], r["reported_by"], r["damage_category"],
             r["severity"], mask_text(r["description"]), r["assessed_cost"], r["claim_status"], r["reported_at"])
            for r in src.execute("SELECT * FROM damage_reports")])

        out.executescript(booking_facts_sql(rules))
        out.executemany("INSERT INTO build_info VALUES (?,?)", [
            ("built_at_utc", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
            ("source_sha256", sha256(SOURCE_DB)),
            ("rules_sha256", sha256(RULES_FILE)),
        ])
        out.commit()
    finally:
        out.close()
        src.close()
    os.replace(tmp, dest)
    return dest


def is_stale(dest: Path = DERIVED_DB) -> bool:
    """True if derived.db is missing or was built from a different source DB or rules file."""
    if not dest.exists():
        return True
    try:
        with sqlite3.connect(dest.resolve().as_uri() + "?mode=ro", uri=True) as c:
            info = dict(c.execute("SELECT key, value FROM build_info"))
    except sqlite3.Error:
        return True
    return info.get("source_sha256") != sha256(SOURCE_DB) or info.get("rules_sha256") != sha256(RULES_FILE)


def ensure_built() -> Path:
    return build() if is_stale() else DERIVED_DB


if __name__ == "__main__":
    path = build()
    with sqlite3.connect(path) as c:
        for t in ("customers", "vehicles", "bookings", "payments", "damage_reports", "booking_facts"):
            print(f"{t:15} {c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:>5} rows")
    print(f"Built {path.relative_to(ROOT)}")
