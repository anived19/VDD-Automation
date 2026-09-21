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
    assert set(out.keys()) == {"_comprehensive", "_error", "_dismissed"}


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


def test_repeated_rows_of_one_list_collapse_to_one_line():
    """A monthly-republished watchlist returns the same entity once per
    compilation file (B R Trading Co, 2026-09-19: 19 rows of the Maharashtra
    GST non-genuine-dealer list, 7,800 characters in the note). One line per
    list + status, the strongest score, the first source and a count."""
    resp = _comprehensive_response_with_esic_hit()
    block = resp["entitychecks"][0]
    block["Indian Watchlists"] = [{
        "ListName": "Government of Maharashtra - GST - Non Genuine Dealers", "fuzzy_score": s, "match_status": "Red",
        "SourceLink": f"https://mahagst.gov.in/files/compilation-{i}.xlsx",
    } for i, s in enumerate(["95%", "100%", "100%"])] + [{
        "ListName": "Employees State Insurance Corporation (ESIC) - Defaulters List", "fuzzy_score": "100%",
        "match_status": "Red", "SourceLink": "https://www.esic.gov.in/x.pdf",
    }]
    block["HitsFound"]["Indian Watchlists"] = 4
    out = summarize_screen(resp, "B R TRADING CO")
    assert len(out["other"]) == 2
    gst = next(x for x in out["other"] if "Non Genuine" in x)
    assert "fuzzy_score=100%" in gst and "3 matching rows" in gst
    assert "compilation-0.xlsx" in gst and "(+2 more source file(s))" in gst
    assert gst.count("https://") == 1
    esic = next(x for x in out["other"] if "ESIC" in x)
    assert "matching rows" not in esic and "more source" not in esic


# ---------------------------------------------------------------- namesakes (21-Sep)
def _one_hit(row, entity="B R TRADING CO", pan="AADFB0389G"):
    resp = {"entitychecks": [{"HitsFound": {"Indian Watchlists": 1}, "Indian Watchlists": [row]}]}
    return summarize_screen(resp, entity, vendor_pan=pan)


def test_row_with_another_entitys_pan_is_dismissed_as_a_namesake():
    """B R Trading Co (PAN AADFB0389G, West Bengal) came back 19 times on the
    Maharashtra non-genuine-dealer list -- every row a different firm with
    its own GSTIN. Fresh Zigram call 2026-09-21: identical."""
    out = _one_hit({"ListName": "Government of Maharashtra - GST - Non Genuine Dealers", "Name": "B R TRADING",
                    "GST": "27EBDPG9119N1Z7", "Extracted PAN": "['EBDPG9119N']", "InputClientId": "AADFB0389G",
                    "fuzzy_score": "100%", "match_status": "Red"})
    assert "other" not in out and "legal_sanctions" not in out
    (d,) = out["_dismissed"]
    assert "EBDPG9119N is not the vendor's AADFB0389G" in d and "1 row(s)" in d


def test_row_with_the_vendors_own_pan_is_kept():
    out = _one_hit({"ListName": "Government of Maharashtra - GST - Non Genuine Dealers", "Name": "B R TRADING CO",
                    "GST": "19AADFB0389G1ZH", "fuzzy_score": "100%", "match_status": "Red"})
    assert out["_dismissed"] == [] and len(out["other"]) == 1


def test_row_whose_matched_name_is_a_different_name_is_dismissed():
    """The analysts' own Zigram case: 'MS R R TRADING COMPANY' on the DRT cause
    list, a 99% 'Exact Match' for M/S. B.R. TRADING COMPANY."""
    out = _one_hit({"ListName": "Debts Recovery Tribunals (DRTs) - DRT Causelist", "Name": "MS R R TRADING COMPANY",
                    "fuzzy_score": "99%", "match_status": "Red"}, entity="M/S. B.R. TRADING COMPANY")
    assert "legal_drt_sarfaesi" not in out
    assert "different name" in out["_dismissed"][0]


def test_row_with_no_pan_and_an_agreeing_name_is_kept():
    out = _one_hit({"ListName": "Employees State Insurance Corporation (ESIC) - Defaulters List",
                    "Name": "SKANDAN PLASTRIX PVT LTD", "fuzzy_score": "100%", "match_status": "Red"},
                   entity="SKANDAN PLASTRIX PRIVATE LIMITED", pan="ABMCS1968D")
    assert out["_dismissed"] == [] and len(out["other"]) == 1


def test_resolve_aml_records_the_dismissals_on_aml01():
    from vdd.aml import zigram_screening as Z
    from vdd.resolve.resolvers import ApiBundle, resolve_aml
    resp = {"entitychecks": [{"HitsFound": {"Indian Watchlists": 2}, "Indian Watchlists": [
        {"ListName": "Government of Maharashtra - GST - Non Genuine Dealers", "Name": "B R TRADING",
         "GST": "27EBDPG9119N1Z7", "fuzzy_score": "100%", "match_status": "Red"},
        {"ListName": "Government of Maharashtra - GST - Non Genuine Dealers", "Name": "B R TRADING",
         "GST": "27CGYPG7879K1Z5", "fuzzy_score": "100%", "match_status": "Red"}]}]}
    real = Z.is_comprehensive
    Z.is_comprehensive = lambda r: True
    try:
        out = resolve_aml(ApiBundle(zigram=resp), "B R TRADING CO", [], vendor_pan="AADFB0389G")
    finally:
        Z.is_comprehensive = real
    r = out["legal_sanctions"]
    assert r.value == "not_listed" and "Zigram namesakes dismissed: 2 row(s)" in r.note
    assert all(out[k].value in ("not_listed", "no_pep", "no_criminal", "no_drt") for k in out)
