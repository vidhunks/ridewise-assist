"""The fixed response format every turn returns (to the UI, logs and tests)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Source(BaseModel):
    type: Literal["database", "policy", "calculation"]
    ref: str                        # e.g. "booking_facts:B-0001", "RW-POL-03 v2, Annexure 1 (p. 2)"
    detail: str | None = None       # passage excerpt, SQL, or calculation steps


class Calculation(BaseModel):
    name: str
    total: float | None = None
    steps: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ToolCall(BaseModel):
    name: str
    args: dict
    ok: bool
    ms: int
    error: str | None = None


class AssistantResponse(BaseModel):
    answer: str
    status: Literal["ok", "refused", "error"] = "ok"
    sources: list[Source] = Field(default_factory=list)
    sql: list[str] = Field(default_factory=list)
    calculations: list[Calculation] = Field(default_factory=list)
    escalate: bool = False
    escalation_reason: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    latency_ms: int = 0
