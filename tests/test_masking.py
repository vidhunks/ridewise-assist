from datetime import date

import pytest

from app.safety.masking import (age_on, contains_pii, mask_dob, mask_email, mask_license,
                                mask_phone, mask_text)


def test_mask_email():
    assert mask_email("aarav.sharma@gmail.com") == "a***@gmail.com"


def test_mask_phone():
    assert mask_phone("9845012345") == "98XXXXXX45"


def test_mask_license_keeps_last_four():
    assert mask_license("KA0120160012345") == "XXXXXXXXXXX2345"


def test_mask_dob_keeps_year():
    assert mask_dob("1998-04-12") == "1998-XX-XX"


def test_age_handles_birthday_not_yet_reached():
    assert age_on("2004-10-07", date(2026, 10, 10)) == 22
    assert age_on("2004-10-11", date(2026, 10, 10)) == 21


def test_mask_text_real_remarks():
    assert mask_text("Payment link resent to shruti.kulkarni@yahoo.com and 8842278939") == \
        "Payment link resent to s***@yahoo.com and 88XXXXXX39"
    assert mask_text("Customer Chetan Singh (8366701050) asked for invoice copy") == \
        "Customer Chetan Singh (83XXXXXX50) asked for invoice copy"


def test_mask_text_leaves_ids_alone():
    for s in ["Damage charge DMG0012", "Late return fee (2 hr x 1.5)", "UPI2608145301"]:
        assert mask_text(s) == s


@pytest.mark.parametrize("raw", ["98450 12345", "9845-01-2345", "9845‑01‑2345", "+91 98450-12345",
                                 "+919845012345", "98.45.01.23.45"])
def test_mask_phone_written_with_separators(raw):
    assert mask_text(f"call {raw} now") == "call 98XXXXXX45 now"


@pytest.mark.parametrize("safe", ["UPI2608145301", "B-0001 on 2026-07-02 14:00", "DMG0012", "₹1,700",
                                  "+91 80 4000 1234", "support@ridewise.example"])
def test_ids_dates_amounts_and_public_contacts_untouched(safe):
    assert mask_text(safe) == safe


def test_output_typography_normalised():
    from app.safety.guards import normalise_typography
    assert normalise_typography("B‑1581 has 1 64 rows") == "B-1581 has 164 rows"


def test_contains_pii():
    assert contains_pii("call 9845012345")
    assert contains_pii("licence KA0120160012345")
    assert not contains_pii(mask_text("call 9845012345, mail a.b@gmail.com"))
