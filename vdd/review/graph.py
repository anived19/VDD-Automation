"""
LangGraph review loop: an LLM reads the deterministic report, decides what
to independently re-check via tool calls, and either corrects it (only
with verified evidence) or escalates it for the analyst -- looping until
the model's own `verdict` says the report is ready, up to a hard safety
cap (`max_iterations`, a cost/latency backstop, not the intended stop
condition).

The LLM never edits HTML directly. It proposes a correction to a
`resolved[parameter_id]` value or an `entity[field]` value; this graph's
`apply_corrections` node patches that and calls back into the existing
deterministic `ScoringEngine` / `build_context` / `render.build` to
regenerate the report. See vdd/pipeline.py for how this graph is invoked
and what happens if it errors out entirely (falls back to the
deterministic-only report -- this graph is not responsible for that
fallback, only for running the loop when it IS invoked).
"""
from __future__ import annotations

import copy
import logging
import re
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from vdd.report import render
from vdd.report.build_context import build_context
from vdd.resolve.resolvers import Resolved
from vdd.review.model import build_review_model, select_provider
from vdd.review.schemas import ReviewReport
from vdd.review.state import ReviewState
from vdd.review.tools import make_tools
from vdd.review.trace import extract_pass_usage, serialize_messages
from vdd.score.engine import ScoringEngine

logger = logging.getLogger(__name__)


def _strip_head(html: str) -> str:
    """Drop the <head>...</head> block (~3.5k+ tokens of inline CSS/meta)
    before the report goes to the reviewer -- it reasons over the data,
    never the styling."""
    return re.sub(r"<head\b[^>]*>.*?</head>", "", html, count=1, flags=re.DOTALL | re.IGNORECASE)

_SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "reviewer.md").read_text(encoding="utf-8")

DEFAULT_MAX_ITERATIONS = 4


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
        "## Rendered report (HTML)",
        _strip_head(state.get("html", "")),
    ]
    if iteration > 1:
        parts += ["", "## History of previous passes", _format_history(state.get("passes", []))]
    return "\n".join(parts)


def llm_review(state: ReviewState) -> dict:
    provider = select_provider()
    model = build_review_model()
    if model is None:
        raise RuntimeError("No usable LLM key configured (GEMINI_API_KEY/OPENAI_API_KEY) -- the review "
                            "graph must not be invoked without one; see vdd/pipeline.py's caller.")
    tools = make_tools(state.get("client"))
    agent = create_agent(model=model, tools=tools, system_prompt=_SYSTEM_PROMPT, response_format=ReviewReport)

    user_msg = _build_user_message(state)
    result = agent.invoke({"messages": [{"role": "user", "content": user_msg}]})
    report: ReviewReport | None = result.get("structured_response")

    if report is None:
        # --- diagnostic logging: capture WHY the structured response was None ---
        messages = result.get("messages", [])
        last_ai = messages[-1] if messages else None
        raw_content = getattr(last_ai, "content", None) if last_ai else None
        finish_reason = None
        response_meta = getattr(last_ai, "response_metadata", None) or {}
        if isinstance(response_meta, dict):
            finish_reason = response_meta.get("finish_reason")

        # Classify the raw output shape for quick triage
        content_str = str(raw_content) if raw_content is not None else "(empty)"
        if len(content_str) > 2000:
            content_preview = content_str[:2000] + f"… [truncated, total {len(content_str)} chars]"
        else:
            content_preview = content_str
        looks_like = "unknown"
        if '"tool_calls"' in content_str or "<tool_call>" in content_str:
            looks_like = "tool-call block (possible parser mismatch)"
        elif content_str.rstrip().endswith(("{", '",', '"', ":")):
            looks_like = "truncated JSON (likely token budget exhaustion)"
        elif content_str.strip().startswith("{"):
            looks_like = "JSON object (possible schema validation failure)"
        else:
            looks_like = "plain prose or non-JSON"

        logger.warning(
            "structured_response is None — the LLM's output could not be parsed "
            "into ReviewReport. Diagnostics:\n"
            "  finish_reason: %s\n"
            "  output_shape: %s\n"
            "  raw_last_message: %s",
            finish_reason, looks_like, content_preview,
        )
        raise RuntimeError(
            f"LLM returned no usable structured ReviewReport (finish_reason={finish_reason}, "
            f"output_shape={looks_like}). See WARNING log above for the raw output. "
            f"Common causes: output truncated by max_tokens, tool-call parser mismatch, "
            f"or schema validation failure."
        )

    report_dict = report.model_dump()

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
            elif field:
                record["before"] = {"value": entity.get(field)}
                entity[field] = f.get("proposed_value")
                corrections.append(record)
            else:
                record["reason"] = "action=='correct' but no matching parameter_id/field on this report -- escalated instead"
                escalations.append(record)
        elif action == "escalate" or (action == "correct" and confidence != "verified"):
            escalations.append(record)
        # action == "confirm_ok": already recorded in `passes`, nothing further to do.

    engine = ScoringEngine(state["scoring_model_path"])
    result = engine.score_no_consent(resolved)
    context = build_context(entity, result)
    html = render.build(context)

    return {
        "entity": entity, "resolved": resolved, "context": context, "html": html,
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
