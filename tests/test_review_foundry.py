"""The Foundry provider (an open-weight model hosted by Azure) and the two
guards that come with it: web search off by default, and a bounded review
loop that always ends in a ReviewReport or a written transcript.

Background: a small self-hosted model (QwenTest branch, 2026-09-16) spent
every allowed model call on recheck tools and never called ReviewReport,
and separately put a PAN and a premises address into web_search queries.
These tests drive the real graph with fake chat models -- no endpoint or
key needed.
"""
from __future__ import annotations

import json
from typing import Any, Iterator

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from vdd.review import graph
from vdd.review.model import build_review_model, select_provider
from vdd.review.tools import make_tools, web_search_enabled

_ALL_PROVIDER_VARS = ("LLM_PROVIDER", "GEMINI_API_KEY", "OPENAI_API_KEY", "OPENAI_MODEL",
                      "FOUNDRY_ENDPOINT", "FOUNDRY_API_KEY", "FOUNDRY_DEPLOYMENT", "FOUNDRY_API_VERSION",
                      "FOUNDRY_EXTRA_BODY", "REVIEW_WEB_SEARCH")


@pytest.fixture
def clean_env(monkeypatch):
    for v in _ALL_PROVIDER_VARS:
        monkeypatch.delenv(v, raising=False)


# ---------------------------------------------------------------- provider selection

def test_foundry_needs_both_endpoint_and_key(clean_env, monkeypatch):
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://r.services.ai.azure.com/openai/v1")
    assert select_provider() is None
    monkeypatch.setenv("FOUNDRY_API_KEY", "k")
    assert select_provider() == "foundry"


