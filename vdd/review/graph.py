"""
LangGraph review loop: an LLM reads the deterministic report, decides what
to independently re-check via tool calls, and either corrects it (only
with verified evidence) or escalates it for the analyst -- looping until
the model's own `verdict` says the report is ready, up to a hard safety
cap (`max_iterations`, a cost/latency backstop, not the intended stop
condition).

The LLM never edits the report directly. It proposes a correction to a
`resolved[parameter_id]` value or an `entity[field]` value; this graph's
`apply_corrections` node patches that and calls back into the existing
deterministic `ScoringEngine` / `build_context` to re-derive the report
context, which the pipeline renders once at the end. See vdd/pipeline.py
for how this graph is invoked and what happens if it errors out entirely
(falls back to the deterministic-only report -- this graph is not
responsible for that fallback, only for running the loop when it IS
invoked).
"""
from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from vdd.report.build_context import REPORT_ENTITY_FIELDS, build_context
from vdd.report.render import build_text
from vdd.resolve.resolvers import Resolved
from vdd.review.model import build_review_model, select_provider
from vdd.review.schemas import ReviewReport
from vdd.review.state import ReviewState
from vdd.review.tools import make_tools
from vdd.review.trace import extract_pass_usage, serialize_messages
from vdd.score.engine import ScoringEngine

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "reviewer.md").read_text(encoding="utf-8")

DEFAULT_MAX_ITERATIONS = 4

# Model calls allowed per pass on the Foundry provider (the last one is
# reserved for ReviewReport -- see _FinishWithReport). Gemini converges in 2
# calls and OpenAI in 2-3 on every trace to date, so 8 is a backstop for a
# model that keeps re-checking, not a budget a good reviewer should approach.
# The Qwen experiments (QwenTest branch) derived this from a 32k context
# window; Foundry-hosted models have 128k+ so a constant is enough here.
FOUNDRY_MAX_MODEL_CALLS = int(os.environ.get("FOUNDRY_MAX_MODEL_CALLS", "8"))


class _FinishWithReport(AgentMiddleware):
    """Guarantees the pass ends with a ReviewReport. create_agent binds every
    turn with tool_choice="any" when a structured output is requested, so the
    model can never answer in prose -- calling `ReviewReport` is the only
    exit. Observed 2026-09-16 with a small self-hosted model: it spent every
    allowed call on recheck tools (one an identical repeat) and never took
    that exit. On the last allowed call this strips every other tool from the
    request, leaving ReviewReport as the only legal move, and appends a
    one-line nudge for that call only (not persisted to state)."""

    def __init__(self, model_calls: int):
        super().__init__()
        self.model_calls = model_calls
        self.calls = 0

    def wrap_model_call(self, request, handler):
        self.calls += 1
        if self.calls >= self.model_calls:
            logger.info("Review: model call %d/%d -- forcing ReviewReport (all other tools withheld).",
                        self.calls, self.model_calls)
            nudge = HumanMessage(content="Your tool budget for this pass is used up. Submit your ReviewReport "
                                         "now, using only the evidence already in this conversation -- anything "
                                         "you could not verify goes in as action='escalate'.")
            request = request.override(tools=[], messages=list(request.messages) + [nudge])
        return handler(request)


def _bounded_loop_middleware(model_calls: int) -> list:
    return [
        _FinishWithReport(model_calls),
        # Backstop only: reached if the forced ReviewReport call itself fails
        # schema validation twice (ToolStrategy re-prompts on a bad payload).
        ModelCallLimitMiddleware(run_limit=model_calls + 2, exit_behavior="end"),
    ]


def _tool_budget_section(tool_calls: int) -> str:
    return ("## Tool budget for this pass\n"
            f"You may make at most {tool_calls} tool call(s) this pass. Then you MUST submit your findings by "
            "calling `ReviewReport` -- that call is how you finish; without it the pass fails and nothing you "
            "found is recorded. Never call the same tool with the same arguments twice in a pass: the result "
            "will not change. Spend calls on the cross-check items first; anything you can't verify within "
            "budget goes in as action='escalate', not as another call.")


def _dump_failed_pass(state: ReviewState, messages: list, error: str) -> Optional[str]:
    """Persist the partial transcript of a pass that didn't produce a
    ReviewReport -- the ReAct loop's own messages are the only evidence of
    WHY (which tool was called how often, how big each result was, what the
    model actually emitted). Same local-only JSON idiom as trace.py; never
    transmitted."""
    out_dir = state.get("out_dir")
    serialized = serialize_messages(messages)
    summary = []
    for i, m in enumerate(serialized):
        name = m.get("tool_name") or ",".join(tc.get("name") or "?" for tc in m.get("tool_calls") or [])
        summary.append(f"  #{i:<3} {m['type']:<11} {name:<28} {len(str(m.get('content') or '')):>7,} chars")
    logger.warning("Review pass %d failed after %d messages (%s). Transcript:\n%s",
                   state.get("iteration", 1), len(serialized), error[:300], "\n".join(summary) or "  (none)")
    if not out_dir:
        return None
    os.makedirs(out_dir, exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in state.get("vendor_name", "")).strip("_").upper() or "VENDOR"
    path = os.path.join(out_dir, f"{safe}_review_failure_pass{state.get('iteration', 1)}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"vendor_name": state.get("vendor_name"), "iteration": state.get("iteration", 1),
                   "provider": select_provider(), "error": error, "messages": serialized}, f, indent=2, default=str)
    logger.warning("Full transcript written to %s", path)
    return path


