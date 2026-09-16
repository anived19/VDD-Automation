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

Every result is appended verbatim to the agent's context for the rest of
the pass, so tools return the *answer*, not the raw API payload. Measured
with the exact Qwen3.8 tokenizer on real traces: a raw Zigram response was
245k chars / ~64k tokens (2x the whole 32k window -- one call guaranteed a
400 on the next model turn), a raw Ongrid fetch-detailed 11.5k chars /
~4.5k tokens (83% of it the full filing_data history). `_cap` is the
backstop so no single result can blow the window regardless of provider.
"""
from __future__ import annotations

import functools
import json
import os
from typing import Any, Optional

from vdd.aml.india_legal import screen_drt_sarfaesi, screen_pep
from vdd.aml.screening import Finding, run_sanctions_sweep
from vdd.aml.zigram_screening import summarize_screen
from vdd.finoscale_api.client import FinoscaleAPIError, FinoscaleClient
from vdd.resolve.resolvers import _filing_delays

# ~2k tokens. Big enough for every compacted result below; small enough that a
# 5-tool pass adds ~10k tokens to the context, not the ~64k one raw Zigram
# response used to.
_MAX_TOOL_RESULT_CHARS = 6000
_WEB_SNIPPET_CHARS = 700


def _cap(fn, seen: dict):
    """Two guards on every tool result, since each one is appended to the
    agent's context for the rest of the pass:
      - an identical repeat (same tool, same arguments -- observed 2026-09-16:
        Qwen3.8 called recheck_gstin_live twice back to back) returns a short
        pointer to the earlier result instead of the full payload again;
      - an oversized result is truncated rather than allowed to eat the window.
    `seen` is per make_tools() call, i.e. per pass. functools.wraps keeps
    __name__/__doc__/__annotations__ and sets __wrapped__, which
    inspect.signature follows -- create_agent still infers the exact same tool
    schema from the original function."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        key = (fn.__name__, json.dumps([args, kwargs], sort_keys=True, default=str))
        if key in seen:
            return {"repeated_call": True,
                    "note": f"You already called {fn.__name__} with exactly these arguments this pass (call "
                            f"#{seen[key]}); the result has not changed -- use that result, do not call again."}
        seen[key] = len(seen) + 1
        result = fn(*args, **kwargs)
        s = json.dumps(result, default=str)
        if len(s) <= _MAX_TOOL_RESULT_CHARS:
            return result
        return {"truncated": True, "total_chars": len(s),
                "note": f"result exceeded the reviewer's {_MAX_TOOL_RESULT_CHARS}-char per-tool budget; "
                        "this is the leading portion only -- treat anything you'd need from the rest as "
                        "unverified and escalate rather than guess",
                "preview": s[:_MAX_TOOL_RESULT_CHARS]}
    return wrapper


def _finding_to_dict(f: Finding) -> dict[str, Any]:
    severity = f.severity.value if hasattr(f.severity, "value") else str(f.severity)
    return {"entity_screened": f.entity_screened, "source_name": f.source_name,
            "finding_summary": f.finding_summary, "severity": severity, "source_url": f.source_url}


