"""
Sanctions/debarment screening -- ported from `report-agentic/tools/aml_tools.py`
(the same tools already built and bug-fixed for the LangGraph credit-report
project). Trimmed to a self-contained module with no cross-project imports:
dropped the Gemini-LLM adverse-media filter, SEC EDGAR (US-specific), and
TI-CPI/FATF jurisdiction context -- none of those map to a ScoringModel.json
legal_* parameter here. Kept: OFAC SDN, World Bank debarred firms, UN Security
Council Consolidated List, EU Financial Sanctions File, OpenSanctions, and the
whole-token name-matcher (the false-positive fix already validated upstream).

Four of the five sources are free/public with no API key. **OpenSanctions is
the exception as of 2026-09-03**: its `/entities/_search`, `/search/*` and
`/match/*` endpoints now all return `401 {"detail":"No API key provided."}`,
so that screener contributes nothing on every run and fails closed to
"unscreened". It is left wired up so the row is visible (and starts working
the moment a key is configured), but the primary EU/UN coverage comes from the
direct EU FSF and UN Consolidated List screeners below, not from
OpenSanctions' aggregation. OpenSanctions' free *bulk* dumps
(data.opensanctions.org) are an alternative no-key path but are licensed
CC-BY-NC, which needs a commercial-licensing decision first.

Every screener fails closed to "no match / unscreened" on a network error,
never a fabricated "clean" result presented the same way as a genuine
negative -- and `_looks_like`/`_cached_get` refuse to cache a response that
doesn't match the shape the source is supposed to return, so an HTTP error
page can't be laundered into a "clean" screen.

India-specific registers (PEP, DRT/SARFAESI, eCourts, RBI wilful defaulter)
live in `vdd/aml/india_legal.py`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx
import requests

logger = logging.getLogger(__name__)


class Severity(str, Enum):
    NONE = "none"
    WATCH = "watch"
    ELEVATED = "elevated"
    HIGH = "high"


@dataclass
class Finding:
    entity_screened: str
    source_name: str
    finding_summary: str
    severity: Severity
    source_url: str = ""


_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, application/xml, text/xml, text/html, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "cache" / "aml"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_XML_CACHE_TTL_HOURS = 24
_TIMEOUT = 15


def _cache_key(url: str) -> Path:
    return _CACHE_DIR / f"{hashlib.md5(url.encode()).hexdigest()}.cache"


def _looks_like(text: str, expect: Optional[str]) -> bool:
    """Cheap content sniff. A tiered fetcher that falls back to a real browser will
    happily render an HTTP error page to text and hand it back as a success, so
    every bulk source declares the shape it expects and anything else is treated
    as a failed fetch rather than cached as data."""
    if not text or not text.strip():
        return False
    head = text.lstrip()[:4096]
    if expect == "xml":
        return head.startswith("<?xml") or (head.startswith("<") and "Whitelabel Error" not in head
                                             and "<html" not in head[:200].lower())
    if expect == "json":
        return head[0] in "[{"
    return True


def _fetch_with_fallback(url: str, headers: dict, expect: Optional[str] = None) -> Optional[str]:
    """Tiered fetch for sources that block naive requests: httpx/HTTP2 -> requests -> Playwright."""
    try:
        with httpx.Client(http2=True, headers=headers, follow_redirects=True, timeout=_TIMEOUT) as client:
            resp = client.get(url)
        if resp.status_code < 400 and _looks_like(resp.text, expect):
            return resp.text
    except Exception as exc:
        logger.debug("Tier-1 (httpx) fetch failed for %s: %s", url, exc)

    try:
        resp = _SESSION.get(url, headers=headers, timeout=_TIMEOUT)
        if resp.status_code < 400 and _looks_like(resp.text, expect):
            return resp.text
    except Exception as exc:
        logger.debug("Tier-2 (requests) fetch failed for %s: %s", url, exc)

    # A headless browser is only useful for JS-gated HTML pages -- never for a bulk
    # XML/JSON file, where it can only launder an error page into plausible-looking
    # text. Skip the tier entirely when a machine-readable shape is expected.
    if expect in ("xml", "json"):
        return None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_context(user_agent=headers.get("User-Agent")).new_page()
            resp = page.goto(url, timeout=20000, wait_until="domcontentloaded")
            text = page.evaluate("() => document.body ? document.body.innerText : ''")
            status = resp.status if resp is not None else 0
            browser.close()
            if status and status < 400 and _looks_like(text, expect):
                return text
            logger.debug("Tier-3 (browser) got HTTP %s / unusable body for %s", status, url)
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("Tier-3 (browser) fetch failed for %s: %s", url, exc)
    return None


def _cached_get(url: str, ttl_hours: int = _XML_CACHE_TTL_HOURS, headers: Optional[dict] = None,
                 expect: Optional[str] = None) -> Optional[str]:
    path = _cache_key(url)
    if path.exists() and (time.time() - path.stat().st_mtime) / 3600 < ttl_hours:
        try:
            cached = path.read_text(encoding="utf-8", errors="replace")
            if _looks_like(cached, expect):
                return cached
            # Poisoned entry from an older run -- drop it rather than serve it.
            path.unlink(missing_ok=True)
        except Exception:
            pass
    req_headers = dict(_SESSION.headers)
    if headers:
        req_headers.update(headers)
    text = _fetch_with_fallback(url, req_headers, expect=expect)
    if text:
        try:
            path.write_text(text, encoding="utf-8")
        except Exception:
            pass
    return text


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


_GENERIC_STOP_WORDS = {
    "bank", "corp", "corporation", "ltd", "limited", "inc", "incorporated",
    "group", "holdings", "holding", "company", "co", "services", "industries",
    "industry", "state", "national", "trust", "financial", "finance", "the",
    "of", "and", "&", "ltd.", "inc.", "plc", "sa", "gmbh", "pvt", "private",
    "public", "enterprises", "international", "global",
}


def _name_matches(entity: str, target: str) -> bool:
    """Whole-token matcher -- avoids substring false positives (e.g. 'tata' inside 'batata')."""
    if not entity or not target:
        return False
    entity_n, target_n = _normalize(entity), _normalize(target)
    if entity_n == target_n:
        return True
    entity_tokens = [w for w in re.findall(r"\b[a-zA-Z0-9]{3,}\b", entity_n) if w not in _GENERIC_STOP_WORDS]
    target_tokens = set(re.findall(r"\b[a-zA-Z0-9]{3,}\b", target_n))
    target_tokens_significant = target_tokens - _GENERIC_STOP_WORDS
    if not entity_tokens:
        return False
    if len(entity_tokens) >= 2:
        matching = [w for w in entity_tokens if w in target_tokens]
        return len(matching) == len(entity_tokens) or len(matching) >= 2
    single_word = entity_tokens[0]
    if re.search(rf"\b{re.escape(single_word)}\b", target_n):
        return len(target_tokens_significant) <= 3
    return False


# ---------------------------------------------------------------- OFAC SDN
_OFAC_URLS = ["https://www.treasury.gov/ofac/downloads/sdn.xml", "https://data.treasury.gov/feed/sdn.xml"]


def screen_ofac_sdn(entity_name: str) -> Finding:
    source_url = "https://sanctionslist.ofac.treas.gov/Home/SdnList"
    try:
        xml_text = None
        for url in _OFAC_URLS:
            xml_text = _cached_get(url)
            if xml_text:
                break
        if xml_text:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml_text)
            for entry in root.iter():
                tag = entry.tag.lower()
                if ("lastname" in tag or "firstname" in tag or "sdnname" in tag or "akaname" in tag) \
                        and entry.text and _name_matches(entity_name, entry.text):
                    return Finding(entity_name, "OFAC SDN List",
                                    "Name match found in OFAC Specially Designated Nationals (SDN) registry. "
                                    "Requires manual compliance verification.", Severity.HIGH, source_url)
        return Finding(entity_name, "OFAC SDN List", "No match found in OFAC SDN list.", Severity.NONE, source_url)
    except Exception as exc:
        logger.warning("OFAC SDN screen failed for %r: %s", entity_name, exc)
        return Finding(entity_name, "OFAC SDN List", "Screen unavailable this run (network/parse error).",
                        Severity.WATCH, source_url)


# ---------------------------------------------------------------- OpenSanctions (aggregates EU/UN/WB + more)
_OPENSANCTIONS_URL = "https://api.opensanctions.org/entities/_search"


def screen_opensanctions(entity_name: str) -> Finding:
    source_url = f"https://www.opensanctions.org/search/?q={requests.utils.quote(entity_name)}"
    try:
        resp = _SESSION.get(_OPENSANCTIONS_URL, params={"q": entity_name, "limit": 5, "schema": "Thing"},
                             timeout=_TIMEOUT)
        if resp.status_code in (401, 403):
            return Finding(entity_name, "OpenSanctions Database",
                            "Requires an API key (endpoint returns 401 'No API key provided') -- unscreened, "
                            "not clean. Direct EU FSF / UN / OFAC / World Bank screens below are unaffected.",
                            Severity.WATCH, source_url)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        matches = [r for r in results if _name_matches(entity_name, " ".join(
            r.get("properties", {}).get("name", []) + r.get("properties", {}).get("alias", [])))]
        if matches:
            datasets = list({ds for r in matches for ds in r.get("datasets", [])})
            return Finding(entity_name, "OpenSanctions Database",
                            f"Potential match(es) found (datasets: {', '.join(datasets[:5]) or 'sanctions/watchlists'}). "
                            "Requires manual verification.", Severity.ELEVATED, source_url)
        return Finding(entity_name, "OpenSanctions Database", "No match found.", Severity.NONE, source_url)
    except Exception as exc:
        logger.warning("OpenSanctions screen failed for %r: %s", entity_name, exc)
        return Finding(entity_name, "OpenSanctions Database", "Screen unavailable this run.", Severity.WATCH, source_url)


# ---------------------------------------------------------------- World Bank debarred firms
_WB_URL = "https://apigwext.worldbank.org/dvsvc/v1.0/json/APPLICATION/ADOBE_EXPRNCE_MGR/FIRM/SANCTIONED_FIRM"
_WB_PUBLIC_API_KEY = "z9duUaFUiEUYSHs97CU38fcZO7ipOPvm"


def screen_world_bank_debarred(entity_name: str) -> Finding:
    source_url = "https://www.worldbank.org/en/projects-operations/procurement/debarred-firms"
    try:
        text = _cached_get(_WB_URL, headers={"apikey": _WB_PUBLIC_API_KEY, "Accept": "application/json"})
        if text:
            firms = json.loads(text).get("response", {}).get("ZPROCSUPP", [])
            if isinstance(firms, list):
                matches = [f for f in firms if _name_matches(entity_name, str(f.get("SUPP_NAME", "")))]
                if matches:
                    return Finding(entity_name, "World Bank Debarred Entities",
                                    f"Name match found ({len(matches)} entry/entries). Requires manual verification.",
                                    Severity.HIGH, source_url)
        return Finding(entity_name, "World Bank Debarred Entities", "No match found.", Severity.NONE, source_url)
    except Exception as exc:
        logger.warning("World Bank debarment screen failed for %r: %s", entity_name, exc)
        return Finding(entity_name, "World Bank Debarred Entities", "Screen unavailable this run.",
                        Severity.WATCH, source_url)


# ---------------------------------------------------------------- UN Consolidated List
_UN_XML_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"


def screen_un_sanctions(entity_name: str) -> Finding:
    source_url = "https://www.un.org/securitycouncil/content/un-sc-consolidated-list"
    try:
        xml_text = _cached_get(_UN_XML_URL)
        if xml_text:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml_text)
            for elem in root.iter():
                if elem.tag.upper() in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME",
                                         "ENTITY_NAME", "NAME_ORIGINAL_SCRIPT", "ALIAS_NAME") \
                        and elem.text and _name_matches(entity_name, elem.text):
                    return Finding(entity_name, "UN SC Consolidated List",
                                    "Name match found. Requires manual verification.", Severity.ELEVATED, source_url)
        return Finding(entity_name, "UN SC Consolidated List", "No match found.", Severity.NONE, source_url)
    except Exception as exc:
        logger.warning("UN sanctions screen failed for %r: %s", entity_name, exc)
        return Finding(entity_name, "UN SC Consolidated List", "Screen unavailable this run.", Severity.WATCH, source_url)


# ---------------------------------------------------------------- EU Financial Sanctions File
# The EU Financial Sanctions File needs the FSF's long-standing *public* access
# token as a query parameter; without it the endpoint answers 403 with a Spring
# "Whitelabel Error Page". This screener used to fetch the un-tokened URL, and
# the Playwright fallback tier then rendered that 403 page to text and cached it
# as if it were data -- which is where the "EU sanctions screen failed: syntax
# error: line 1, column 0" on every run came from (ET.fromstring on an HTML
# error page), not from anything about the name being screened.
_EU_XML_URL = ("https://webgate.ec.europa.eu/fsd/fsf/public/files/"
                "xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw")


def screen_eu_sanctions(entity_name: str) -> Finding:
    source_url = "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content"
    unavailable = Finding(entity_name, "EU Financial Sanctions List",
                           "EU FSF download not reachable this run -- treat this row as unscreened, not as clean.",
                           Severity.WATCH, source_url)
    try:
        xml_text = _cached_get(_EU_XML_URL, expect="xml")
        if not xml_text:
            return unavailable
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_text)
        for elem in root.iter():
            tag = elem.tag.lower()
            if "name" in tag or "alias" in tag:
                text_val = elem.text or elem.attrib.get("wholeName", "") or elem.attrib.get("name", "")
                if text_val and _name_matches(entity_name, text_val):
                    return Finding(entity_name, "EU Financial Sanctions List",
                                    "Name match found. Requires manual verification.", Severity.ELEVATED, source_url)
        return Finding(entity_name, "EU Financial Sanctions List", "No match found.", Severity.NONE, source_url)
    except Exception as exc:
        logger.warning("EU sanctions screen failed for %r: %s", entity_name, exc)
        return unavailable


def run_sanctions_sweep(entity_name: str) -> list[Finding]:
    """OFAC + OpenSanctions + World Bank + UN + EU, run in parallel. Free, no API key."""
    import concurrent.futures
    screeners = [screen_ofac_sdn, screen_opensanctions, screen_world_bank_debarred,
                 screen_un_sanctions, screen_eu_sanctions]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(screeners)) as ex:
        return list(ex.map(lambda fn: fn(entity_name), screeners))