def _pass_instructions(iteration: int) -> str:
    if iteration <= 1:
        return ("This is pass 1. Do a broad read of the whole report, anchored on the cross-check "
                "items plus anything else that looks off.")
    return (f"This is pass {iteration}. The report below has already been revised based on your own "
            "previous findings -- treat nothing as pre-cleared, including sections you approved last "
            "time. Re-verify previously-approved claims with fresh tool calls wherever you don't yet "
            "have a definitive, citable answer for them, not just the rows that changed since last pass.")


def _format_resolved(resolved: dict[str, Any]) -> str:
    lines = [f"- {pid}: value={getattr(r, 'value', None)!r} unresolved={getattr(r, 'unresolved', False)} "
             f"source={getattr(r, 'source', '')!r} note={getattr(r, 'note', '')!r}"
             for pid, r in resolved.items()]
    return "\n".join(lines) or "(none)"


def _normalise_parameter_ids(findings: list[dict], state: ReviewState) -> None:
    """Map a finding's parameter_id from the report's display code (POA-01) to
    the scoring-model parameterId (addr_ownership_type) in place. Observed
    2026-09-16 with Gemini: one pass named all four findings by display code
    even though the schema asks for the parameterId -- apply_corrections
    would then find no such key in `resolved` and silently downgrade a
    verified correction to an escalation, and compare_reviews.py's recall
    reads 0/4 for a pass that found everything. Case-insensitive; an id that
    is already a parameterId (or unknown) is left alone."""
    code_ids = {k.upper(): v for k, v in (state.get("context") or {}).get("code_ids", {}).items()}
    for f in findings:
        pid = f.get("parameter_id")
        if pid and str(pid).strip().upper() in code_ids:
            f["parameter_id"] = code_ids[str(pid).strip().upper()]


def _format_entity_fields(entity: dict[str, Any]) -> str:
    """The entity keys the report reads, with their current values -- the only
    valid `field` targets for a correction. A key the extractors never set
    is shown as None so the reviewer can see what the report is missing."""
    lines = []
    for k in REPORT_ENTITY_FIELDS:
        v = entity.get(k)
        if isinstance(v, (list, tuple, dict)):
            shown = f"<{type(v).__name__} of {len(v)}>"
        else:
            shown = repr(v)[:100] if v is not None else "None  (report shows N/A / Not Available)"
        lines.append(f"- {k}: {shown}")
    return "\n".join(lines)


def _format_history(passes: list[dict]) -> str:
    lines = []
    for i, p in enumerate(passes, start=1):
        lines.append(f"### Pass {i} (verdict: {p.get('verdict')}, note: {p.get('thoroughness_note')})")
        for f in p.get("findings", []):
            lines.append(f"- [{f.get('action')}/{f.get('confidence')}] "
                         f"{f.get('parameter_id') or f.get('field')}: {f.get('issue')} -- {f.get('proposed_note')}")
    return "\n".join(lines) or "(none)"


def _build_user_message(state: ReviewState) -> str:
    iteration = state.get("iteration", 1)
    parts = [
        _pass_instructions(iteration),
        "",
        f"## Vendor\n{state.get('vendor_name', '')}",
        "",
        "## Resolved parameter values (parameterId -> value/source/note/unresolved)",
        _format_resolved(state.get("resolved", {})),
        "",
        "## Cross-check items (the pipeline's own flagged judgment calls / gaps)",
        "\n".join(f"- {i}" for i in state.get("cross_check_items", [])) or "(none)",
        "",
        "## Entity fields the report reads (the ONLY valid `field` values for a correction -> current value)",
        _format_entity_fields(state.get("entity", {})),
        "",
        "## The report as the analyst will see it (plain-text rendering)",
        build_text(state.get("context", {})),
    ]
    if iteration > 1:
        parts += ["", "## History of previous passes", _format_history(state.get("passes", []))]
    return "\n".join(parts)