def _find_key(obj: Any, key: str) -> Any:
    """First value for `key` anywhere in a nested dict/list, or None."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_key(v, key)
            if found is not None:
                return found
    return None


def _compact_gstin(raw: Any) -> dict:
    """Everything the reviewer can act on from Ongrid fetch-detailed, minus the
    ~140-row filing_data table, which is replaced by the same per-return-type
    delay summary the resolvers score from (resolvers._filing_delays, last 12
    months, GSTR3B due 20th / GSTR1 due 11th) plus the recent rows themselves."""
    g = _find_key(raw, "gstin_data")
    if not isinstance(g, dict):
        return {"error": "response carried no gstin_data", "raw_keys": list(raw)[:10] if isinstance(raw, dict) else str(type(raw))}
    out = {k: v for k, v in g.items() if not isinstance(v, (dict, list))}
    for k in ("directors", "principal_address", "filing_frequency"):
        if g.get(k) is not None:
            out[k] = g[k]
    hsn = g.get("hsn_data") or {}
    codes = [(h.get("hsn") or h.get("sac"), (h.get("description") or "")[:80])
             for h in list(hsn.get("goods") or []) + list(hsn.get("services") or []) if isinstance(h, dict)]
    out["hsn_codes"] = codes[:15] + ([f"... {len(codes) - 15} more"] if len(codes) > 15 else [])
    filing = g.get("filing_data") or []
    summary = {}
    for rt in ("GSTR3B", "GSTR1"):
        delays = _filing_delays(filing, rt)
        summary[rt] = {"periods_last_12m": len(delays), "late": sum(1 for d in delays if d > 0),
                       "max_delay_days": max(delays) if delays else None}
    recent = [{"type": r.get("return_type"), "fy": r.get("financial_year"), "period": r.get("tax_period"),
               "filed": r.get("date_of_filing"), "status": r.get("status")}
              for r in filing if r.get("return_type") in ("GSTR3B", "GSTR1")]
    out["filing_summary_last_12m"] = summary
    out["filing_rows_total"] = len(filing)
    out["recent_gstr3b_gstr1_rows"] = recent[:26]
    return out


def make_tools(client: Optional[FinoscaleClient]) -> list:
    """Builds the reviewer's tool list. `client` may be None (a --no-api
    run) -- the live-refetch tools then report unavailable instead of
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
        """Re-fetch this GSTIN's live record from Ongrid. Use this to
        independently confirm a GST registration status / vintage / legal
        name / directors / filing-timeliness claim in the report. Returns the
        registration fields, and for filings a per-return-type summary over
        the last 12 months (periods, late count, max delay in days -- the
        same due-date rule the report was scored with) plus the most recent
        GSTR3B/GSTR1 rows, not the full multi-year filing history."""
        if client is None:
            return {"error": "no Finoscale API client configured for this run"}
        try:
            return _compact_gstin(client.ongrid_gstin_fetch_detailed(gstin))
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

    def recheck_zigram_screening(entity_name: str, type_: str = "Organization",
                                  pan: str = None, cin: str = None, llpin: str = None) -> dict:
        """Re-run Zigram's watchlist/sanctions/PEP/adverse-media screening --
        this pipeline's PRIMARY AML sweep (~60 list categories: India-specific
        registries such as ESIC defaulters, OFAC-style sanctions, PEP, courts,
        and more). Requires a real identifier -- `pan`, `cin`, or `llpin` --
        for the entity/person screened; a name-only call is untested and is
        refused, so use `recheck_pep`/`recheck_sanctions`/
        `recheck_drt_sarfaesi` for a name-only screen. `type_="Individual"`
        for a partner/director screened by their own PAN; `"Organization"`
        (default) for the firm itself.

        Returns only real matches: `hits` maps each of this report's AML
        parameter ids (or 'other' for a genuine finding outside those 5
        slots) to citable summaries -- list matched, fuzzy score, match
        status, source link. `comprehensive=False` means Zigram returned a
        hollow stub and the result proves nothing either way."""
        if client is None:
            return {"error": "no Finoscale API client configured for this run"}
        identifier = cin or pan or llpin
        if not identifier:
            return {"error": "no identifier (pan/cin/llpin) provided -- a name-only Zigram call is "
                              "untested; use recheck_pep/recheck_sanctions/recheck_drt_sarfaesi instead "
                              "for a name-only screen"}
        try:
            raw = client.zigram_screening(entity_name=entity_name, client_id=identifier,
                                           type_=type_, country=["India"], cin=cin, pan=pan, llpin=llpin)
        except FinoscaleAPIError as e:
            return {"error": f"[{e.status_code}] {e.message}"}
        summary = summarize_screen(raw, entity_name)
        ec = raw.get("entitychecks") if isinstance(raw, dict) else None
        block = ec[0] if isinstance(ec, list) and ec and isinstance(ec[0], dict) else {}
        return {"comprehensive": summary["_comprehensive"], "error": summary["_error"],
                "categories_screened": sum(1 for v in block.values() if isinstance(v, list)),
                "hits": {k: v for k, v in summary.items() if not k.startswith("_")}}

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
        return [{"title": r.get("title", ""), "url": r.get("url", ""),
                 "content": (r.get("content") or "")[:_WEB_SNIPPET_CHARS]}
                for r in response.get("results", [])]

    seen: dict = {}
    return [_cap(t, seen) for t in (recheck_sanctions, recheck_pep, recheck_drt_sarfaesi, recheck_gstin_live,
                                    recheck_bank_verification, recheck_zigram_screening, web_search)]
