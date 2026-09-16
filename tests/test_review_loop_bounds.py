"""The self-hosted Qwen path must never let a review pass grow the context
unboundedly, and a pass that fails must leave its transcript behind.

Background (2026-09-16): a pass whose first call fit in 7k tokens was
rejected by vLLM at >=28.7k tokens several turns later, and the exception
discarded every message the loop had produced -- three days of debugging
blind. These tests drive graph.llm_review with a fake chat model that
misbehaves the way a small quantized model does, no server or key needed.
"""
from __future__ import annotations

import json
from typing import Any, Iterator

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from vdd.review import graph


class _ToolLoopingModel(GenericFakeChatModel):
    """Calls recheck_pep on every turn, forever -- never emits a ReviewReport."""

    def __init__(self):
        def gen() -> Iterator[AIMessage]:
            i = 0
            while True:
                i += 1
                yield AIMessage(content="", tool_calls=[{"name": "recheck_pep", "id": f"c{i}",
                                                          "args": {"person_names": ["Nobody Real"]}}])
        super().__init__(messages=gen())

    def bind_tools(self, tools: Any, **kwargs: Any):
        return self


class _CrashingModel(GenericFakeChatModel):
    """One tool call, then the server 'rejects' the next turn."""

    def __init__(self):
        def gen() -> Iterator[AIMessage]:
            yield AIMessage(content="", tool_calls=[{"name": "recheck_pep", "id": "c1",
                                                      "args": {"person_names": ["Nobody Real"]}}])
            raise RuntimeError("400 maximum context length is 32768 tokens (simulated)")
        super().__init__(messages=gen())

    def bind_tools(self, tools: Any, **kwargs: Any):
        return self


def _state(tmp_path) -> dict:
    return {"vendor_name": "Test Vendor", "out_dir": str(tmp_path), "client": None, "iteration": 1,
            "resolved": {}, "context": _minimal_context(), "cross_check_items": [], "passes": []}


def _minimal_context() -> dict:
    return {"firm": "T", "legal": "T", "date": "d", "chips": [], "score": 0, "com": 0, "poa": 0, "poi": 0,
            "aml": 0, "entity": [], "reg": [], "profile": "", "hsn": [], "findings": [],
            "com_rows": [], "poa_rows": [], "poi_rows": [], "aml_rows": [], "extra_unlock": []}


@pytest.fixture
def qwen_env(monkeypatch):
    monkeypatch.setattr(graph, "select_provider", lambda: "qwen")
    # No server in CI: pretend /tokenize said 7k prompt in a 32k window.
    monkeypatch.setattr(graph, "_qwen_count_tokens", lambda messages, tools: (7000, 32768))
    # Keep the fake tool offline -- recheck_pep would otherwise hit MyNeta/Wikidata.
    import vdd.review.tools as tools_mod
    monkeypatch.setattr(tools_mod, "screen_pep", lambda names: tools_mod.Finding(
        entity_screened=", ".join(names), source_name="fake", finding_summary="no match",
        severity="clean", source_url=None))


def test_budget_derives_model_call_limit_from_measured_headroom(qwen_env):
    max_tokens, calls = graph._qwen_budget("user msg", [])
    assert max_tokens == graph.QWEN_MAX_OUTPUT_TOKENS
    # (32768 - 7000 - 2048) // (2048 + 2000) == 5
    assert calls == 5


def test_tool_loop_is_cut_off_and_transcript_written(qwen_env, monkeypatch, tmp_path):
    monkeypatch.setattr(graph, "build_review_model", lambda **kw: _ToolLoopingModel())
    with pytest.raises(RuntimeError, match="no usable structured ReviewReport"):
        graph.llm_review(_state(tmp_path))
    dumps = list(tmp_path.glob("TEST_VENDOR_review_failure_pass1.json"))
    assert dumps, "failed pass must leave its transcript behind"
    d = json.loads(dumps[0].read_text(encoding="utf-8"))
    ai_turns = [m for m in d["messages"] if m["type"] == "ai" and m["tool_calls"]]
    # 5 allowed model calls -> at most 5 tool-calling turns, not an unbounded loop
    assert 1 <= len(ai_turns) <= 5
    assert d["provider"] == "qwen"


def test_midpass_exception_still_dumps_transcript(qwen_env, monkeypatch, tmp_path):
    monkeypatch.setattr(graph, "build_review_model", lambda **kw: _CrashingModel())
    with pytest.raises(RuntimeError, match="maximum context length"):
        graph.llm_review(_state(tmp_path))
    d = json.loads((tmp_path / "TEST_VENDOR_review_failure_pass1.json").read_text(encoding="utf-8"))
    types = [m["type"] for m in d["messages"]]
    assert "human" in types and "ai" in types and "tool_result" in types
    assert "simulated" in d["error"]
