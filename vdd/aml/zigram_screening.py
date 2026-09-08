"""
Zigram screening -- the PRIMARY AML sweep as of 2026-09-08, superseding the
free OFAC/UN/EU/World Bank + MyNeta/Wikidata + drt.gov.in sweep as this
pipeline's main source of truth for the legal_* parameters. Those free
sources are NOT removed -- they now also run as Gemini review tools for
independent corroboration (see vdd/review/tools.py), and remain the
fallback path here whenever Zigram doesn't come through on a given run.

RELIABILITY -- CORRECTED FINDING, 2026-09-08 (read this before trusting
any earlier claim about "the clientId/country format is the fix"): three
live, non-cached calls for Skandan Plastrix (PAN ABMCS1968D / CIN
U24311TZ2023PTC030021) this session ALL returned the full ~63-category
response with the same real hit (see below) -- including the FIRST call,
which used an arbitrary placeholder `clientId` and `country: ["IN"]` (the
supposedly "broken" format). That call was originally misdiagnosed as a
hollow stub; the mistake was in the diagnostic script, not the API
response -- it printed `len(entitychecks)` (always 1, since entitychecks
is a list containing exactly ONE dict whose keys ARE the ~60-63 watchlist
categories) and a truncated 800-character preview that happened to cut off
right after the first category ("Angola Watchlists"), which is present on
every response regardless of comprehensiveness. Re-reading that same
saved response with the corrected category-count check (`is_comprehensive`
below) shows 63 categories and the same real hit as the other two calls.
**This means the clientId/country hypothesis is NOT confirmed** -- it may
be that `pan=`/`cin=` being present and correct is what actually matters
(all three of today's calls had one), or that Zigram's reliability has
simply improved since the original ~3%-hit-rate finding (2026-09-03,
predates this session, raw responses not available to re-check with the
corrected method). Both are plausible; neither is proven. What IS solid,
from real non-cached data today: 3 for 3 comprehensive responses,
including a genuine, previously-invisible finding -- an ESIC (Employees'
State Insurance Corporation) Defaulters List match, 100% fuzzy score,
sourced from esic.gov.in's own published PDF, found identically in all
three calls. Going forward, `is_comprehensive()`/`summarize_screen()`
below are the CORRECT way to tell a real response from a hollow one
(category count + `HitsFound`, never `Subscribed`, never a truncated
preview) -- use them, don't hand-inspect a raw response again.

SCOPE, and what's still genuinely uncertain -- be honest about this in
every resolver that uses this module, don't silently overclaim:
  - CONFIRMED reliable (3 for 3 today): Organization-type screening for
    the firm itself, with a real `pan=` or `cin=` in the request. Whether
    `clientId` specifically needs to equal that identifier is NOT
    confirmed (see RELIABILITY note above) -- this module still sets it
    that way defensively (plausible, costs nothing) but that detail is not
    what's been proven to matter.
  - NOT tested at all: Individual-type screening for partners/directors
    who often have no identifier on file (only a name from GST
    registration data or Probe42). Partners without an identifier stay
    primarily on the existing free MyNeta/Wikidata sweep
    (india_legal.py::screen_pep), which needs no identifier at all. If the
    owner's own PAN was extracted from a pan_owner document, this module
    also attempts an Individual-type Zigram screen for them specifically,
    as a bonus corroborating check -- this specific path is untested, not
    load-bearing.
  - The exact taxonomy of what "Indian Watchlists" (a broad, multi-source
    category) actually contains is only confirmed for ONE real example so
    far (the ESIC list). classify_hit() below maps known/likely
    list-name patterns to this pipeline's 5 legal_* parameters on a
    best-effort basis; anything that doesn't match a known pattern is
    real, citable evidence that doesn't fit this pipeline's existing
    scoring buckets -- it is surfaced as an extra cross-check finding,
    never silently dropped, and never force-fit into a parameter it
    doesn't actually belong to.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from vdd.finoscale_api.client import FinoscaleAPIError, FinoscaleClient

logger = logging.getLogger(__name__)

# Confirmed from Zigram's own fixed category names.
_SANCTIONS_CATEGORY_MARKERS = ("sanctioncheck", "uani check", "country regimes watchlist")
_PEP_CATEGORY_MARKERS = ("pepcheck",)
# Best-effort keyword match against a hit row's own ListName/List Type text --
# NOT yet confirmed against a real example of each; see module docstring.
_WILFUL_DEFAULTER_KEYWORDS = ("cibil", "wilful defaulter", "wilful defaulters")
_ECOURTS_KEYWORDS = ("high court", "supreme court", "district court", "nclt", "nclat", "litigation")
_DRT_KEYWORDS = ("debt recovery tribunal", "sarfaesi", " drt ", "drat")

# A hollow stub response has exactly 1 category ("Angola Watchlists"); a real
# response has ~60. `Subscribed` is NOT a reliable signal for this -- confirmed
# empirically (2026-09-08) to read empty {} on BOTH hollow and genuinely
# comprehensive responses. Category *count* is the real signal.
_MIN_CATEGORIES_FOR_COMPREHENSIVE = 10


def zigram_full_screen(client: Optional[FinoscaleClient], entity_name: str, entity_type: str,
                        identifier: Optional[str], identifier_kind: str = "pan") -> Optional[dict]:
    """One live Zigram call. Requires a real CIN/PAN/LLPIN -- refuses to
    call with none at all, since a name-only screen is untested (see
    module docstring). Sets `clientId` to that same identifier
    defensively; whether that specific detail matters is NOT confirmed
    (see module docstring's RELIABILITY note) -- what IS confirmed is that
    a real `cin=`/`pan=` in the request correlates with comprehensive
    responses (3 for 3 in live testing, 2026-09-08).
    `identifier_kind` must be one of "pan", "cin", "llpin"."""
    if client is None or not identifier:
        return None
    kwargs = {identifier_kind: identifier}
    try:
        return client.zigram_screening(entity_name=entity_name, client_id=identifier, type_=entity_type,
                                        country=["India"], **kwargs)
    except FinoscaleAPIError as e:
        return {"_error": f"[{e.status_code}] {e.message}"}
    except Exception as e:
        return {"_error": str(e)}


def is_comprehensive(response: Optional[dict]) -> bool:
    """Distinguishes a real ~57-category response from the hollow
    "Angola Watchlists only" stub."""
    if not isinstance(response, dict) or "_error" in response:
        return False
    ec = response.get("entitychecks")
    if not isinstance(ec, list) or not ec or not isinstance(ec[0], dict):
        return False
    list_like_keys = [k for k, v in ec[0].items() if isinstance(v, list)]
    return len(list_like_keys) >= _MIN_CATEGORIES_FOR_COMPREHENSIVE


_META_KEYS = ("HitsFound", "entityName", "entityType", "totalResponseCount")


def _iter_hit_rows(response: dict):
    """Yield (category, row) ONLY for categories `HitsFound` reports a
    nonzero count for. Every category -- hit or not -- carries at least
    one row in its list (usually a blank/placeholder row, status "Green",
    zero real content), the same pattern as the "Angola Watchlists"
    boilerplate block that's present on every response regardless of
    comprehensiveness. Confirmed the hard way, 2026-09-08: iterating every
    row in every category (instead of gating on HitsFound) produced ~60
    false "hits" -- one per watchlist category -- when only ONE category
    (Indian Watchlists, HitsFound=1) had a real match. `HitsFound` is the
    actual ground truth for which categories found something; a category's
    row list existing, or its rows having non-blank-looking fields, is
    NOT sufficient on its own."""
    ec = response.get("entitychecks")
    if not isinstance(ec, list) or not ec or not isinstance(ec[0], dict):
        return
    block = ec[0]
    hits_found = block.get("HitsFound")
    if not isinstance(hits_found, dict):
        # No HitsFound to gate on at all -- can't reliably tell real hits from
        # placeholder rows, so report nothing rather than risk ~60 false positives.
        return
    for category, count in hits_found.items():
        try:
            if int(count or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        rows = block.get(category)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row:
                    yield category, row


def classify_hit(category: str, row: dict) -> str:
    """Maps one hit row to a legal_* parameter id, or 'other' if it's real
    evidence that doesn't fit this pipeline's existing 5-parameter model
    (e.g. the confirmed ESIC-defaulter finding -- a genuine labour-
    compliance issue with no matching AML-01..05 slot)."""
    cat_low = (category or "").lower()
    list_name = str(row.get("ListName") or row.get("List Type") or "").lower()
    blob = f"{cat_low} {list_name}"
    if any(k in cat_low for k in _SANCTIONS_CATEGORY_MARKERS):
        return "legal_sanctions"
    if any(k in cat_low for k in _PEP_CATEGORY_MARKERS):
        return "legal_pep"
    if any(k in blob for k in _WILFUL_DEFAULTER_KEYWORDS):
        return "legal_rbi_wilful_defaulter"
    if any(k in blob for k in _ECOURTS_KEYWORDS):
        return "legal_ecourts"
    if any(k in blob for k in _DRT_KEYWORDS):
        return "legal_drt_sarfaesi"
    return "other"


def summarize_screen(response: Optional[dict], entity_label: str) -> dict[str, Any]:
    """-> {parameter_id_or_'other': [hit summary strings]}, plus
    '_comprehensive': bool, '_error': str|None. A hit summary always cites
    the specific list matched, the match confidence, and the source link
    when Zigram provides one -- never a bare 'found something'."""
    out: dict[str, Any] = {"_comprehensive": False, "_error": None}
    if response is None:
        out["_error"] = "not screened this run"
        return out
    if "_error" in response:
        out["_error"] = response["_error"]
        return out
    out["_comprehensive"] = is_comprehensive(response)
    for category, row in _iter_hit_rows(response):
        # _iter_hit_rows already gates every category (including "Angola
        # Watchlists", present on every response regardless of
        # comprehensiveness) on HitsFound > 0, so a row reaching here is a
        # real match, not a placeholder -- no further filtering needed.
        pid = classify_hit(category, row)
        list_name = row.get("ListName") or row.get("List Type") or category
        summary = (f"{entity_label}: matched '{list_name}' (category: {category}), "
                    f"fuzzy_score={row.get('fuzzy_score', row.get('FinalScore', '?'))}, "
                    f"status={row.get('match_status', row.get('FinalStatus', '?'))}"
                    + (f", source={row.get('SourceLink')}" if row.get('SourceLink') else ""))
        out.setdefault(pid, []).append(summary)
    return out
