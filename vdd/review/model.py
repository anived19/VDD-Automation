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
    if explicit == "openai":
        return "openai" if os.environ.get("OPENAI_API_KEY") else None
    if explicit:
        raise ValueError(f"Unrecognized LLM_PROVIDER={explicit!r} -- expected 'gemini' or 'openai'")
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return None


def build_review_model() -> Optional[BaseChatModel]:
    """Returns None if no usable LLM key is configured."""
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
