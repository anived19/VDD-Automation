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
from typing import Any


def _serialize_message(m: Any) -> dict[str, Any]:
    cls = m.__class__.__name__
    if cls == "AIMessage":
        tool_calls = getattr(m, "tool_calls", None) or []
        content = m.content
        text = content if isinstance(content, str) else None
        return {"type": "ai", "content": text,
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


def write_review_trace(vendor_name: str, out_dir: str, final_state: dict[str, Any]) -> str:
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
    }
    os.makedirs(out_dir, exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in vendor_name).strip("_").upper() or "VENDOR"
    path = os.path.join(out_dir, f"{safe}_review_trace.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    return path
