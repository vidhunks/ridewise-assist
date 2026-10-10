"""Phase 2: calculators reproduce the policy documents' own worked examples,
handle the boundary cases, and agree with the money actually recorded in the database."""
import copy
from datetime import datetime, timedelta

import pytest

from app.data.db import open_derived
from app.rules import load_rules
from app.tools import calculators as C

RULES = load_rules()
V1, V2 = (next(v for v in RULES["refund_policy_versions"] if v["id"] == i) for i in ("v1", "v2"))


@pytest.fixture(scope="module")
def db():
    from scripts.build_derived import ensure_built
    ensure_built()
    conn = open_derived()
    yield conn
    conn.close()


# ---- RW-POL-01 §4.2 worked examples (hourly ₹80, daily ₹700)
@pytest.mark.parametrize("hours,expected", [(4, 320), (10, 700), (24, 700), (27, 940)])
def test_rental_charge_pdf_examples(hours, expected):
    assert C.rental_charge(80, 700, hours).amount == expected


def test_rental_minimum_two_hours():
    assert C.rental_charge(80, 700, 1).amount == 160


# ---- RW-POL-04 §1.4 worked examples (hourly ₹100, no grace period)
@pytest.mark.parametrize("minutes,hours,fee", [(5, 1, 150), (59, 1, 150), (61, 2, 300), (180, 3, 450), (195, 4, 600)])
def test_late_fee_pdf_examples(minutes, hours, fee):
    r = C.late_fee(minutes, 100, "STANDARD")
    assert r.amount == fee and f"{hours} hour(s)" in r.steps[0]


def test_late_fee_per_model_table():  # RW-POL-04 §1.5: TVS Apache RTR 160 at ₹85/h -> ₹127.50 per late hour
    assert C.late_fee(60, 85, "SILVER").amount == 127.5


def test_gold_grace_is_inclusive_then_full_lateness():
    assert C.late_fee(30, 100, "GOLD").amount == 0
    assert C.late_fee(31, 100, "GOLD").amount == 150      # full lateness from minute one
    assert C.late_fee(30, 100, "SILVER").amount == 150    # no grace for SILVER


def test_late_fee_gst():
    r = C.late_fee(61, 100, "STANDARD")
    assert (r.gst, r.total) == (54, 354)


# ---- RW-POL-04 §2 / RW-POL-05 Figure 2
@pytest.mark.parametrize("pct,fee", [(3, 150), (9, 150), (10, 0), (19, 0), (80, 0)])
def test_low_battery_fee(pct, fee):
    assert C.low_battery_fee(pct, is_ev=True).amount == fee


def test_low_battery_fee_not_for_petrol():
    assert C.low_battery_fee(5, is_ev=False).amount == 0


# ---- Refunds
def test_refund_v1_pdf_example():  # RW-POL-02 §5: ₹500 paid, cancelled 12 h before -> ₹250 + ₹45 GST
    r = C.refund(V1, "CNL_U", 12 * 60, 500, 90)
    assert (r.amount, r.gst) == (250, 45)


@pytest.mark.parametrize("minutes,pct", [(48 * 60, 100), (48 * 60 - 1, 75), (12 * 60, 75), (12 * 60 - 1, 25),
                                         (2 * 60, 25), (2 * 60 - 1, 0), (0, 0)])
def test_refund_v2_slab_boundaries(minutes, pct):
    assert C.refund(V2, "CNL_U", minutes, 400, 72).inputs["refund_pct"] == pct


@pytest.mark.parametrize("minutes,pct", [(24 * 60, 100), (24 * 60 - 1, 50), (6 * 60, 50), (6 * 60 - 1, 0)])
def test_refund_v1_slab_boundaries(minutes, pct):
    assert C.refund(V1, "CNL_U", minutes, 400, 72).inputs["refund_pct"] == pct


def test_refund_special_cases():
    assert C.refund(V1, "CNL_S", 10, 500, 90).amount == 500     # RideWise cancelled: always 100%
    assert C.refund(V2, "NSH", None, 500, 90).amount == 0       # no-show: nothing back


