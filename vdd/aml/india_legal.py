"""
India-specific legal / PEP screeners for the `legal_*` ScoringModel.json
parameters that the sanctions sweep in `screening.py` doesn't cover.

Same conventions as `screening.py`: every screener is free/public with no API
key, returns a `Finding`, and **fails closed to Severity.WATCH ("unscreened")**
on any network/parse error -- a source that couldn't be reached is never
reported the same way as a source that was reached and came back empty.

What is and isn't wired up here, from a live investigation of each portal
(2026-09-03). This is deliberately documented in code because "we didn't
automate it" and "we automated it and it found nothing" must never be
confusable in a KYC report:

  DRT / SARFAESI  -- AUTOMATED. drt.gov.in's React app talks to a plain
      JSON API at https://drt.gov.in/drtapi with no login, no cookies and no
      server-side captcha (the on-screen captcha is generated *and* compared
      in the browser by `Math.random`, and is never sent to the server).
      Covers all 44 tribunals (39 DRTs + 5 DRATs), including SARFAESI s.17
      Securitisation Applications (casetype "SA"). robots.txt disallows
      nothing.

  PEP             -- AUTOMATED via two free India sources: MyNeta/ADR
      (Election Commission candidate affidavits, incl. a criminal-cases flag)
      and the Wikidata Query Service (live office-holder records; Wikidata is
      also the largest single contributor of Indian PEPs to OpenSanctions'
      own PEP collection). OpenSanctions' /search and /match APIs now return
      401 "No API key provided", so they are not usable key-free; its free
      bulk PEP dump is usable but is ~180 MB/day and is licensed CC-BY-NC,
      which needs a commercial-licensing decision before it ships inside a
      paid product -- noted as a future enhancement, not silently used.

  eCourts / NJDG  -- NOT AUTOMATED. The party-name search is gated by a
      server-side Securimage image captcha (the answer lives in the PHP
      session and never reaches the client), behind a second rotating
      `app_token` request-validation layer that rejects fully-formed POSTs
      with "Invalid Request". NJDG itself has no party-name search at all,
      and there is no eCourts API on API Setu. Automating it would require a
      captcha-solving service -- out of scope for a compliance product.

  RBI Wilful Defaulter -- NOT AUTOMATED. RBI no longer publishes the list:
      under the Nov-2025 "Treatment of Wilful Defaulters and Large
      Defaulters" Directions, lenders report to credit information companies
      and there is no public-website publication clause. The one free public
      search (TransUnion CIBIL's suit.cibil.com "Public Access") is behind a
      Cloudflare Turnstile managed challenge, and its own terms forbid
      automated access and commercial reuse. Manual analyst step.
"""
from __future__ import annotations

import logging
import re
import time
from typing import List, Optional

import requests

from vdd.aml.screening import Finding, Severity, _SESSION, _name_matches, _normalize

logger = logging.getLogger(__name__)

_TIMEOUT = 45

# Honorifics that appear on Indian PEP/candidate records and carry no identifying
# information -- left in, they inflate token overlap and cause false positives.
_HONORIFICS = {"shri", "smt", "smt.", "sri", "kumari", "km", "dr", "dr.", "mr", "mrs",
                "ms", "prof", "adv", "late", "thiru", "shrimati", "sardar", "haji"}


def _strip_honorifics(name: str) -> str:
    toks = [t for t in re.split(r'\s+', _normalize(name)) if t.strip(".") not in _HONORIFICS]
    return " ".join(toks)


# Cause-title boilerplate on Indian tribunal records. It carries no identifying
# information but does inflate a party string's significant-token count, which
# matters because `_name_matches` only lets a single-token entity name match a
# target with <= 3 significant tokens (its guard against a common word matching
# an unrelated long name). Verified: "KINGFISHER INDUSTRIES" against the real
# respondent "MESSRS KINGFISHER INDUSTRIES AND OTHERS" was rejected before this.
_PARTY_BOILERPLATE = re.compile(
    r'\b(messrs|m\s*/?\s*s|and\s+others|&\s*others|anr|ors|others|through\s+its\s+\w+|'
    r'proprietor|proprietrix|partner|partners|director|directors|represented\s+by)\b', re.I)


def _clean_party(party: str) -> str:
    return re.sub(r'\s+', ' ', _PARTY_BOILERPLATE.sub(' ', party or '')).strip()


