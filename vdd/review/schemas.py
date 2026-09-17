"""Structured output schemas for the reviewer agent's `response_format`."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class Finding(BaseModel):
    parameter_id: Optional[str] = Field(
        default=None,
        description="Scoring-model parameterId this finding is about, exactly as listed under 'Resolved "
                    "parameter values' and shown in [brackets] on the report's scoring rows (e.g. "
                    "'com_bank_verification') -- never the display code such as 'COM-07'.")
    field: Optional[str] = Field(
        default=None,
        description="Non-scored entity/display field this finding is about, if it isn't a scored parameter. "
                    "Must be one of the keys listed under 'Entity fields the report reads' in the message "
                    "(e.g. 'date_of_registration' for the 'GST Since' row) -- a correction naming any other "
                    "field cannot be applied.")
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
        description="'approved' = ready for the analyst: every correction you could verify has been applied "
                    "and everything else has been escalated. Escalations do NOT block approval -- the analyst "
                    "resolves them, not another pass. 'needs_another_pass' only if you still have a specific "
                    "tool call in mind that could turn an escalation into a verified correction, or a "
                    "correction you made this pass needs to be re-read in the revised report.")
    thoroughness_note: str = Field(
        description="What depth of scrutiny you actually applied this pass, and -- if verdict is "
                    "'needs_another_pass' -- specifically what you still want to dig into next pass.")
