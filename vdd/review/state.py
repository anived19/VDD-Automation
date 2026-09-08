"""LangGraph state schema for the report review loop.

Fields without `Annotated[..., operator.add]` use LangGraph's default
"last write wins" replace semantics (entity/resolved/context/html get
replaced wholesale by `apply_corrections` after every pass -- there's
nothing to accumulate about them). `findings`/`corrections_applied`/
`escalations`/`passes` accumulate across the whole run so the final trace
shows every pass, not just the last one.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Optional, TypedDict


class ReviewState(TypedDict, total=False):
    vendor_name: str
    scoring_model_path: str
    client: Optional[Any]  # FinoscaleClient | None -- for the live-refetch review tools

    entity: dict[str, Any]
    resolved: dict[str, Any]  # dict[str, vdd.resolve.resolvers.Resolved]
    context: dict[str, Any]
    html: str
    cross_check_items: list[str]

    iteration: int
    max_iterations: int
    last_verdict: str
    provider: Optional[str]
    approved: bool

    findings: Annotated[list[dict], operator.add]
    corrections_applied: Annotated[list[dict], operator.add]
    escalations: Annotated[list[dict], operator.add]
    passes: Annotated[list[dict], operator.add]  # one ReviewReport dict per llm_review call
    message_traces: Annotated[list[list[dict]], operator.add]  # one serialized ReAct message list per pass
    pass_usage: Annotated[list[dict], operator.add]  # one token-usage summary (trace.extract_pass_usage) per pass