# ==================================================================== DRT / SARFAESI
DRT_API = "https://drt.gov.in/drtapi"


def _drt_tribunals() -> List[dict]:
    """44 rows: [{"SchemaName": "...", "schemeNameDrtId": "1"}, ...].
    ids >= 100 are the appellate tribunals (DRATs) and use a different endpoint."""
    resp = _SESSION.post(f"{DRT_API}/getDrtDratScheamName", timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def _drt_search_one(tribunal_id: str, party_name: str) -> List[dict]:
    """The API answers a HIT with a JSON *array* of case rows and a MISS with the
    JSON *object* {"status": "Record Not Fund"} (the source's own typo). Both are
    truthy, so the shape -- not truthiness -- is what distinguishes them.

    Must be form-encoded/multipart: a JSON body returns HTTP 500.
    """
    endpoint = "drat_party_name_wise" if int(tribunal_id) >= 100 else "casedetail_party_name_wise"
    resp = _SESSION.post(f"{DRT_API}/{endpoint}",
                          data={"schemeNameDratDrtId": str(tribunal_id),
                                "partyName": party_name.upper()},
                          timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def screen_drt_sarfaesi(names: List[str]) -> Finding:
    """Screen the firm and each partner/director across all DRTs and DRATs.

    The server matches with a case-insensitive SQL `LIKE %...%` substring, so raw
    rows are riddled with false positives (the query "SBI" returns an applicant
    named "JASBIR KAUR"). Every returned applicant/respondent is therefore
    re-checked through the shared whole-token `_name_matches` before it is
    allowed to count as a hit, and raw row counts are never reported as findings.
    """
    source_url = "https://drt.gov.in/"
    names = [n for n in (names or []) if n and len(n.strip()) >= 4]
    if not names:
        return Finding("", "DRT / DRAT (Debt Recovery Tribunals)",
                        "No entity or partner name available to screen.", Severity.WATCH, source_url)
    label = names[0]
    try:
        tribunals = _drt_tribunals()
        if not tribunals:
            return Finding(label, "DRT / DRAT (Debt Recovery Tribunals)",
                            "Tribunal list came back empty -- screen not performed.", Severity.WATCH, source_url)
        hits, raw_rows = [], 0
        for name in names:
            for t in tribunals:
                tid = t.get("schemeNameDrtId")
                if tid is None:
                    continue
                for row in _drt_search_one(tid, name):
                    raw_rows += 1
                    # Check applicant and respondent independently -- concatenating them
                    # doubles the target's token count and trips _name_matches' guard.
                    if any(_name_matches(_strip_honorifics(name), _clean_party(p))
                           for p in (row.get("applicant"), row.get("respondent")) if p):
                        hits.append(f"{t.get('SchemaName', tid)}: {row.get('casetype', '?')} "
                                     f"{row.get('caseno', '?')} filed {row.get('dateoffiling', '?')} "
                                     f"-- {(row.get('applicant') or '').strip()} vs "
                                     f"{(row.get('respondent') or '').strip()} [matched on '{name}']")
        if hits:
            return Finding(label, "DRT / DRAT (Debt Recovery Tribunals)",
                            f"{len(hits)} debt-recovery / SARFAESI case(s) matched: " + "; ".join(hits[:6])
                            + (f" (+{len(hits) - 6} more)" if len(hits) > 6 else ""),
                            Severity.HIGH, source_url)
        detail = (f"No DRT/DRAT or SARFAESI case matched {len(names)} name(s) across "
                  f"{len(tribunals)} tribunals")
        if raw_rows:
            detail += (f" ({raw_rows} loose substring row(s) returned by the portal were rejected by the "
                        f"whole-token name matcher)")
        return Finding(label, "DRT / DRAT (Debt Recovery Tribunals)", detail + ".", Severity.NONE, source_url)
    except Exception as exc:
        logger.warning("DRT screen failed for %r: %s", label, exc)
        return Finding(label, "DRT / DRAT (Debt Recovery Tribunals)",
                        f"Screen unavailable this run ({type(exc).__name__}) -- not counted as clean.",
                        Severity.WATCH, source_url)


# ==================================================================== PEP
_MYNETA_URL = "https://www.myneta.info/search_myneta.php"


def _myneta_search(name: str) -> Optional[List[dict]]:
    """Election Commission candidate affidavits (ADR/MyNeta). Server-rendered
    HTML table: Candidate Name | Party | Constituency | Election | Criminal(Y/N).
    Returns None if the page couldn't be fetched/parsed at all."""
    resp = _SESSION.get(_MYNETA_URL, params={"q": name}, timeout=_TIMEOUT)
    resp.raise_for_status()
    html = resp.text
    if "results found" not in html and "Searched for" not in html:
        return None
    rows = []
    for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.S | re.I):
        cells = [re.sub(r'<[^>]+>', '', c) for c in re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S | re.I)]
        cells = [re.sub(r'\s+', ' ', c).strip() for c in cells]
        if len(cells) >= 5 and cells[0]:
            rows.append({"name": cells[0], "party": cells[1], "constituency": cells[2],
                         "election": cells[3], "criminal": cells[4].upper().startswith("Y")})
    return rows


