"""
Provider-swappable chat model for the reviewer agent.

Three providers:
  - gemini  -- gemini-3.5-flash-lite via GEMINI_API_KEY (tested, the default
               when only that key is present).
  - openai  -- OPENAI_API_KEY + OPENAI_MODEL, Responses API with reasoning
               summaries (tested 2026-09-16, gpt-5.6-luna).
  - foundry -- an open-weight model (DeepSeek-V4-Pro, Kimi K3, ...) sold and
               hosted by Microsoft Foundry in the customer's own Azure
               subscription, reached through its OpenAI-compatible chat
               completions endpoint. This is the data-residency path: the
               model provider never sees a request, Microsoft does not train
               on or share prompts/completions, and with a regional / Data
               Zone deployment plus the modified abuse-monitoring exemption
               nothing is stored. See .env.example for the deployment
               checklist that makes those guarantees actually hold.

Uses LangChain's provider-agnostic `init_chat_model`, which standardizes
`api_key` as a constructor kwarg across every supported provider
(including google_genai and openai) specifically so callers don't need to
know each provider's own key-parameter name.
"""
from __future__ import annotations

import json
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


def _foundry_configured() -> bool:
    return bool(os.environ.get("FOUNDRY_ENDPOINT") and os.environ.get("FOUNDRY_API_KEY"))


def select_provider() -> Optional[str]:
    """Which provider to use, or None if no usable key is configured --
    callers (vdd/pipeline.py) must treat None as 'skip the review step',
    never fabricate a model or silently proceed without one."""
    explicit = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if explicit in ("gemini", "google", "google_genai"):
        return "gemini" if os.environ.get("GEMINI_API_KEY") else None
    if explicit == "openai":
        return "openai" if os.environ.get("OPENAI_API_KEY") else None
    if explicit in ("foundry", "azure"):
        return "foundry" if _foundry_configured() else None
    if explicit:
        raise ValueError(f"Unrecognized LLM_PROVIDER={explicit!r} -- expected 'gemini', 'openai', or 'foundry'")
    # A configured Foundry endpoint is a deliberate deployment decision (it
    # only exists if someone set the resource up), so it outranks API keys that
    # may simply still be sitting in .env from earlier testing.
    if _foundry_configured():
        return "foundry"
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return None


FOUNDRY_MAX_OUTPUT_TOKENS_DEFAULT = 8192


def foundry_max_output_tokens() -> int:
    """Per-turn output cap for the Foundry model. A reasoning model's thinking
    counts against it before the tool call / JSON starts, so it is deliberately
    higher than a plain model needs (real ReviewReports run ~1k tokens)."""
    return int(os.environ.get("FOUNDRY_MAX_OUTPUT_TOKENS", FOUNDRY_MAX_OUTPUT_TOKENS_DEFAULT))


def build_review_model() -> Optional[BaseChatModel]:
    """Returns None if no usable LLM key is configured."""
    provider = select_provider()
    if provider == "foundry":
        deployment = os.environ.get("FOUNDRY_DEPLOYMENT")
        if not deployment:
            raise ValueError("FOUNDRY_ENDPOINT/FOUNDRY_API_KEY are set but FOUNDRY_DEPLOYMENT is not -- set it to "
                              "the deployment name from the Foundry portal (e.g. DeepSeek-V4-Pro).")
        key = os.environ["FOUNDRY_API_KEY"]
        # Azure authenticates with an `api-key` header; the OpenAI client only
        # knows `Authorization: Bearer`. Sending both is harmless and covers the
        # /openai/v1 route (accepts either) and the Model Inference route
        # (api-key only). FOUNDRY_API_VERSION is only needed by the latter.
        kwargs: dict = {"api_key": key, "base_url": os.environ["FOUNDRY_ENDPOINT"].rstrip("/"),
                        "default_headers": {"api-key": key}, "max_tokens": foundry_max_output_tokens()}
        if os.environ.get("FOUNDRY_API_VERSION"):
            kwargs["default_query"] = {"api-version": os.environ["FOUNDRY_API_VERSION"]}
        # Model-specific request fields the OpenAI schema has no name for
        # (DeepSeek / Kimi thinking controls, etc.) -- taken verbatim from the
        # model card, never guessed here. JSON object, e.g. {"thinking": {"type": "enabled"}}.
        extra = os.environ.get("FOUNDRY_EXTRA_BODY", "").strip()
        if extra:
            kwargs["extra_body"] = json.loads(extra)
        return init_chat_model(deployment, model_provider="openai", **kwargs)
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
        # Reasoning summaries are the only view OpenAI gives of a reasoning
        # model's thinking (the raw chain-of-thought is never returned) and
        # they have to be asked for: `reasoning={"summary": "auto"}` routes the
        # call through the Responses API, and output_version="responses/v1"
        # puts them in message.content as {"type": "reasoning", "summary":
        # [...]} blocks -- which langchain-core's `content_blocks` normalises
        # to the same shape as Gemini's thinking blocks, so trace.py logs both
        # providers' thoughts through one code path. Effort defaults to the
        # model's own default ("medium"); "none" would switch thinking off.
        # Requires a verified OpenAI org -- otherwise the API answers 400
        # "Your organization must be verified to generate reasoning summaries".
        effort = os.environ.get("OPENAI_REASONING_EFFORT", "medium")
        return init_chat_model(model_name, model_provider="openai", api_key=os.environ["OPENAI_API_KEY"],
                                reasoning={"effort": effort, "summary": "auto"}, output_version="responses/v1")
    return None
