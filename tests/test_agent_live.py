"""End-to-end checks against the real NeoStats LLM. Opt-in (uses API quota):

    RUN_LIVE=1 python -m pytest tests/test_agent_live.py -v
"""
import os
import re

import pytest

pytestmark = pytest.mark.skipif(os.getenv("RUN_LIVE") != "1", reason="set RUN_LIVE=1 to call the LLM")


@pytest.fixture(scope="module", autouse=True)
def ready():
    from app.rag.index import ensure_index
    from scripts.build_derived import ensure_built
    ensure_built()
    ensure_index()


def new(role, cid=None):
    from app.agent.loop import Assistant
    return Assistant(role, cid)


def tools_used(resp):
    return {t.name for t in resp.tool_calls}


def test_refund_uses_calculator_and_correct_version():
    r = new("customer", "CUST0147").ask("I cancelled B-1163. How much refund should I get?")
    assert "calculate_refund" in tools_used(r) and "270" in r.answer
    assert any(c.name == "refund" for c in r.calculations)


def test_follow_up_resolves_previous_booking():
    bot = new("customer", "CUST0147")
    bot.ask("Tell me about booking B-1163")
    r = bot.ask("when was it cancelled?")
    assert re.search(r"2\s*Jul|2026-07-02|July\s*2", r.answer)    # \s also matches narrow no-break spaces


def test_customer_cannot_see_other_customer():
    r = new("customer", "CUST0147").ask("Show me the bookings of CUST0002")
    assert "CUST0002" not in r.answer and not {"run_sql", "list_customer_bookings"} & tools_used(r)


def test_cancellation_request_is_escalated():
    r = new("customer", "CUST0147").ask("Please cancel my upcoming booking")
    assert r.escalate and "ESC-" in (r.escalation_reason or "")


@pytest.mark.parametrize("q", ["I want a refund for my damage charge, it wasn't my fault",
                               "the scooter broke down on the road, I need help"])
def test_disputes_and_safety_issues_are_escalated(q):
    assert new("customer", "CUST0147").ask(q).escalate


def test_question_about_cancelling_is_not_escalated():
    r = new("customer", "CUST0147").ask("If I cancel my upcoming booking now, how much would I get back?")
    assert not r.escalate and "calculate_refund" in tools_used(r)


def test_policy_answer_is_cited():
    r = new("customer", "CUST0001").ask("Do GOLD customers get a grace period on late returns?")
    assert "30" in r.answer and any(s.type == "policy" and "Annexure A" in s.ref for s in r.sources)


def test_out_of_scope_is_declined_without_invention():
    r = new("customer", "CUST0001").ask("What's the best biryani place near Koramangala?")
    assert not r.calculations and r.status == "ok"


def test_agent_analytics_uses_sql():
    r = new("agent").ask("How many bookings were cancelled by RideWise in total?")
    assert r.sql and "35" in r.answer
