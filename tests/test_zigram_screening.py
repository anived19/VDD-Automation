"""Coverage for vdd/aml/zigram_screening.py -- the hollow-vs-comprehensive
detector and the hit classifier.

Fixtures below are shaped to match the real, live, non-cached responses
captured 2026-09-08 (Skandan Plastrix) as closely as a compact test
fixture reasonably can. Note on the "hollow" fixture specifically: every
live call actually made this session came back comprehensive (see
zigram_screening.py's module docstring for the full, self-corrected
story) -- there is currently no confirmed-real example of a genuinely
hollow response with `HitsFound` present. `_hollow_response()` below is a
defensive fixture for `is_comprehensive()`'s category-count threshold
logic, not a claim that this exact shape has been observed live.

Run: C:\\Python313\\python.exe -m pytest tests -q
"""
from vdd.aml.zigram_screening import classify_hit, is_comprehensive, summarize_screen

_BLANK_ANGOLA_ROW = {"Action Taken": "", "Address": "", "Full Name": "", "InputClientId": "x"}


def _hollow_response():
    """Defensive fixture only -- see module docstring above. Includes a
    HitsFound dict (all zero) since every real response observed so far
    carries one; a response with no HitsFound at all is handled separately
    (see test_summarize_response_with_no_hitsfound_reports_nothing)."""
    return {
        "Case_Outcome": {"Status": "Red", "Score": "10/10"},
        "Subscribed": {},
        "entitychecks": [{"Angola Watchlists": [dict(_BLANK_ANGOLA_ROW)], "HitsFound": {"Angola Watchlists": 0}}],
        "pdfUrl": "NA",
    }


_OTHER_CATEGORIES = (
    "Australia Watchlists", "SanctionCheck", "PepCheck", "United States Watchlist",
    "UANI Check", "Suspected Shell Companies Check", "ICIJ Watchlist", "Canada Watchlist",
    "Japan Watchlist", "Singapore Watchlist", "Human Rights Violations", "State Owned Enterprises",
    "Country Regimes Watchlist", "United Kingdom Watchlists", "New Zealand Watchlist",
)  # 15 more, on top of Angola + Indian Watchlists -- 17 total, comfortably past the real 1-vs-~63 gap


def _comprehensive_response_with_esic_hit():
    """Real shape confirmed live 2026-09-08 (3 for 3 calls, identical
    result each time): ~63 category keys, one genuine hit (ESIC
    Defaulters List) under "Indian Watchlists", HitsFound all zero except
    that one category."""
    block = {"Angola Watchlists": [dict(_BLANK_ANGOLA_ROW)]}
    hits_found = {"Angola Watchlists": 0}
    for name in _OTHER_CATEGORIES:
        block[name] = []
        hits_found[name] = 0
    block["Indian Watchlists"] = [{
        "ListName": "Employees State Insurance Corporation (ESIC) - Defaulters List",
        "Name of Office": "Esic Sro Coimbatore",
        "fuzzy_score": "100%", "match_status": "Red",
        "SourceLink": "https://www.esic.gov.in/attachments/defaulterfile/x.pdf",
    }]
    hits_found["Indian Watchlists"] = 1
    block["HitsFound"] = hits_found
    block["entityName"] = "SKANDAN PLASTRIX PRIVATE LIMITED"
    return {
        "Case_Outcome": {"Status": "Red", "Score": "10/10"},
        "Subscribed": {},  # confirmed empty on every real response seen so far -- not a usable signal
        "entitychecks": [block],
        "pdfUrl": "NA",
    }


def _comprehensive_clean_response():
    block = {"Angola Watchlists": [dict(_BLANK_ANGOLA_ROW)], "Indian Watchlists": []}
    hits_found = {"Angola Watchlists": 0, "Indian Watchlists": 0}
    for name in _OTHER_CATEGORIES:
        block[name] = []
        hits_found[name] = 0
    block["HitsFound"] = hits_found
    return {"Case_Outcome": {"Status": "Green", "Score": "0/10"}, "Subscribed": {},
            "entitychecks": [block], "pdfUrl": "NA"}


# ---------------------------------------------------------------- is_comprehensive
def test_hollow_response_is_not_comprehensive():
    assert is_comprehensive(_hollow_response()) is False


