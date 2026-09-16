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
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import requests
from langchain.agents import create_agent
from langchain.agents.middleware import (AgentMiddleware, ClearToolUsesEdit, ContextEditingMiddleware,
                                         ModelCallLimitMiddleware)
from langchain_core.messages import HumanMessage
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
# real passes in out/*_review_trace.json ran 1.8k-3.3k chars (~0.6-1k tokens).
# This is a per-TURN cap, and every turn's output stays in the context for the
# rest of the pass, so it is also the single biggest lever on mid-pass growth.
QWEN_MAX_OUTPUT_TOKENS = 2048
# The invariant the server enforces on EVERY turn, not just the first:
#     prompt_so_far + max_tokens <= max_model_len
# A pass that fit its first call at 7k tokens still died at >=28.7k after the
# model's own turns piled up (observed 2026-09-16). So the loop is bounded, not
# just the first call: the number of model calls per pass is derived from the
# measured headroom, assuming each round trip can add a full max_tokens of
# model output plus one capped tool result. Older tool results are also
# cleared once the conversation passes QWEN_TOOL_CLEAR_TRIGGER_APPROX.
QWEN_TOOL_RESULT_TOKENS = 2000            # ~tools._MAX_TOOL_RESULT_CHARS / 3
QWEN_MAX_MODEL_CALLS = 8
QWEN_MIN_MODEL_CALLS = 2                  # one tool round trip + the ReviewReport call
# langchain's approximate counter (~chars/4) reads ~30% LOW for this content
# (2.86 chars/token measured), and it counts state messages only -- not the
# system prompt or tool schemas. 10k approximate ~= the 4.5k-token user
# message plus ~8k real tokens of tool traffic, at which point all but the 2
# most recent tool results are replaced with a placeholder.
QWEN_TOOL_CLEAR_TRIGGER_APPROX = 10000
QWEN_TOOL_RESULTS_KEEP = 2
# Stated in the prompt for every provider; enforced (see _FinishWithReport) only
# for Qwen, where the number is derived from the measured context headroom.
# Gemini converges in 1-2 tool calls on its own (every out/*_review_trace.json).
DEFAULT_TOOL_CALLS_PER_PASS = 6
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


def _qwen_budget(user_msg: str, tools: list) -> tuple[int, int]:
    """-> (max_tokens per turn, model calls allowed this pass)."""
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

    max_tokens = QWEN_MAX_OUTPUT_TOKENS
    headroom = max_model_len - prompt_tokens - max_tokens
    per_round_trip = max_tokens + QWEN_TOOL_RESULT_TOKENS
    calls = max(QWEN_MIN_MODEL_CALLS, min(QWEN_MAX_MODEL_CALLS, headroom // per_round_trip))
    if headroom < per_round_trip * QWEN_MIN_MODEL_CALLS:
        logger.warning(
            "Qwen review: prompt is %d tokens (%s) against max_model_len=%d -- only %d tokens of headroom, "
            "less than the %d two round trips need. The pass may be rejected by the server; raise "
            "--max-model-len on the vLLM server.", prompt_tokens, how, max_model_len, headroom,
            per_round_trip * QWEN_MIN_MODEL_CALLS)
    else:
        logger.info("Qwen review: prompt %d tokens (%s), max_model_len %d, max_tokens %d/turn, "
                    "%d tokens of headroom -> at most %d model calls this pass.",
                    prompt_tokens, how, max_model_len, max_tokens, headroom, calls)
    return max_tokens, calls


class _FinishWithReport(AgentMiddleware):
    """Guarantees the pass ends with a ReviewReport. create_agent binds every
    turn with tool_choice="any" when a structured output is requested, so the
    model can never answer in prose -- calling `ReviewReport` is the only
    exit. Observed 2026-09-16: Qwen3.8-27B spent all 5 allowed calls on
    recheck tools (one an identical repeat) and never took that exit. On the
    last allowed call this strips every other tool from the request, leaving
    ReviewReport as the only legal move, and appends a one-line nudge for
    that call only (not persisted to state)."""

    def __init__(self, model_calls: int):
        super().__init__()
        self.model_calls = model_calls
        self.calls = 0

    def wrap_model_call(self, request, handler):
        self.calls += 1
        if self.calls >= self.model_calls:
            logger.info("Qwen review: model call %d/%d -- forcing ReviewReport (all other tools withheld).",
                        self.calls, self.model_calls)
            nudge = HumanMessage(content="Your tool budget for this pass is used up. Submit your ReviewReport "
                                         "now, using only the evidence already in this conversation -- anything "
                                         "you could not verify goes in as action='escalate'.")
            request = request.override(tools=[], messages=list(request.messages) + [nudge])
        return handler(request)


def _qwen_middleware(model_calls: int) -> list:
    return [
        _FinishWithReport(model_calls),
        # Backstop only: reached if the forced ReviewReport call itself fails
        # schema validation twice (ToolStrategy re-prompts on a bad payload).
        ModelCallLimitMiddleware(run_limit=model_calls + 2, exit_behavior="end"),
        ContextEditingMiddleware(edits=[ClearToolUsesEdit(
            trigger=QWEN_TOOL_CLEAR_TRIGGER_APPROX, keep=QWEN_TOOL_RESULTS_KEEP,
            placeholder="[cleared -- this older tool result was removed to stay within the model's "
                        "context window; call the tool again if you still need it]")]),
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
    model actually emitted), and until this existed every failure discarded
    them. Same local-only JSON idiom as trace.py; never transmitted."""
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

    qwen_max_tokens, middleware, tool_calls = None, [], DEFAULT_TOOL_CALLS_PER_PASS
    if provider == "qwen":
        # Counted before the budget section is appended -- it's ~80 tokens, well
        # inside the slack the per-round-trip allowance carries.
        qwen_max_tokens, model_calls = _qwen_budget(user_msg, tools)
        middleware = _qwen_middleware(model_calls)
        tool_calls = model_calls - 1  # the last call is reserved for ReviewReport
    user_msg += "\n\n" + _tool_budget_section(tool_calls)
    model = build_review_model(qwen_max_tokens=qwen_max_tokens)
    if model is None:
        raise RuntimeError("No usable LLM key configured (GEMINI_API_KEY/OPENAI_API_KEY) -- the review "
                            "graph must not be invoked without one; see vdd/pipeline.py's caller.")
    agent = create_agent(model=model, tools=tools, system_prompt=_SYSTEM_PROMPT, response_format=ReviewReport,
                         middleware=middleware)

    # Streamed rather than invoke()d so the messages accumulated so far survive
    # an exception mid-pass (e.g. the server rejecting a later turn) -- invoke()
    # would raise with nothing to show for what the loop had done.
    result: dict = {}
    try:
        for result in agent.stream({"messages": [{"role": "user", "content": user_msg}]}, stream_mode="values"):
            pass
    except Exception as e:
        _dump_failed_pass(state, result.get("messages", []), f"{type(e).__name__}: {e}")
        raise
    report: ReviewReport | None = result.get("structured_response")

    if report is None:
        # --- diagnostic logging: capture WHY the structured response was None ---
        messages = result.get("messages", [])
        _dump_failed_pass(state, messages, "agent finished without a structured ReviewReport")
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
