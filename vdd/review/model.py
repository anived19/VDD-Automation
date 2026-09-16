"""
Provider-swappable chat model for the reviewer agent.

Gemini (gemini-3.5-flash-lite via GEMINI_API_KEY) is the only tested path
right now. The OpenAI branch exists so a future move to OpenAI in
production is a key/env change, not a rewrite -- per the user's own plan
("make it swappable depending on which key is active") -- but it is
UNTESTED: no OpenAI key was available while building this, so treat it as
unverified until it's actually run against a real key.

Uses LangChain's provider-agnostic `init_chat_model`, which standardizes
`api_key` as a constructor kwarg across every supported provider
(including google_genai and openai) specifically so callers don't need to
know each provider's own key-parameter name.
"""
from __future__ import annotations

import os
from typing import Optional

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.rate_limiters import InMemoryRateLimiter

# gemini-3.5-flash-lite's free-tier RPM is tight enough to need pacing across
# a whole run (multiple review passes, each with several tool-calling
# turns) -- same InMemoryRateLimiter pattern as the sibling
# lanngraph-creditreport project's credit_report/model.py. Not applied to
# OpenAI -- no comparable free-tier constraint at the volumes this pipeline
# generates, and the OpenAI path is untested regardless.
_GEMINI_RATE_LIMITER = InMemoryRateLimiter(requests_per_second=12 / 60, check_every_n_seconds=0.1, max_bucket_size=1)


def select_provider() -> Optional[str]:
    """Which provider to use, or None if no usable key is configured --
    callers (vdd/pipeline.py) must treat None as 'skip the review step',
    never fabricate a model or silently proceed without one."""
    explicit = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if explicit in ("gemini", "google", "google_genai"):
        return "gemini" if os.environ.get("GEMINI_API_KEY") else None
    if explicit in ("qwen", "local", "vllm"):
        return "qwen" if os.environ.get("QWEN_BASE_URL") else None
    if explicit == "openai":
        return "openai" if os.environ.get("OPENAI_API_KEY") else None
    if explicit:
        raise ValueError(f"Unrecognized LLM_PROVIDER={explicit!r} -- expected 'gemini', 'qwen', or 'openai'")
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if os.environ.get("QWEN_BASE_URL"):
        return "qwen"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return None


def qwen_thinking_enabled() -> bool:
    """QWEN_THINKING=1 turns Qwen3.8's <think> reasoning on for the reviewer.
    Off by default: it multiplies GPU time per turn (every reasoning token is
    decoded at T4 speed) but costs nothing in context -- langchain_openai never
    sends reasoning back on later turns, and vLLM's --reasoning-parser keeps it
    out of `content`. It also can't be read back: langchain_openai 1.6 drops
    the `reasoning` field in chat-completions mode, so the trace records only
    the reasoning TOKEN COUNT per call (trace.py), not the text."""
    return os.environ.get("QWEN_THINKING", "").strip().lower() in ("1", "true", "yes", "on")


QWEN_THINKING_MAX_TOKENS_DEFAULT = 8192


def qwen_thinking_max_tokens() -> int:
    """Per-turn cap while thinking (reasoning + tool call / JSON together).
    QWEN_THINKING_MAX_TOKENS overrides; 0 means UNCAPPED -- max_tokens is not
    sent and vLLM lets the turn run to whatever is left of the window. Safe
    against context-length rejections (vLLM sizes it per request) but a
    runaway think on T4s can take an hour, so the default stays finite.
    For scale: Gemini with thinking_level="high" spends ~0.6-1.3k output
    tokens per turn on this task (out/*_review_trace.json totals / calls)."""
    raw = os.environ.get("QWEN_THINKING_MAX_TOKENS", "").strip()
    return int(raw) if raw else QWEN_THINKING_MAX_TOKENS_DEFAULT


def build_review_model(*, qwen_max_tokens: Optional[int] = None) -> Optional[BaseChatModel]:
    """Returns None if no usable LLM key is configured.

    `qwen_max_tokens` overrides the qwen branch's output-token cap -- see
    vdd/review/graph.py::llm_review, which sizes it against that call's
    actual prompt length before invoking this. 0 means send no cap at all
    (vLLM then allows the rest of the window). Ignored for every other
    provider."""
    provider = select_provider()
    if provider == "gemini":
        model_name = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
        # include_thoughts=True surfaces Gemini's reasoning summary in the response
        # content (as "thinking"-type blocks) instead of just the internal, invisible
        # thinking Gemini 3+ models already do by default -- vdd/review/trace.py's
        # serializer picks these up and writes them into the review trace file.
        # thinking_level="high" is also required, verified empirically (2026-09-07):
        # with include_thoughts=True alone, gemini-3.5-flash-lite still answered with
        # a plain "text" block (no distinct "thinking" block) -- the general Gemini 3+
        # docs claim thinking_level defaults to "high", but that did not hold for this
        # lite model in practice. Setting it explicitly reliably produced a real
        # "thinking" block with actual chain-of-thought content.
        return init_chat_model(model_name, model_provider="google_genai",
                                api_key=os.environ["GEMINI_API_KEY"], rate_limiter=_GEMINI_RATE_LIMITER,
                                include_thoughts=True, thinking_level="high")
    if provider == "qwen":
        # Self-hosted Qwen3.8-27B behind vLLM's OpenAI-compatible server
        # (see .env.example). QWEN_MODEL must match exactly what was passed
        # to `vllm serve` (or its --served-model-name alias) -- vLLM
        # validates the model field against that, not against any real
        # model registry.
        model_name = os.environ.get("QWEN_MODEL", "Qwen/Qwen3.8-27B")
        # Qwen3.8's <think>…</think> tokens count against max_tokens BEFORE the
        # JSON starts, so the per-turn cap depends on whether thinking is on (see
        # qwen_thinking_enabled). The caller (graph.py::_qwen_budget) owns the cap
        # because it is a per-turn cap that also bounds mid-pass context growth;
        # the server rejects prompt_tokens + max_tokens > --max-model-len on every
        # turn. The defaults here are only for callers that don't pass one.
        thinking = qwen_thinking_enabled()
        if qwen_max_tokens is None:
            qwen_max_tokens = qwen_thinking_max_tokens() if thinking else 2048
        return init_chat_model(model_name, model_provider="openai",
                                api_key=os.environ.get("QWEN_API_KEY", "EMPTY"),
                                base_url=os.environ["QWEN_BASE_URL"],
                                max_tokens=qwen_max_tokens or None,  # 0 -> omit, vLLM fills the window
                                extra_body={"chat_template_kwargs": {"enable_thinking": thinking}})
    if provider == "openai":
        # No guessed default model name here (this codebase's own convention
        # is "never guess, never fabricate") -- OPENAI_MODEL must be set
        # explicitly once this path is actually adopted.
        model_name = os.environ.get("OPENAI_MODEL")
        if not model_name:
            raise ValueError("OPENAI_API_KEY is set but OPENAI_MODEL is not -- set it explicitly "
                              "(this path is untested; pick a current model deliberately, don't guess).")
        return init_chat_model(model_name, model_provider="openai", api_key=os.environ["OPENAI_API_KEY"])
    return None
