"""
Local-only JSON audit trace for the review loop.

Never transmitted anywhere -- this is the audit mechanism this project
uses INSTEAD OF LangSmith/LangChain tracing, which must stay off (see
vdd/review/__init__.py's import-time guard). Captures both the reviewer's
final structured findings per pass (`passes`) AND the raw message-by-message
ReAct history (`serialize_messages`/`message_traces`) -- the actual tool
calls made and their real results, not just the model's own claims about
what it checked. Matches the same "never trust the LLM's own account,
verify against the raw data" principle used throughout this codebase.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Optional


def _serialize_message(m: Any) -> dict[str, Any]:
    cls = m.__class__.__name__
    if cls == "AIMessage":
        tool_calls = getattr(m, "tool_calls", None) or []
        content = m.content
        text, thinking = None, None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # Gemini (include_thoughts=True) returns content as a list of blocks --
            # {"type": "thinking", "thinking": "..."} for the reasoning summary,
            # {"type": "text", "text": "..."} for the actual reply -- instead of a
            # plain string. Split them out rather than dropping the whole thing.
            text_parts, thinking_parts = [], []
            for block in content:
                if isinstance(block, str):
                    text_parts.append(block)
                elif isinstance(block, dict):
                    if block.get("type") == "thinking":
                        thinking_parts.append(block.get("thinking") or "")
                    elif block.get("type") == "text":
                        text_parts.append(block.get("text") or "")
            text = "\n".join(p for p in text_parts if p) or None
            thinking = "\n".join(p for p in thinking_parts if p) or None
        return {"type": "ai", "content": text, "thinking": thinking,
                "tool_calls": [{"name": tc.get("name"), "args": tc.get("args")} for tc in tool_calls]}
    if cls == "ToolMessage":
        content = m.content
        return {"type": "tool_result", "tool_name": getattr(m, "name", None),
                "content": content if isinstance(content, (str, int, float, bool, type(None))) else str(content)}
    if cls == "HumanMessage":
        return {"type": "human", "content": str(m.content)}
    if cls == "SystemMessage":
        return {"type": "system", "content": str(m.content)}
    return {"type": cls.lower(), "content": str(getattr(m, "content", ""))}


def serialize_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """Convert one llm_review call's raw agent.invoke()['messages'] (the
    full internal tool-calling loop for that pass) into a JSON-safe trace."""
    return [_serialize_message(m) for m in messages]


def _usage_from_message(m: Any) -> Optional[dict[str, int]]:
    if m.__class__.__name__ != "AIMessage":
        return None
    u = getattr(m, "usage_metadata", None)
    if not u:
        return None
    return {"input_tokens": int(u.get("input_tokens") or 0),
            "output_tokens": int(u.get("output_tokens") or 0),
            "total_tokens": int(u.get("total_tokens") or 0)}


def extract_pass_usage(messages: list[Any]) -> dict[str, int]:
    """Sum token usage across every LLM call made during one llm_review pass --
    a ReAct pass invokes the model once per tool-calling round, not just once,
    so this sums usage_metadata across every AIMessage in that pass."""
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "llm_call_count": 0}
    for m in messages:
        u = _usage_from_message(m)
        if u is None:
            continue
        totals["input_tokens"] += u["input_tokens"]
        totals["output_tokens"] += u["output_tokens"]
        totals["total_tokens"] += u["total_tokens"]
        totals["llm_call_count"] += 1
    return totals


def summarize_usage(pass_usage: list[dict[str, int]]) -> dict[str, int]:
    """Grand total across every pass of one review run."""
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "llm_call_count": 0}
    for p in pass_usage:
        for k in totals:
            totals[k] += p.get(k, 0)
    return totals


def write_review_trace(vendor_name: str, out_dir: str, final_state: dict[str, Any]) -> str:
    pass_usage = final_state.get("pass_usage", [])
    payload = {
        "vendor_name": vendor_name,
        "generated_at": datetime.now().isoformat(),
        "provider": final_state.get("provider"),
        "iterations_run": max(0, final_state.get("iteration", 1) - 1),
        "approved": final_state.get("approved", False),
        "passes": final_state.get("passes", []),
        "message_traces": final_state.get("message_traces", []),
        "corrections_applied": final_state.get("corrections_applied", []),
        "escalations": final_state.get("escalations", []),
        "token_usage": {"total": summarize_usage(pass_usage), "per_pass": pass_usage},
    }
    os.makedirs(out_dir, exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in vendor_name).strip("_").upper() or "VENDOR"
    path = os.path.join(out_dir, f"{safe}_review_trace.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    return path
