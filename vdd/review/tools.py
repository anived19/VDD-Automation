"""
Tools exposed to the reviewer agent.

Every tool here operates on names/IDs/text already extracted by the
deterministic pipeline -- NEVER on raw document bytes/images. This
pipeline's source documents are never sent to any LLM, full stop (see
vdd/extract/ocr.py, which has no vision fallback for the same reason).

Plain typed functions, no @tool decorator -- passed straight into
langchain.agents.create_agent(tools=[...]), same convention as the sibling
lanngraph-creditreport project (create_agent infers each tool's schema
from its signature + docstring).
"""
from __future__ import annotations

import os
from typing import Any, Optional

from vdd.aml.india_legal import screen_drt_sarfaesi, screen_pep
from vdd.aml.screening import Finding, run_sanctions_sweep
from vdd.finoscale_api.client import FinoscaleAPIError, FinoscaleClient


def _finding_to_dict(f: Finding) -> dict[str, Any]:
    severity = f.severity.value if hasattr(f.severity, "value") else str(f.severity)
    return {"entity_screened": f.entity_screened, "source_name": f.source_name,
            "finding_summary": f.finding_summary, "severity": severity, "source_url": f.source_url}


def make_tools(client: Optional[FinoscaleClient]) -> list:
    """Builds the reviewer's tool list. `client` may be None (a --no-api
    run) -- the two live-refetch tools then report unavailable instead of
    raising, same degrade-gracefully idiom used everywhere else in this
    pipeline."""

    def recheck_sanctions(entity_name: str) -> list[dict]:
        """Re-run the OFAC / UN Security Council / EU Financial Sanctions
        File / World Bank debarment sweep for a name -- the entity itself,
        or a partner/director not covered by the original run. Use this if
        you suspect the original screen missed a name variant, or want
        independent confirmation of a clean result."""
        return [_finding_to_dict(f) for f in run_sanctions_sweep(entity_name)]

    def recheck_pep(person_names: list[str]) -> dict:
        """Re-run the PEP (Politically Exposed Person) screen -- MyNeta/ECI
        + Wikidata -- for one or more person names."""
        return _finding_to_dict(screen_pep(person_names))

    def recheck_drt_sarfaesi(names: list[str]) -> dict:
        """Re-run the DRT/SARFAESI search (all 44 Indian Debt Recovery
        Tribunals) for one or more names."""
        return _finding_to_dict(screen_drt_sarfaesi(names))

    def recheck_gstin_live(gstin: str) -> dict:
        """Re-fetch this GSTIN's live status directly from Ongrid, bypassing
        the disk cache the original run used. Use this if you suspect the
        cached result is stale, or want independent confirmation of a GST
        registration status/vintage/filing claim in the report."""
        if client is None:
            return {"error": "no Finoscale API client configured for this run"}
        try:
            return client.ongrid_gstin_fetch_detailed(gstin)
        except FinoscaleAPIError as e:
            return {"error": f"[{e.status_code}] {e.message}"}

    def recheck_bank_verification(account_number: str, ifsc: str) -> dict:
        """Re-run the live Ongrid bank-verification penny drop for an
        account number + IFSC. Use this if the report's bank-verification
        result looks suspicious, or if you found a different account
        number worth checking (e.g. a cross-source mismatch)."""
        if client is None:
            return {"error": "no Finoscale API client configured for this run"}
        try:
            return client.ongrid_bank_verification_verify(account_number, ifsc)
        except FinoscaleAPIError as e:
            return {"error": f"[{e.status_code}] {e.message}"}

    def web_search(query: str) -> list[dict]:
        """Open-ended web search for anything not covered by the tools
        above (e.g. corroborating a business address or claim). Backed by
        Tavily today; kept as a stable interface so a future swap to
        OpenAI's hosted web-search tool (once this project's underlying LLM
        moves to OpenAI in production) changes only this function's
        implementation, not the reviewer's tool-calling contract."""
        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            return [{"error": "no TAVILY_API_KEY configured for this run"}]
        from tavily import TavilyClient
        response = TavilyClient(api_key=api_key).search(
            query=query, search_depth="basic", max_results=5, include_answer=False)
        return [{"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")}
                for r in response.get("results", [])]

    return [recheck_sanctions, recheck_pep, recheck_drt_sarfaesi, recheck_gstin_live,
            recheck_bank_verification, web_search]