def llm_review(state: ReviewState) -> dict:
    provider = select_provider()
    model = build_review_model()
    if model is None:
        raise RuntimeError("No usable LLM key configured (GEMINI_API_KEY/OPENAI_API_KEY/FOUNDRY_*) -- the "
                            "review graph must not be invoked without one; see vdd/pipeline.py's caller.")
    tools = make_tools(state.get("client"))
    user_msg = _build_user_message(state)

    # Gemini and OpenAI converge on their own (every trace to date); an
    # open-weight model on Foundry is unproven, so it gets the bounded loop:
    # a stated budget in the prompt and a forced ReviewReport on the last call.
    middleware: list = []
    if provider == "foundry":
        middleware = _bounded_loop_middleware(FOUNDRY_MAX_MODEL_CALLS)
        user_msg += "\n\n" + _tool_budget_section(FOUNDRY_MAX_MODEL_CALLS - 1)
    agent = create_agent(model=model, tools=tools, system_prompt=_SYSTEM_PROMPT, response_format=ReviewReport,
                         middleware=middleware)

    # Streamed rather than invoke()d so the messages accumulated so far survive
    # an exception mid-pass (a provider rejecting a later turn, a tool crash) --
    # invoke() would raise with nothing to show for what the loop had done.
    result: dict = {}
    try:
        for result in agent.stream({"messages": [{"role": "user", "content": user_msg}]}, stream_mode="values"):
            pass
    except Exception as e:
        _dump_failed_pass(state, result.get("messages", []), f"{type(e).__name__}: {e}")
        raise
    report: ReviewReport | None = result.get("structured_response")
    if report is None:
        messages = result.get("messages", [])
        _dump_failed_pass(state, messages, "agent finished without a structured ReviewReport")
        last = messages[-1] if messages else None
        preview = str(getattr(last, "content", ""))[:500]
        raise RuntimeError("LLM returned no usable structured ReviewReport -- see the transcript logged above. "
                           f"Last message: {preview!r}")
    report_dict = report.model_dump()
    _normalise_parameter_ids(report_dict["findings"], state)

    return {
        "findings": report_dict["findings"],
        "passes": [report_dict],
        "message_traces": [serialize_messages(result.get("messages", []))],
        "pass_usage": [extract_pass_usage(result.get("messages", []))],
        "last_verdict": report_dict["verdict"],
        "provider": provider,
    }


def apply_corrections(state: ReviewState) -> dict:
    resolved = copy.deepcopy(state["resolved"])
    entity = copy.deepcopy(state["entity"])
    corrections: list[dict] = []
    escalations: list[dict] = []

    this_pass_findings = state["passes"][-1]["findings"] if state.get("passes") else []
    for f in this_pass_findings:
        record = {**f, "pass": state.get("iteration", 1)}
        action, confidence = f.get("action"), f.get("confidence")

        if action == "correct" and confidence == "verified":
            pid, field = f.get("parameter_id"), f.get("field")
            if pid and pid in resolved:
                before = resolved[pid]
                record["before"] = {"value": before.value, "note": before.note}
                resolved[pid] = Resolved.ok(f.get("proposed_value"), source="llm-review",
                                             note=f.get("proposed_note", ""))
                corrections.append(record)
            elif field in REPORT_ENTITY_FIELDS:
                record["before"] = {"value": entity.get(field)}
                entity[field] = f.get("proposed_value")
                corrections.append(record)
            elif field:
                # Setting an arbitrary key on `entity` would be logged as a
                # correction while the rendered report stays exactly the same.
                record["reason"] = (f"action=='correct' names entity field {field!r}, which the report does not "
                                    f"read -- escalated instead (valid: {', '.join(REPORT_ENTITY_FIELDS)})")
                escalations.append(record)
            else:
                record["reason"] = "action=='correct' but no matching parameter_id/field on this report -- escalated instead"
                escalations.append(record)
        elif action == "escalate" or (action == "correct" and confidence != "verified"):
            escalations.append(record)
        # action == "confirm_ok": already recorded in `passes`, nothing further to do.

    engine = ScoringEngine(state["scoring_model_path"])
    result = engine.score_no_consent(resolved)
    context = build_context(entity, result)

    return {
        "entity": entity, "resolved": resolved, "context": context,
        "corrections_applied": corrections, "escalations": escalations,
        "iteration": state.get("iteration", 1) + 1,
    }


def _route_after_apply(state: ReviewState) -> str:
    max_iter = state.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    if state.get("last_verdict") == "needs_another_pass" and state.get("iteration", 1) <= max_iter:
        return "llm_review"
    return "finalize"


def finalize(state: ReviewState) -> dict:
    return {"approved": state.get("last_verdict") == "approved"}


def build_review_graph() -> CompiledStateGraph:
    graph = StateGraph(ReviewState)
    graph.add_node("llm_review", llm_review)
    graph.add_node("apply_corrections", apply_corrections)
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "llm_review")
    graph.add_edge("llm_review", "apply_corrections")
    graph.add_conditional_edges("apply_corrections", _route_after_apply, ["llm_review", "finalize"])
    graph.add_edge("finalize", END)

    return graph.compile()
