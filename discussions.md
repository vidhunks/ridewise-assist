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
* **Decision**: We created a lightweight derived database (`data/derived/ridewise.db`) protected by strict SQL AST validation:
  * **Isolated Read-Only DB**: Raw dataset attached with `mode=ro` and `PRAGMA query_only=ON`, ensuring the original database can never be modified or corrupted.
  * **Pre-Cleaned Views**: Views like `v_booking_facts` and `v_customers` pre-clean city names (`BLR` $\rightarrow$ `Bengaluru`), mask phone/email PII, and pre-calculate late minutes, saving the LLM from writing complex multi-table joins.
  * **`sqlglot` AST Guard**: Validates every generated query against an Abstract Syntax Tree (AST) before execution to strictly enforce `SELECT`-only queries and reject any write or schema-modifying commands.
* **Why**: Prevents dirty data errors, simplifies LLM text-to-SQL generation, and physically blocks unauthorized data mutations or schema leaks.

---

## 2. Which alternatives did you consider and reject?

### ❌ Separate Intent Router / Classifier vs. ✅ Integrated ReAct Agent Loop
* **Rejected**: A standalone classifier/router (e.g., regex or zero-shot LLM router) to direct queries exclusively to *either* the SQL database *or* the RAG document retriever.
* **Why**: Many real-world support queries require **hybrid reasoning** across both structured DB data and unstructured policy text. For example, answering *"How much refund do I get for booking B-1163?"* requires looking up booking timestamps in the DB *and* evaluating the cancellation policy version (v1 vs v2). A rigid router misclassifies these queries. The ReAct agent loop allows the model to call RAG, DB, and calculator tools dynamically in a single turn.

### ❌ Plain PDF Text Extraction vs. ✅ OCR-Enhanced Hybrid RAG (BM25 + Qdrant + PyMuPDF + RapidOCR)
* **Rejected**: Relying solely on basic text extractors (like `pypdf` or standard PDF text parsers) for policy indexing.
* **Why**: Critical business policy tables—such as v2 cancellation refund percentage slabs, GOLD member grace period rules, and EV battery charge bands in `docs/policies/`—exist as embedded raster images and scanned figures. Standard text extractors skip these images entirely. We implemented a hybrid ingestion pipeline using **PyMuPDF** for text and **RapidOCR** for scanned table figures, indexing chunks via **BM25** (sparse) + **FastEmbed/Qdrant** (dense) merged with **Reciprocal Rank Fusion (RRF)**.

### ❌ Human-in-the-Loop (HITL) Sandbox for Database Writes & Deletes vs. ✅ Enforced Read-Only DB Architecture
* **Rejected**: A Human-in-the-Loop approval system allowing the LLM to propose `INSERT`, `UPDATE`, or `DELETE` SQL queries requiring human support agent authorization.
* **Why**: We evaluated the hackathon problem statement and confirmed that database mutations are strictly forbidden (`mode=ro`). Nothing in the user stories requires database writes. Instead of building a complex write/delete approval workflow, we enforced a strict read-only database architecture (`PRAGMA query_only=ON` + AST validation) and routed all customer disputes, refund requests, and waivers to an external human escalation queue (`data/escalations.jsonl`).

---

## 3. Where does your assistant currently fail or behave poorly?

1. **Ambiguous Customer Disambiguation**: When an agent searches for a customer by a non-unique full name (e.g. 6 duplicate name pairs exist in the dataset like *"Anil Naidu"*), the model sometimes defaults to querying the first matching customer ID rather than proactively asking the agent to clarify with a Customer ID.
2. **OCR Token Noise in Dense Lexical Searches**: While RapidOCR captures scanned policy tables effectively, minor character noise on low-resolution image borders can slightly degrade exact keyword matching in BM25 search (though dense vector embeddings successfully retrieve the relevant context).

---

## 4. What would you build next with one more week?

1. **Persistent Conversation History & Session Reloading**: Save customer and support agent chat histories into a permanent database so users can reload past conversations across sessions and devices.
2. **Semantic Query Caching Layer**: Implement a semantic cache (e.g., Redis / GPTCache) over incoming RAG and SQL queries to deliver sub-50ms responses for repeated policy inquiries and reduce token consumption.
3. **Multi-Lingual Voice & Regional Chat Interface**: Add speech-to-text input and multi-lingual neural translation (Hindi, Kannada, Tamil) to assist ground staff at rental hubs and local customers.
