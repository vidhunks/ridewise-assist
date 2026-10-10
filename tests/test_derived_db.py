"""Phase 1 checks: the cleaned database is correct, masked and read-only,
and building it leaves the provided database untouched."""
import sqlite3

import pytest

from app.data.db import SOURCE_DB, QueryTimeout, open_derived, query
from app.safety.masking import contains_pii
from scripts.build_derived import build, sha256


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    before = sha256(SOURCE_DB)
    path = build(tmp_path_factory.mktemp("derived") / "derived.db")
    assert sha256(SOURCE_DB) == before, "building must not modify ridewise.db"
    assert not SOURCE_DB.with_name("ridewise.db-wal").exists()
    conn = open_derived(path)
    yield conn
    conn.close()


def one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def test_row_counts_match_source(db):
    for table, n in {"customers": 200, "vehicles": 60, "bookings": 1800, "payments": 3289,
                     "damage_reports": 80, "booking_facts": 1800}.items():
        assert one(db, f"SELECT COUNT(*) FROM {table}")[0] == n


def test_city_spellings_normalised(db):
    cities = {r[0] for r in db.execute("SELECT DISTINCT city FROM customers")}
    assert "Bengaluru" in cities
    assert not cities & {"BLR", "Bangalore", "bangalore ", "bangalore"}


def test_identity_fields_are_masked_not_dropped(db):
    r = one(db, "SELECT dob_masked, birth_year, age_years, is_adult, license_masked, phone_masked, "
                "email_masked FROM customers WHERE customer_id='CUST0001'")
    assert r["dob_masked"] == "1998-XX-XX" and r["birth_year"] == 1998 and r["is_adult"] == 1
    assert r["license_masked"] == "XXXXXXXXXXX2345"
    assert r["phone_masked"] == "98XXXXXX45" and r["email_masked"] == "a***@gmail.com"


def test_no_raw_pii_anywhere(db):
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for t in tables:
        for row in db.execute(f"SELECT * FROM {t}"):
            for value in row:
                assert not (isinstance(value, str) and contains_pii(value)), f"{t}: {value}"


def test_failed_payment_not_counted(db):
    # B-0002: ₹270 FAILED then ₹270 SUCCESS -> paid once.
    r = one(db, "SELECT rental_paid, failed_payment_attempts FROM booking_facts WHERE booking_id='B-0002'")
    assert r["rental_paid"] == 270 and r["failed_payment_attempts"] == 1


def test_refund_policy_version_uses_ist_cutoff(db):
    # B-1163 booked 2026-06-30 17:45 UTC = 23:15 IST -> v1; B-1165 at 20:15 UTC = 01:45 IST 1 Jul -> v2.
    v = dict(db.execute("SELECT booking_id, refund_policy_version FROM booking_facts "
                        "WHERE booking_id IN ('B-1163','B-1165')").fetchall())
    assert v == {"B-1163": "v1", "B-1165": "v2"}


def test_minutes_are_exact(db):
    assert one(db, "SELECT late_minutes FROM booking_facts WHERE booking_id='B-0249'")[0] == 30
    assert one(db, "SELECT booked_minutes FROM booking_facts WHERE booking_id='B-0001'")[0] == 240


def test_refunds_are_positive_in_facts(db):
    assert one(db, "SELECT MIN(refunded) FROM booking_facts")[0] >= 0


@pytest.mark.parametrize("sql", [
    "DELETE FROM bookings",
    "UPDATE payments SET amount = 0",
    "INSERT INTO customers (customer_id) VALUES ('X')",
    "DROP TABLE vehicles",
    "CREATE TABLE t (x)",
    "ATTACH DATABASE 'other.db' AS o",
    "PRAGMA writable_schema = ON",
])
def test_writes_are_blocked(db, sql):
    with pytest.raises(sqlite3.DatabaseError):
        db.execute(sql)


def test_query_row_cap_and_timeout(db):
    cols, rows, truncated = query(db, "SELECT booking_id FROM bookings", max_rows=10)
    assert cols == ["booking_id"] and len(rows) == 10 and truncated
    slow = ("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n) "
            "SELECT COUNT(*) FROM n")
    with pytest.raises(QueryTimeout):
        query(db, slow, timeout_seconds=0.3)