def test_configured_foundry_outranks_leftover_api_keys(clean_env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    assert select_provider() == "gemini"
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://r.services.ai.azure.com/openai/v1")
    monkeypatch.setenv("FOUNDRY_API_KEY", "k")
    assert select_provider() == "foundry"
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    assert select_provider() == "gemini"


def test_explicit_foundry_without_config_returns_none(clean_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "foundry")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert select_provider() is None


# ---------------------------------------------------------------- model construction

def test_foundry_model_sends_azure_api_key_header_and_extra_body(clean_env, monkeypatch):
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://r.services.ai.azure.com/openai/v1/")
    monkeypatch.setenv("FOUNDRY_API_KEY", "secret")
    monkeypatch.setenv("FOUNDRY_DEPLOYMENT", "DeepSeek-V4-Pro")
    monkeypatch.setenv("FOUNDRY_EXTRA_BODY", json.dumps({"thinking": {"type": "enabled"}}))
    monkeypatch.setenv("FOUNDRY_API_VERSION", "2025-04-01-preview")
    m = build_review_model()
    assert m.model_name == "DeepSeek-V4-Pro"
    assert str(m.openai_api_base).rstrip("/") == "https://r.services.ai.azure.com/openai/v1"
    assert m.default_headers == {"api-key": "secret"}
    assert m.default_query == {"api-version": "2025-04-01-preview"}
    assert m.extra_body == {"thinking": {"type": "enabled"}}
    assert m.max_tokens == 8192


def test_foundry_model_requires_deployment_name(clean_env, monkeypatch):
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://r.services.ai.azure.com/openai/v1")
    monkeypatch.setenv("FOUNDRY_API_KEY", "secret")
    with pytest.raises(ValueError, match="FOUNDRY_DEPLOYMENT"):
        build_review_model()


# ---------------------------------------------------------------- web search gating

def test_web_search_is_not_offered_unless_enabled(clean_env, monkeypatch):
    assert not web_search_enabled()
    assert "web_search" not in [t.__name__ for t in make_tools(None)]
    monkeypatch.setenv("REVIEW_WEB_SEARCH", "1")
    assert "web_search" in [t.__name__ for t in make_tools(None)]


# ---------------------------------------------------------------- bounded loop

class _ToolHappyModel(GenericFakeChatModel):
    """Calls a recheck tool on every turn (repeating the same call) and only
    calls ReviewReport when that is the sole tool it's offered."""

    def __init__(self):
        self._offered: list[str] = []

        def gen() -> Iterator[AIMessage]:
            i = 0
            while True:
                i += 1
                if self._offered == ["ReviewReport"]:
                    yield AIMessage(content="", tool_calls=[{"name": "ReviewReport", "id": f"r{i}", "args": {
                        "findings": [{"issue": "forced finish", "proposed_note": "budget exhausted",
                                      "confidence": "unverified", "action": "escalate"}],
                        "verdict": "approved", "thoroughness_note": "forced"}}])
                else:
                    yield AIMessage(content="", tool_calls=[{"name": "recheck_pep", "id": f"c{i}",
                                                              "args": {"person_names": ["Nobody Real"]}}])
        super().__init__(messages=gen())

    def bind_tools(self, tools: Any, **kwargs: Any):
        self._offered = [getattr(t, "name", None) or t["function"]["name"] for t in tools]
        return self


class _NeverFinishesModel(_ToolHappyModel):
    def bind_tools(self, tools: Any, **kwargs: Any):
        self._offered = ["recheck_pep"]
        return self


def _minimal_context() -> dict:
    return {"firm": "T", "legal": "T", "date": "d", "chips": [], "score": 0, "com": 0, "poa": 0, "poi": 0,
            "aml": 0, "entity": [], "reg": [], "profile": "", "hsn": [], "findings": [],
            "com_rows": [], "poa_rows": [], "poi_rows": [], "aml_rows": [], "extra_unlock": [], "code_ids": {}}


def _state(tmp_path) -> dict:
    return {"vendor_name": "Test Vendor", "out_dir": str(tmp_path), "client": None, "iteration": 1,
            "entity": {}, "resolved": {}, "context": _minimal_context(), "cross_check_items": [], "passes": []}


@pytest.fixture
def foundry_env(clean_env, monkeypatch):
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://r.services.ai.azure.com/openai/v1")
    monkeypatch.setenv("FOUNDRY_API_KEY", "k")
    monkeypatch.setenv("FOUNDRY_DEPLOYMENT", "DeepSeek-V4-Pro")
    monkeypatch.setattr(graph, "FOUNDRY_MAX_MODEL_CALLS", 5)
    import vdd.review.tools as tools_mod
    monkeypatch.setattr(tools_mod, "screen_pep", lambda names: tools_mod.Finding(
        entity_screened=", ".join(names), source_name="fake", finding_summary="no match",
        severity="clean", source_url=None))


def test_tool_happy_model_is_forced_to_submit_a_report(foundry_env, monkeypatch, tmp_path):
    monkeypatch.setattr(graph, "build_review_model", lambda: _ToolHappyModel())
    out = graph.llm_review(_state(tmp_path))
    assert out["provider"] == "foundry"
    assert out["last_verdict"] == "approved"
    assert out["findings"][0]["issue"] == "forced finish"
    trace = out["message_traces"][0]
    tool_turns = [m for m in trace if m["type"] == "ai" and any(tc["name"] == "recheck_pep" for tc in m["tool_calls"])]
    assert len(tool_turns) == 4  # 5 calls allowed -> 4 tool rounds, 5th is ReviewReport-only
    repeats = [m for m in trace if m["type"] == "tool_result" and "repeated_call" in str(m["content"])]
    assert len(repeats) == 3
    assert "Tool budget for this pass" in trace[0]["content"]
    assert not list(tmp_path.glob("*_review_failure_*.json"))


def test_model_that_ignores_forced_finish_leaves_a_transcript(foundry_env, monkeypatch, tmp_path):
    monkeypatch.setattr(graph, "build_review_model", lambda: _NeverFinishesModel())
    with pytest.raises(RuntimeError, match="no usable structured ReviewReport"):
        graph.llm_review(_state(tmp_path))
    d = json.loads((tmp_path / "TEST_VENDOR_review_failure_pass1.json").read_text(encoding="utf-8"))
    ai_turns = [m for m in d["messages"] if m["type"] == "ai" and m["tool_calls"]]
    assert 1 <= len(ai_turns) <= 7  # 5 + 2 backstop, never unbounded
    assert d["provider"] == "foundry"


class _StubAgent:
    """Stands in for create_agent's return value: records the input it was
    streamed and yields one final state carrying a valid ReviewReport."""

    def __init__(self):
        from vdd.review.schemas import ReviewReport
        self.input = None
        self.final = {"messages": [], "structured_response": ReviewReport(
            findings=[], verdict="approved", thoroughness_note="stub")}

    def stream(self, input, stream_mode="values"):
        self.input = input
        yield self.final


@pytest.mark.parametrize("provider_env", [{"GEMINI_API_KEY": "g"}, {"OPENAI_API_KEY": "o", "OPENAI_MODEL": "m"}])
def test_api_providers_are_untouched_by_the_bounded_loop(clean_env, monkeypatch, tmp_path, provider_env):
    """Gemini/OpenAI converge on their own; they must not get the budget
    section or the forced finish (their validated behaviour is the baseline)."""
    for k, v in provider_env.items():
        monkeypatch.setenv(k, v)
    captured: dict = {}
    agent = _StubAgent()

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return agent

    monkeypatch.setattr(graph, "create_agent", fake_create_agent)
    monkeypatch.setattr(graph, "build_review_model", lambda: object())
    out = graph.llm_review(_state(tmp_path))
    assert out["provider"] in ("gemini", "openai")
    assert captured["middleware"] == []
    assert "Tool budget for this pass" not in agent.input["messages"][0]["content"]
