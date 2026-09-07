"""Structured output schemas for the reviewer agent's `response_format`."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class Finding(BaseModel):
    parameter_id: Optional[str] = Field(
        default=None,
        description="Scoring-model parameterId this finding is about (e.g. 'com_bank_verification'), if it's "
                    "a scored parameter.")
    field: Optional[str] = Field(
        default=None,
        description="Non-scored entity/display field this finding is about (e.g. 'nic_5_description'), if it "
                    "isn't a scored parameter.")
    issue: str = Field(description="What looks wrong or under-verified, in plain language.")
    severity: Literal["low", "medium", "high"] = "medium"
    proposed_value: Optional[Any] = Field(
        default=None,
        description="The corrected value, if action=='correct'. Must be a valid bucket value for this "
                    "parameter's scoring model, or plain text for a display field.")
    proposed_note: str = Field(
        description="Evidence for this finding -- MUST cite exactly which tool call (name + key result) or "
                    "existing resolver note backs it. Never state a fact with no cited source.")
    confidence: Literal["verified", "unverified"] = Field(
        description="'verified' ONLY if a tool call you made this pass returned a definitive, citable answer "
                    "that settles the question. A suspicion, an inference, or a source that didn't respond is "
                    "'unverified'.")
    action: Literal["correct", "escalate", "confirm_ok"] = Field(
        description="'correct' is only valid together with confidence=='verified'. 'confirm_ok' means you "
                    "checked this and it's fine (recorded for the audit trail). Everything else is 'escalate'.")


class ReviewReport(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    verdict: Literal["approved", "needs_another_pass"] = Field(
        description="Your own judgment on whether this report is ready for the analyst. 'approved' means "
                    "you're confident an additional tool call would not change your assessment -- don't "
                    "approve just because you've run out of ideas; if in doubt, request another pass.")
    thoroughness_note: str = Field(
        description="What depth of scrutiny you actually applied this pass, and -- if verdict is "
                    "'needs_another_pass' -- specifically what you still want to dig into next pass.")
