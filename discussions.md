# 📜 RideWise Assist — Key Design Decisions & Architecture Trade-offs

*(This file is alias-mapped to [DECISIONS.md](file:///g:/Neostats/DECISIONS.md))*

This document outlines the core architectural choices, rejected alternatives, current limitations, and future roadmap for **RideWise Assist**, created for the NeoStats AI Hackathon.

---

## 1. What were your three most important design decisions, and why?

### 1. Dual-Role Access Control & Scoped Tooling vs. Unrestricted SQL
* **Decision**: We enforced strict separation between **Customer** and **Support Agent** roles at both the authentication level (PBKDF2-hashed agent passwords vs. Customer ID + Phone verification) and the tool execution level. Customers never execute free-form SQL; they receive parameterized Python tools strictly scoped to their authenticated `customer_id`. Support agents receive read-only SQL querying access over sanitized, PII-masked database views.
* **Why**: Security, privacy, and data isolation. Allowing customers to run unconstrained SQL (even read-only) invites schema enumeration, indirect PII extraction, and data leaks across customer accounts. Scoping customer tools to `customer_id` guarantees zero cross-tenant data leakage.

### 2. Deterministic Python Business Calculators vs. LLM Arithmetic
* **Decision**: We offloaded all financial and policy calculations—cancellation refunds (v1 vs. v2 slabs), late return penalty fees, EV battery depletion charges, member tier grace periods, and 18% GST tax applications—to pure Python functions reading policy parameters from `config/policy_rules.yaml`.
* **Why**: LLMs notoriously fail at exact temporal math, date/time timezone conversions (converting UTC database timestamps to IST policy cutoffs), and multi-condition percentage calculations. A deterministic calculator guarantees 100% mathematical precision, eliminates hallucinated fees, and cites exact policy clauses in the response metadata.

### 3. Isolated Derived Database (`ridewise.db`) with Pre-Aggregated Views & `sqlglot` AST Validation
* **Decision**: We built a lightweight derived SQLite database (`data/derived/ridewise.db`) that attaches the raw dataset read-only. It exposes cleaned views like `v_booking_facts` (pre-calculating booking durations, late minutes, applied policy versions, and fee totals) and `v_customers` (normalizing city names like `BLR` $\rightarrow$ `Bengaluru` and masking PII). Every agent-generated SQL query is parsed and validated against an Abstract Syntax Tree (AST) via `sqlglot` before execution.
* **Why**: Raw relational tables contain data inconsistencies, PII in free-text payment remarks, and complex join paths. Pre-aggregated views reduce text-to-SQL complexity, while `sqlglot` AST parsing paired with SQLite `mode=ro` guarantees that unauthorized data mutations, system table reads, or destructive queries are physically impossible.

---

## 2. Which alternatives did you consider and reject?

### ❌ Separate Intent Router / Classifier vs. ✅ Integrated ReAct Agent Loop
* **Rejected**: A standalone classifier/router (e.g., regex or zero-shot LLM router) to direct queries exclusively to *either* the SQL database *or* the RAG document retriever.
* **Why**: Many real-world support queries require **hybrid reasoning** across both structured DB data and unstructured policy text. For example, answering *"How much refund do I get for booking B-1163?"* requires looking up booking timestamps in the DB *and* evaluating the cancellation policy version (v1 vs v2). A rigid router misclassifies these queries. The ReAct agent loop allows the model to call RAG, DB, and calculator tools dynamically in a single turn.

### ❌ Plain PDF Text Extraction vs. ✅ OCR-Enhanced Hybrid RAG (BM25 + Qdrant + PyMuPDF + RapidOCR)
* **Rejected**: Relying solely on basic text extractors (like `pypdf` or standard PDF text parsers) for policy indexing.
* **Why**: Critical business policy tables—such as v2 cancellation refund percentage slabs, GOLD member grace period rules, and EV battery charge bands in `docs/policies/`—exist as embedded raster images and scanned figures. Standard text extractors skip these images entirely. We implemented a hybrid ingestion pipeline using **PyMuPDF** for text and **RapidOCR** for scanned table figures, indexing chunks via **BM25** (sparse) + **FastEmbed/Qdrant** (dense) merged with **Reciprocal Rank Fusion (RRF)**.

### ❌ Database Mutation for Support Tickets vs. ✅ Audit-Backed Anti-Hallucination Ticket Guard
* **Rejected**: Granting the LLM write permissions to update database tables when creating support tickets or escalating claims.
* **Why**: Contest rules strictly prohibit database modifications. Instead, we implemented an external JSONL escalation queue (`data/escalations.jsonl`) governed by an **Anti-Hallucination Output Guard**. If the model claims to a customer that a support ticket was created, the guard verifies the ticket number. If missing or hallucinated, the guard auto-generates a real ticket entry to ensure every promise made to the user is true.

---

## 3. Where does your assistant currently fail or behave poorly?

1. **Run-to-Run LLM Tool Omission**: The hosted LLM (`gpt-oss-120b`) occasionally outputs plain text answers instead of invoking a tool call when multi-hop reasoning requires checking 3 or more entities in sequence.
2. **Ambiguous Customer Disambiguation**: When an agent searches for a customer by a non-unique full name (e.g. 6 duplicate name pairs exist in the dataset like *"Anil Naidu"*), the model sometimes defaults to querying the first matching customer ID rather than proactively asking the agent to clarify with a Customer ID.
3. **OCR Token Noise in Dense Lexical Searches**: While RapidOCR captures scanned policy tables effectively, minor character noise on low-resolution image borders can slightly degrade exact keyword matching in BM25 search (though dense vector embeddings successfully retrieve the relevant context).

---

## 4. What would you build next with one more week?

1. **Live Asynchronous Human-in-the-Loop Escalation Center**: Expand the Streamlit UI with WebSockets / Server-Sent Events (SSE) so human support agents can claim, inspect working traces, approve refund overrides, and resolve customer tickets directly from the UI dashboard.
2. **Semantic Query Caching Layer**: Implement a semantic cache (e.g., Redis / GPTCache) over incoming RAG and SQL queries to deliver sub-50ms responses for repeated policy inquiries and reduce token consumption.
3. **Automated Continuous Evaluation Pipeline**: Integrate real-time tracing (LangSmith / OpenTelemetry) to log production conversations, automatically detect low-confidence or escalated responses, and convert them into ground-truth test cases for `eval/questions.yaml`.
4. **Multi-Lingual Voice & Regional Chat Interface**: Add speech-to-text input and multi-lingual neural translation (Hindi, Kannada, Tamil) to assist ground staff at rental hubs and local customers.
