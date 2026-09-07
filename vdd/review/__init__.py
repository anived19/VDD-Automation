"""
LLM review loop for the deterministic VDD report (see graph.py).

Import-time guard: this pipeline processes consented but highly sensitive
personal financial/KYC data, and must never transmit run data to any
tracing/observability SaaS. LangSmith tracing is opt-in via these env
vars -- assert they're not set rather than trusting they default off,
since a stray env var on a shared machine/CI would otherwise silently
start tracing every future run without anyone deciding that here.
"""
import os

for _var in ("LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING"):
    if os.environ.get(_var, "false").strip().lower() == "true":
        raise RuntimeError(
            f"{_var} is set to true, but this pipeline must never trace to LangSmith or any "
            f"third-party observability service -- it processes consented but highly sensitive "
            f"personal financial/KYC data. Unset {_var} before running."
        )