_WDQS_URL = "https://query.wikidata.org/sparql"
_WDQS_QUERY = """
SELECT ?pLabel ?posLabel ?start WHERE {
  SERVICE wikibase:mwapi {
    bd:serviceParam wikibase:api "EntitySearch" ; wikibase:endpoint "www.wikidata.org" ;
                    mwapi:search %s ; mwapi:language "en" .
    ?p wikibase:apiOutputItem mwapi:item .
  }
  ?p wdt:P31 wd:Q5 ; p:P39 ?st .
  ?st ps:P39 ?pos .
  OPTIONAL { ?st pq:P580 ?start }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" }
} LIMIT 25
"""


def _sparql_literal(s: str) -> str:
    """SPARQL string literal (double-quoted, escaped)."""
    import json as _json
    return _json.dumps(s)


_WDQS_MIN_INTERVAL = 1.5   # seconds between WDQS queries -- it 429s on back-to-back calls
_WDQS_ATTEMPTS = 2         # best-effort enrichment only: don't stall a run retrying a throttled service
_WDQS_TIMEOUT = 20
_wdqs_last_call = 0.0


def _wikidata_officeholders(name: str) -> Optional[List[dict]]:
    """Live office-holder lookup (P39 'position held' on a human).

    WDQS enforces a robot policy: it requires a descriptive User-Agent and 429s
    on back-to-back queries (observed while screening 3 partner names in a
    loop), so calls are throttled and retried once with backoff. Without this
    the whole PEP parameter degrades to 'unscreened' for any vendor with more
    than one or two partners.
    """
    global _wdqs_last_call
    query = _WDQS_QUERY % _sparql_literal(name)
    headers = {"User-Agent": "vdd-report-agent/1.0 (Finoscale VDD; compliance screening)",
               "Accept": "application/sparql-results+json"}
    resp, last_exc = None, None
    for attempt in range(_WDQS_ATTEMPTS):
        wait = _WDQS_MIN_INTERVAL - (time.time() - _wdqs_last_call)
        if wait > 0:
            time.sleep(wait)
        _wdqs_last_call = time.time()
        try:
            resp = _SESSION.get(_WDQS_URL, params={"query": query, "format": "json"},
                                 headers=headers, timeout=_WDQS_TIMEOUT)
        except requests.RequestException as exc:
            last_exc, resp = exc, None
            logger.debug("WDQS transport error for %r (attempt %d): %s", name, attempt + 1, exc)
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code != 429:
            break
        backoff = float(resp.headers.get("Retry-After") or (2 * (attempt + 1)))
        logger.debug("WDQS 429 for %r -- backing off %.1fs (attempt %d)", name, backoff, attempt + 1)
        time.sleep(min(backoff, 10))
    if resp is None:
        raise last_exc if last_exc else RuntimeError("WDQS unreachable")
    resp.raise_for_status()
    bindings = resp.json().get("results", {}).get("bindings", [])
    return [{"name": b.get("pLabel", {}).get("value", ""),
             "position": b.get("posLabel", {}).get("value", ""),
             "since": b.get("start", {}).get("value", "")[:10]} for b in bindings]