@pytest.mark.parametrize("booked_utc,version", [("2026-06-30 18:29:00", "v1"), ("2026-06-30 18:30:00", "v2"),
                                                ("2025-03-01 10:00:00", "v1")])
def test_version_cutoff_is_midnight_ist(booked_utc, version):
    assert C.refund_policy_version_for(booked_utc)["id"] == version


# ---- Real bookings
def test_b1163_v1_and_b1165_v2(db):
    assert C.refund_for_booking(db, "B-1163").amount == 270   # v1: 20 h notice -> 50% of ₹540
    assert C.refund_for_booking(db, "B-1165").amount == 240   # v2: 20 h notice -> 75% of ₹320


@pytest.mark.parametrize("booking,tier,minutes,fee", [
    ("B-0788", "GOLD", 30, 0),       # inside the inclusive 30-min grace
    ("B-0166", "GOLD", 31, 135),     # one minute over: full lateness charged
    ("B-0249", "SILVER", 30, 90),    # no grace for SILVER
])
def test_grace_period_edge_on_real_bookings(db, booking, tier, minutes, fee):
    r = C.late_fee_for_booking(db, booking)
    assert (r.inputs["customer_tier"], r.inputs["late_minutes"], r.amount) == (tier, minutes, fee)


def test_recent_cancellation_explains_missing_refund(db):
    r = C.refund_for_booking(db, "B-1709")
    assert r.amount > 0 and any("No refund is recorded yet" in n for n in r.notes)


def test_upcoming_booking_what_if_cancelled_now(db):
    f = db.execute("SELECT booking_id, scheduled_start_utc FROM booking_facts WHERE booking_status='UPC' "
                   "AND rental_paid > 0 LIMIT 1").fetchone()
    start = datetime.strptime(f["scheduled_start_utc"], C.TS_FORMAT)
    r = C.refund_for_booking(db, f["booking_id"], now_utc=start.replace(year=start.year - 1))
    assert r.calculation == "refund_if_cancelled_now" and r.inputs["refund_pct"] == 100


def test_ongoing_booking_late_fee_so_far(db):
    f = db.execute("SELECT booking_id, scheduled_end_utc FROM booking_facts WHERE booking_status='ONG' LIMIT 1").fetchone()
    end = datetime.strptime(f["scheduled_end_utc"], C.TS_FORMAT)
    r = C.late_fee_for_booking(db, f["booking_id"], now_utc=end + timedelta(minutes=61))
    assert r.calculation == "late_fee_so_far" and r.inputs["late_minutes"] == 61


def test_policy_agrees_with_recorded_money_across_whole_dataset(db):
    """Regression guard: the calculators reproduce what RideWise actually charged/refunded."""
    late_mismatch, refund_mismatch = [], []
    for row in db.execute("SELECT booking_id, booking_status, refunded, cancelled_at_utc FROM booking_facts").fetchall():
        s = C.booking_charges_summary(db, row["booking_id"])
        late_mismatch += [m for m in s["mismatches"] if "pending" not in m]
        if row["booking_status"] in ("CNL_U", "CNL_S", "NSH") and row["refunded"] > 0:
            if abs(s["policy_amounts"]["refund"]["amount"] - row["refunded"]) > 0.01:
                refund_mismatch.append(row["booking_id"])
    assert late_mismatch == [] and refund_mismatch == []


def test_rules_file_drives_results():
    """Adaptability: change the rule, the answer changes - no code edits."""
    rules = copy.deepcopy(RULES)
    rules["late_fee"]["grace_minutes_by_tier"]["SILVER"] = 15
    assert C.late_fee(10, 100, "SILVER", rules).amount == 0
    assert C.late_fee(10, 100, "SILVER").amount == 150


def test_every_result_cites_sources():
    for r in (C.rental_charge(80, 700, 27), C.late_fee(61, 100, "GOLD"), C.low_battery_fee(5, True),
              C.refund(V2, "CNL_U", 600, 400, 72)):
        assert r.sources and r.steps