def test_full_response_is_comprehensive_even_though_subscribed_is_empty():
    # The whole point of this check: Subscribed=={} on its own must NOT be
    # read as "hollow" -- category count is the real signal.
    resp = _comprehensive_response_with_esic_hit()
    assert resp["Subscribed"] == {}
    assert is_comprehensive(resp) is True


def test_error_response_is_not_comprehensive():
    assert is_comprehensive({"_error": "[403] blocked"}) is False


def test_none_response_is_not_comprehensive():
    assert is_comprehensive(None) is False


# ---------------------------------------------------------------- classify_hit
def test_sanctioncheck_category_maps_to_legal_sanctions():
    assert classify_hit("SanctionCheck", {"ListName": "OFAC SDN"}) == "legal_sanctions"


def test_pepcheck_category_maps_to_legal_pep():
    assert classify_hit("PepCheck", {"ListName": "Politically Exposed Persons"}) == "legal_pep"


def test_cibil_list_name_maps_to_wilful_defaulter():
    assert classify_hit("Indian Watchlists", {"ListName": "CIBIL Suit Filed List"}) == "legal_rbi_wilful_defaulter"


def test_high_court_list_name_maps_to_ecourts():
    assert classify_hit("Indian Watchlists", {"ListName": "Delhi High Court Case Status"}) == "legal_ecourts"


def test_drt_list_name_maps_to_drt_sarfaesi():
    assert classify_hit("Indian Watchlists", {"ListName": "Debt Recovery Tribunal Order"}) == "legal_drt_sarfaesi"


def test_esic_list_name_does_not_get_force_fit_into_any_existing_parameter():
    # Confirmed real (2026-09-08): a genuine finding with no matching AML-01..05
    # slot must classify as 'other', never silently mapped to the wrong bucket.
    assert classify_hit("Indian Watchlists", {
        "ListName": "Employees State Insurance Corporation (ESIC) - Defaulters List"}) == "other"


# ---------------------------------------------------------------- summarize_screen
def test_summarize_hollow_response_finds_nothing_and_is_not_comprehensive():
    out = summarize_screen(_hollow_response(), "the entity")
    assert out["_comprehensive"] is False
    assert out["_error"] is None
    assert "legal_sanctions" not in out and "other" not in out


def test_summarize_comprehensive_esic_hit_lands_under_other_not_a_scored_parameter():
    out = summarize_screen(_comprehensive_response_with_esic_hit(), "SKANDAN PLASTRIX PRIVATE LIMITED")
    assert out["_comprehensive"] is True
    assert "other" in out and len(out["other"]) == 1
    assert "ESIC" in out["other"][0]
    assert "legal_rbi_wilful_defaulter" not in out  # ESIC is not wilful-defaulter data -- must not be conflated
    assert "legal_sanctions" not in out and "legal_pep" not in out


def test_summarize_comprehensive_clean_response_has_no_hits_anywhere():
    out = summarize_screen(_comprehensive_clean_response(), "a clean entity")
    assert out["_comprehensive"] is True
    assert set(out.keys()) == {"_comprehensive", "_error"}


def test_summarize_zero_hit_placeholder_rows_are_never_reported_as_findings():
    # Regression test for a real bug caught 2026-09-08: iterating every row in
    # every category (instead of gating on HitsFound) produced ~60 false
    # "other" hits from zero-count categories' placeholder rows.
    out = summarize_screen(_comprehensive_response_with_esic_hit(), "SKANDAN PLASTRIX PRIVATE LIMITED")
    assert len(out.get("other", [])) == 1  # only the real ESIC hit, not one per category


def test_summarize_response_with_no_hitsfound_reports_nothing():
    # No HitsFound to gate on at all -- must not guess from row presence/shape.
    resp = {"entitychecks": [{"Angola Watchlists": [dict(_BLANK_ANGOLA_ROW)]}]}
    out = summarize_screen(resp, "the entity")
    assert "other" not in out and "legal_sanctions" not in out


def test_summarize_none_response_reports_not_screened():
    out = summarize_screen(None, "the entity")
    assert out["_comprehensive"] is False
    assert out["_error"] == "not screened this run"


def test_summarize_error_response_surfaces_the_error():
    out = summarize_screen({"_error": "[403] blocked"}, "the entity")
    assert out["_error"] == "[403] blocked"