def screen_pep(person_names: List[str]) -> Finding:
    """PEP screen over MyNeta (ECI candidate affidavits) + Wikidata office-holders.

    Takes **natural-person names only** (proprietor / partners / authorised
    signatories), which is what the scoring model's parameter asks for -- PEP
    status is a property of people, not of a firm. Passing the firm name here
    was also the expensive case for Wikidata's EntitySearch (a company name
    fans out to many candidate items and timed out).

    Source tiering, and why it is split this way:

      * MyNeta/ADR is **required**. It is the Election Commission's candidate
        affidavit database covering every candidate in every Lok Sabha, Rajya
        Sabha, State Assembly and Legislative Council election -- i.e. the core
        of the Indian PEP population (holders of prominent public functions),
        and it also carries a declared-criminal-cases flag. If it can't be
        reached, the whole screen degrades to WATCH/unscreened.

      * Wikidata WDQS is **best-effort enrichment**. It adds appointed
        officials, judges and bureaucrats that MyNeta misses, but it enforces a
        strict per-IP robot policy and returns 429/timeouts unpredictably.
        Gating the parameter on it made AML-02 unresolvable on any throttled
        run, so a WDQS failure is disclosed in the note as a residual coverage
        gap rather than voiding an otherwise-real screen.
    """
    source_url = "https://www.myneta.info/ + https://query.wikidata.org/"
    names = [n for n in (person_names or []) if n and len(n.strip()) >= 4]
    if not names:
        return Finding("", "PEP Screening (MyNeta/ECI + Wikidata)",
                        "No proprietor/partner/signatory name on record, so PEP status could not be "
                        "screened. Not counted as clean.", Severity.WATCH, source_url)
    label = names[0]
    hits, sources_ok, sources_failed = [], [], []
    myneta_ok = False

    try:
        for name in names:
            rows = _myneta_search(name)
            if rows is None:
                raise ValueError("MyNeta returned an unparseable page")
            for r in rows:
                if _name_matches(_strip_honorifics(name), _strip_honorifics(r["name"])):
                    hits.append(f"MyNeta/ECI: '{r['name']}' ({r['party']}, {r['constituency']}, "
                                 f"{r['election']}"
                                 + (", criminal cases declared" if r["criminal"] else "")
                                 + f") vs '{name}'")
        sources_ok.append("MyNeta/ECI candidate affidavits")
        myneta_ok = True
    except Exception as exc:
        logger.warning("MyNeta PEP screen failed: %s", exc)
        sources_failed.append(f"MyNeta/ECI ({type(exc).__name__})")

    try:
        for name in names:
            for r in _wikidata_officeholders(name) or []:
                if r["position"] and _name_matches(_strip_honorifics(name), _strip_honorifics(r["name"])):
                    hits.append(f"Wikidata: '{r['name']}' holds/held '{r['position']}'"
                                 + (f" since {r['since']}" if r["since"] else "") + f" vs '{name}'")
        sources_ok.append("Wikidata Query Service office-holders")
    except Exception as exc:
        # Best-effort tier: log at INFO, not WARNING. A throttled WDQS does not
        # void the screen (MyNeta is the required source) -- it becomes a
        # disclosed coverage gap in the Finding's summary instead, and a
        # warning-level line here reads like a failed run when it isn't.
        logger.info("Wikidata PEP enrichment unavailable (%s): %s", type(exc).__name__,
                     str(exc).split("\n")[0][:160])
        sources_failed.append(f"Wikidata WDQS ({type(exc).__name__})")

    if hits:
        return Finding(label, "PEP Screening (MyNeta/ECI + Wikidata)",
                        f"Possible PEP match on {len(names)} screened name(s) -- requires enhanced due "
                        f"diligence: " + "; ".join(hits[:5]), Severity.ELEVATED, source_url)
    if not myneta_ok:
        # The required source didn't run: there is no screen to report.
        return Finding(label, "PEP Screening (MyNeta/ECI + Wikidata)",
                        f"NOT SCREENED -- {', '.join(sources_failed)} unavailable this run"
                        + (f"; only {', '.join(sources_ok)} ran, which is not sufficient coverage on its own"
                            if sources_ok else "")
                        + ". Not counted as clean.", Severity.WATCH, source_url)
    summary = (f"No PEP match for {len(names)} proprietor/partner name(s) "
                f"({', '.join(names)}) across {', '.join(sources_ok)}.")
    if sources_failed:
        summary += (f" COVERAGE GAP: {', '.join(sources_failed)} did not run this time, so appointed "
                     f"officials/judiciary/bureaucracy records that source adds were not checked.")
    return Finding(label, "PEP Screening (MyNeta/ECI + Wikidata)", summary, Severity.NONE, source_url)
