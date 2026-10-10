"""RideWise Assist - chat UI.

Run:  streamlit run streamlit_app.py
"""
from __future__ import annotations

import json
import logging

import streamlit as st

from app.agent.loop import Assistant
from app.agent.prompts import SUPPORT_CONTACT
from app.agent.tools import ESCALATIONS_FILE

logging.basicConfig(level=logging.WARNING)
st.set_page_config(page_title="RideWise Assist", page_icon="🛵", layout="wide")

EXAMPLES = {
    "customer": ["Show my recent bookings", "Why was I charged a penalty on my last ride?",
                 "If I cancel my upcoming booking now, how much do I get back?",
                 "Do GOLD customers get a grace period on late returns?"],
    "agent": ["Which hub had the most late returns in September 2026?", "Find customer Anil Naidu",
              "List disputed damage claims over ₹2,000", "How do v1 and v2 refund policies differ?"],
}


# ------------------------------------------------------------------ one-time startup

@st.cache_resource(show_spinner=False)
def prepare() -> str:
    """Build the cleaned DB and the policy index if needed, and load the search models."""
    from app.rag.index import dense_model, ensure_index, sparse_model
    from app.rag.retriever import reranker
    from scripts.build_derived import ensure_built
    ensure_built()
    n = ensure_index()
    dense_model(), sparse_model(), reranker()
    return f"{n} policy passages indexed"


try:
    with st.spinner("Preparing RideWise Assist (the first run downloads search models, ~1 minute)..."):
        status = prepare()
except Exception as e:      # show a clear error instead of a stack trace
    st.error(f"RideWise Assist could not start: {e}")
    st.stop()


# ------------------------------------------------------------------ session handling

def start_session(role: str, customer_id: str | None) -> None:
    old = st.session_state.get("bot")
    if old:
        old.close()
    st.session_state.bot = Assistant(role, customer_id)
    st.session_state.messages = []


def new_conversation() -> None:
    bot: Assistant = st.session_state.bot
    start_session(bot.session.role, bot.session.customer_id)


with st.sidebar:
    st.title("🛵 RideWise Assist")
    bot: Assistant | None = st.session_state.get("bot")

    if bot is None:
        st.subheader("Start a session")
        role_label = st.radio("I am a", ["Customer", "Support agent"], key="role_choice")
        cid = st.text_input("Customer ID", placeholder="e.g. CUST0001", key="cid_input") \
            if role_label == "Customer" else None
        if st.button("Start", type="primary", width="stretch"):
            try:
                start_session("customer" if role_label == "Customer" else "agent", cid)
                st.rerun()
            except ValueError as e:
                st.error(str(e))
    else:
        if bot.session.is_customer:
            p = bot.profile
            st.markdown(f"**{p['full_name']}**  \n`{p['customer_id']}` · {p['customer_tier'].title()} tier · "
                        f"KYC {p['kyc_status'].lower()}")
        else:
            st.markdown("**Support agent**  \nOperational access · personal data masked")
        c1, c2 = st.columns(2)
        if c1.button("New conversation", width="stretch"):
            new_conversation()
            st.rerun()
        if c2.button("Switch user", width="stretch"):
            bot.close()
            del st.session_state["bot"]
            st.session_state.messages = []
            st.rerun()

    st.divider()
    st.caption(f"Ready · {status}")
    st.caption(f"Need a person? {SUPPORT_CONTACT}")


if st.session_state.get("bot") is None:
    st.header("Welcome to RideWise Assist")
    st.write("Answers about bookings, payments, charges and RideWise policies. "
             "Start a session from the sidebar as a customer (with your customer ID) or as a support agent.")
    st.stop()

bot: Assistant = st.session_state.bot
is_agent = not bot.session.is_customer


# ------------------------------------------------------------------ rendering helpers

def friendly_ref(ref: str) -> str:
    kind, _, key = ref.partition(":")
    return {"booking_facts": f"Booking record {key}" if key.startswith("B-") else f"Bookings of {key}",
            "payments": f"Payment records for {key}", "damage_reports": f"Damage reports ({key})",
            "customers": "Customer profile" + (f" {key}" if key else ""), "vehicles": "Fleet and prices",
            "escalation": f"Support ticket {key}"}.get(kind, ref)


def render_details(resp: dict) -> None:
    if resp.get("escalate"):
        st.info(f"🧑‍💼 Passed to a support agent: {resp.get('escalation_reason')}")
    sources = resp.get("sources", [])
    if not (sources or resp.get("calculations")):
        return
    with st.expander("Sources and working", expanded=False):
        policies = [s for s in sources if s["type"] == "policy"]
        records = [s for s in sources if s["type"] == "database" and s["ref"] != "SQL query"]
        if policies:
            st.markdown("**Policy passages**")
            for s in policies:
                st.markdown(f"- {s['ref']}")
                if s.get("detail"):
                    st.caption(s["detail"][:400] + ("…" if len(s["detail"]) > 400 else ""))
        for c in resp.get("calculations", []):
            st.markdown(f"**Calculation: {c['name'].replace('_', ' ')}**"
                        + (f" — total ₹{c['total']:,.2f} incl. GST" if c.get("total") else ""))
            for step in c["steps"]:
                st.markdown(f"- {step}")
            if c.get("sources"):
                st.caption("Rules: " + ", ".join(c["sources"]))
            for n in c.get("notes", []):
                st.caption(n)
        if records:
            st.markdown("**Records checked**")
            st.markdown("\n".join(f"- {s['ref'] if is_agent else friendly_ref(s['ref'])}" for s in records))
        if is_agent:
            for q in resp.get("sql", []):
                st.code(q, language="sql")
            calls = resp.get("tool_calls", [])
            if calls:
                st.caption("Tools: " + ", ".join(f"{t['name']} ({t['ms']} ms{'' if t['ok'] else ', error'})"
                                                 for t in calls) + f" · {resp.get('latency_ms', 0)} ms total")


def show_message(m: dict) -> None:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        if m.get("response"):
            render_details(m["response"])


def ask(question: str) -> None:
    st.session_state.messages.append({"role": "user", "content": question})
    show_message(st.session_state.messages[-1])
    with st.chat_message("assistant"):
        with st.spinner("Checking..."):
            resp = bot.ask(question)
        st.markdown(resp.answer)
        render_details(resp.model_dump())
    st.session_state.messages.append({"role": "assistant", "content": resp.answer, "response": resp.model_dump()})


# ------------------------------------------------------------------ main area

tabs = st.tabs(["Chat", "Escalation queue"]) if is_agent else [st.container()]

with tabs[0]:
    for m in st.session_state.messages:
        show_message(m)
    if not st.session_state.messages:
        st.caption("Try asking:")
        cols = st.columns(len(EXAMPLES[bot.session.role]))
        for col, ex in zip(cols, EXAMPLES[bot.session.role]):
            if col.button(ex, width="stretch"):
                st.session_state.pending = ex
                st.rerun()

    pending = st.session_state.pop("pending", None)
    question = st.chat_input("Ask about bookings, payments, charges or policies") or pending
    if question:
        ask(question)

if is_agent:
    with tabs[1]:
        st.subheader("Requests waiting for a human")
        st.caption("Tickets raised by the assistant when a user asked for something it cannot do itself "
                   "(cancellations, refunds, disputes, safety issues).")
        rows = []
        if ESCALATIONS_FILE.exists():
            for line in ESCALATIONS_FILE.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if rows:
            st.dataframe(list(reversed(rows))[:200], width="stretch", hide_index=True,
                         column_order=["ticket", "ts", "category", "customer_id", "booking_id", "reason", "status"])
        else:
            st.write("No open requests.")
