from datetime import date

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


def test_contains_pii():
    assert contains_pii("call 9845012345")
    assert contains_pii("licence KA0120160012345")
    assert not contains_pii(mask_text("call 9845012345, mail a.b@gmail.com"))
