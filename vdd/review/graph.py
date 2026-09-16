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

Context budget: the reviewer is shown `render.build_text(context)` -- the
same report the analyst gets, as plain text -- never the HTML. Measured on
a real Skandan report with the exact Qwen3.8 tokenizer, the HTML alone was
~16k tokens of a ~21k-token first call; the data underneath it is a small
fraction of that. This matters for the self-hosted Qwen path (32k window,
see `_qwen_output_budget`) but also cuts Gemini spend per pass.
"""
from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any, Optional

import requests
from langchain.agents import create_agent
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from vdd.report.build_context import build_context
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

# Fallback only -- the real value is read off the server's /tokenize response
# (`max_model_len`) on every pass. Set this if that endpoint is unavailable.
QWEN_MAX_MODEL_LEN_FALLBACK = int(os.environ.get("QWEN_MAX_MODEL_LEN", "32768"))
# With thinking disabled (model.py) the output is just the ReviewReport JSON:
# real passes in out/*_review_trace.json ran 1.8k-3.3k chars (~0.6-1k tokens),
# so 4096 is generous. The old 16384 cap left no room for tool results.
QWEN_MAX_OUTPUT_TOKENS = 4096
QWEN_MIN_OUTPUT_TOKENS = 1024
# Reserved for context growth WITHIN a pass: every tool-call round trip appends
# the model's call + the tool's result, but max_tokens is fixed for the whole
# pass. tools.py caps each tool result (_MAX_TOOL_RESULT_CHARS) so this covers
# a typical pass's 3-5 calls; the count itself is exact (server tokenizer).
QWEN_MIDPASS_RESERVE = 8192
# Used only if /tokenize is unreachable. chars/3 undercounted a real prompt by
# ~13% (2.86 chars/token measured) -- plus a flat allowance for the rendered
# tool schemas, which the estimate can't see.
_FALLBACK_CHARS_PER_TOKEN = 2.5
_FALLBACK_TOOL_SCHEMA_TOKENS = 2500


def _qwen_count_tokens(messages: list[dict], tools: list[dict]) -> Optional[tuple[int, int]]:
    """Exact (prompt_tokens, max_model_len) from vLLM's /tokenize, which applies
    the same chat template + tool rendering + chat_template_kwargs the real
    completion call will -- so this IS the number the server checks against
    --max-model-len, not an estimate. Costs no GPU inference. None on any
    failure so the caller can fall back rather than abort the review."""
    base = os.environ.get("QWEN_BASE_URL", "").rstrip("/")
    if not base:
        return None
    # /tokenize lives at the server root, not under the OpenAI-compatible /v1.
    url = (base[:-3] if base.endswith("/v1") else base) + "/tokenize"
    body = {"model": os.environ.get("QWEN_MODEL", "Qwen/Qwen3.8-27B"), "messages": messages,
            "tools": tools, "add_generation_prompt": True,
            "chat_template_kwargs": {"enable_thinking": False}}
    try:
        r = requests.post(url, json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        return int(data["count"]), int(data.get("max_model_len") or QWEN_MAX_MODEL_LEN_FALLBACK)
    except Exception as e:
        logger.warning("vLLM /tokenize unavailable (%s: %s) -- falling back to a character estimate "
                        "for the Qwen output budget.", type(e).__name__, e)
        return None


def _qwen_output_budget(user_msg: str, tools: list) -> int:
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user_msg}]
    # Mirror what create_agent binds: every tool, plus ReviewReport itself (the
    # structured-output ToolStrategy adds it as one more tool -- visible as a
    # `ReviewReport` tool_result in every trace).
    tool_schemas = [convert_to_openai_tool(t) for t in tools] + [convert_to_openai_tool(ReviewReport)]
    counted = _qwen_count_tokens(messages, tool_schemas)
    if counted is not None:
        prompt_tokens, max_model_len = counted
        how = "exact"
    else:
        prompt_tokens = int((len(_SYSTEM_PROMPT) + len(user_msg)) / _FALLBACK_CHARS_PER_TOKEN) + _FALLBACK_TOOL_SCHEMA_TOKENS
        max_model_len = QWEN_MAX_MODEL_LEN_FALLBACK
        how = "estimated"

    available = max_model_len - prompt_tokens - QWEN_MIDPASS_RESERVE
    budget = max(QWEN_MIN_OUTPUT_TOKENS, min(QWEN_MAX_OUTPUT_TOKENS, available))
    if available < QWEN_MIN_OUTPUT_TOKENS:
        logger.warning(
            "Qwen review: prompt is %d tokens (%s) against max_model_len=%d -- only %d left after the "
            "%d-token mid-pass reserve, so max_tokens is pinned at the %d floor. Expect the server to "
            "reject a later call in this pass if the model uses several tools; raise --max-model-len.",
            prompt_tokens, how, max_model_len, available, QWEN_MIDPASS_RESERVE, budget)
    else:
        logger.info("Qwen review: prompt %d tokens (%s), max_model_len %d, max_tokens %d, "
                    "%d tokens of headroom for tool results this pass.",
                    prompt_tokens, how, max_model_len, budget, available - budget)
    return budget


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
        "## The report as the analyst will see it (plain-text rendering)",
        build_text(state.get("context", {})),
    ]
    if iteration > 1:
        parts += ["", "## History of previous passes", _format_history(state.get("passes", []))]
    return "\n".join(parts)


def llm_review(state: ReviewState) -> dict:
    provider = select_provider()
    user_msg = _build_user_message(state)
    tools = make_tools(state.get("client"))

    qwen_max_tokens = _qwen_output_budget(user_msg, tools) if provider == "qwen" else None
    model = build_review_model(qwen_max_tokens=qwen_max_tokens)
    if model is None:
        raise RuntimeError("No usable LLM key configured (GEMINI_API_KEY/OPENAI_API_KEY) -- the review "
                            "graph must not be invoked without one; see vdd/pipeline.py's caller.")
    agent = create_agent(model=model, tools=tools, system_prompt=_SYSTEM_PROMPT, response_format=ReviewReport)

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
