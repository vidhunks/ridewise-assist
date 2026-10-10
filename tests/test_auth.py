"""Sign-in: customers by customer ID + registered phone, agents by username + password."""
import json

import pytest

from app import auth


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_LOG", tmp_path / "auth.jsonl")
    auth._failures.clear()
    yield
    auth._failures.clear()


@pytest.mark.parametrize("cid,phone", [("CUST0001", "9845012345"), ("cust1", "+91 98450 12345"),
                                       ("CUST0001", "098450-12345"), ("CUST0147", "7133006742")])
def test_customer_signs_in_with_registered_phone(cid, phone):
    p = auth.authenticate_customer(cid, phone)
    assert p.role == "customer" and p.customer_id in ("CUST0001", "CUST0147")


@pytest.mark.parametrize("cid,phone", [("CUST0001", "9845012346"),      # wrong phone
                                       ("CUST0001", "7133006742"),      # another customer's phone
                                       ("CUST9999", "9845012345"),      # unknown customer
                                       ("hello", "9845012345"),         # malformed ID
                                       ("CUST0001", ""), ("CUST0001", "12345")])
def test_customer_wrong_details_get_one_generic_message(cid, phone):
    with pytest.raises(auth.AuthError) as e:
        auth.authenticate_customer(cid, phone)
    assert str(e.value) == auth.CUSTOMER_FAIL           # never reveals whether the ID exists


def test_agent_signs_in():
    p = auth.authenticate_agent("john1234", "password")
    assert p.role == "agent" and p.username == "john1234" and p.customer_id is None


@pytest.mark.parametrize("user,pw", [("john1234", "Password"), ("john1234", ""), ("john", "password"),
                                     ("admin", "admin"), ("", "")])
def test_agent_wrong_credentials(user, pw):
    with pytest.raises(auth.AuthError) as e:
        auth.authenticate_agent(user, pw)
    assert str(e.value) == auth.AGENT_FAIL


def test_lockout_after_repeated_failures():
    for _ in range(auth.MAX_FAILURES):
        with pytest.raises(auth.AuthError):
            auth.authenticate_customer("CUST0001", "0000000000")
    with pytest.raises(auth.AuthError) as e:
        auth.authenticate_customer("CUST0001", "9845012345")     # right details, but locked
    assert "Too many failed attempts" in str(e.value)
    auth.authenticate_customer("CUST0002", auth.open_source().execute(
        "SELECT phone FROM customers WHERE customer_id='CUST0002'").fetchone()[0])   # others unaffected


def test_agent_lockout():
    for _ in range(auth.MAX_FAILURES):
        with pytest.raises(auth.AuthError):
            auth.authenticate_agent("john1234", "guess")
    with pytest.raises(auth.AuthError, match="Too many failed attempts"):
        auth.authenticate_agent("john1234", "password")


def test_attempts_are_audited_without_raw_phone():
    with pytest.raises(auth.AuthError):
        auth.authenticate_customer("CUST0001", "9845012399")
    auth.authenticate_customer("CUST0001", "9845012345")
    rows = [json.loads(l) for l in auth.AUTH_LOG.read_text(encoding="utf-8").splitlines()]
    assert [r["success"] for r in rows] == [False, True]
    assert "9845012399" not in auth.AUTH_LOG.read_text(encoding="utf-8")


def test_password_is_not_stored_in_plain_text():
    text = auth.USERS_FILE.read_text(encoding="utf-8")
    assert "john1234" in text and "password\n" not in text and ": password" not in text
