"""Phase 5: the Streamlit app, driven headlessly with Streamlit's AppTest.

The LLM is replaced by a stub here so these tests are fast and free; the live agent is covered by
tests/test_agent_live.py.
"""
import pytest
from streamlit.testing.v1 import AppTest

from app.agent import loop
from app.config import ROOT
from app.agent.schema import AssistantResponse, Calculation, Source

TIMEOUT = 180   # first run builds the index / loads models


@pytest.fixture(autouse=True)
def stub_llm(monkeypatch):
    def fake_run(self, question):
        if "cancel" in question.lower():
            return AssistantResponse(answer="Queued ticket ESC-TEST.", escalate=True,
                                     escalation_reason="ESC-TEST (cancellation): cancel B-1759",
                                     sources=[Source(type="database", ref="escalation:ESC-TEST")])
        return AssistantResponse(
            answer=f"Echo: {question}",
            sources=[Source(type="database", ref="booking_facts:B-1163"),
                     Source(type="policy", ref="RW-POL-02 v1, 1. Cancellation by the customer (p. 1)",
                            detail="1.1 The customer can cancel..."),
                     Source(type="database", ref="SQL query", detail="SELECT 1")],
            sql=["SELECT 1"],
            calculations=[Calculation(name="refund", total=318.6, steps=["50% of ₹540 = ₹270"])])
    monkeypatch.setattr(loop.Assistant, "_run", fake_run)


def boot():
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=TIMEOUT)
    at.run()
    assert not at.exception, at.exception
    return at


def login_customer(at, cid):
    at.sidebar.radio[0].set_value("Customer").run()
    at.sidebar.text_input[0].set_value(cid)
    at.sidebar.button[0].click().run()
    return at


def test_welcome_screen():
    at = boot()
    assert "Welcome to RideWise Assist" in at.header[0].value


def test_unknown_customer_is_rejected():
    at = login_customer(boot(), "CUST9999")
    assert at.sidebar.error and "No customer with ID CUST9999" in at.sidebar.error[0].value


def test_customer_session_chat_and_sources():
    at = login_customer(boot(), "cust147")         # sloppy ID is normalised
    assert "Rakesh Mishra" in at.sidebar.markdown[0].value
    at.chat_input[0].set_value("How much refund for B-1163?").run()
    assert not at.exception
    msgs = at.chat_message
    assert msgs[0].markdown[0].value == "How much refund for B-1163?"
    assert msgs[1].markdown[0].value == "Echo: How much refund for B-1163?"
    shown = " ".join(m.value for m in at.markdown)
    assert "Booking record B-1163" in shown          # friendly label for customers
    assert not at.code                               # customers never see SQL


def test_new_conversation_clears_history():
    at = login_customer(boot(), "CUST0147")
    at.chat_input[0].set_value("hello").run()
    assert len(at.chat_message) == 2
    at.sidebar.button[0].click().run()               # "New conversation"
    assert len(at.chat_message) == 0 and "Rakesh Mishra" in at.sidebar.markdown[0].value


def test_switch_user_returns_to_login():
    at = login_customer(boot(), "CUST0147")
    at.sidebar.button[1].click().run()               # "Switch user"
    assert at.sidebar.radio and "Welcome" in at.header[0].value


def test_agent_sees_sql_and_escalation_queue():
    at = boot()
    at.sidebar.radio[0].set_value("Support agent").run()
    at.sidebar.button[0].click().run()
    assert len(at.tabs) == 2 and at.tabs[1].label == "Escalation queue"
    at.chat_input[0].set_value("Which hub has most late returns?").run()
    assert any(c.value == "SELECT 1" for c in at.code)


def test_escalation_banner():
    at = login_customer(boot(), "CUST0147")
    at.chat_input[0].set_value("Please cancel my booking").run()
    assert any("Passed to a support agent" in i.value for i in at.info)


def test_example_question_buttons():
    at = login_customer(boot(), "CUST0147")
    at.button[0].click().run()                       # first example question
    assert at.chat_message[0].markdown[0].value == "Show my recent bookings"
