"""The LLM reviewer's context budget: the report goes in as plain text, not
HTML, and no tool result can flood the context.

Background (2026-09-16, measured on the Skandan vendor with the real
tokenizer): the reviewer prompt used to embed the full rendered HTML report
(~16k tokens for ~2.5k tokens of actual content) and recheck_zigram_screening
returned Zigram's raw 245k-char response (~64k tokens) on almost every pass
-- ~115k input tokens per pass. These tests pin the two fixes without a
network, key or LLM.

Run: C:\\Python313\\python.exe -m pytest tests -q
"""
from langchain_core.utils.function_calling import convert_to_openai_tool

from vdd.report.render import build, build_text
from vdd.review import tools as review_tools
from vdd.review.tools import _MAX_TOOL_RESULT_CHARS, _cap, make_tools


def _minimal_context() -> dict:
    """Smallest dict build() accepts, with the HTML-escaped / inline-span
    values build_context() really produces so _text()'s collapsing is
    exercised, not just passed through."""
    return {
        "firm": "Acme &amp; Sons", "legal": "ACME AND SONS", "date": "16 September 2026",
        "location": "Coimbatore, Tamil Nadu", "seller_type": "Manufacturer",
        "chips": [("teal", "&#10003; GST Active"), ("white", "@LOC@Coimbatore, Tamil Nadu"), ("white", "Manufacturer")],
        "score": 37, "com": 20, "poa": 8, "poi": 6, "aml": 3,
        "entity": [("Trade Name", "Acme &amp; Sons"), ("Constitution", "Partnership")],
        "reg": [("GSTIN", "33AAAAA0000A1Z5", "m"), ("PAN", "AAAAA0000A", "m")],
        "profile": "Makes <b>widgets</b> &mdash; since 2019.",
        "hsn": [("3926", "Articles of plastics")],
        "findings": [("c", "GST registration is active."), ("n", "Bank account not verified.")],
        "com_rows": [("C1", "GSTIN Active", 'Yes <span class="pill ok">Active</span>'
                      '<span class="ev">Ongrid fetch-detailed: status=Active</span>')],
        "poa_rows": [("A1", "Address Ownership", "Rented")],
        "poi_rows": [("I1", "PAN Active", "Yes")],
        "aml_rows": [("L1", "Sanctions", "Clear")],
        "extra_unlock": [],
    }


def test_build_text_has_score_line_and_every_section_header():
    txt = build_text(_minimal_context())
    assert "Finoscale Basic Score: 37 / 100" in txt
    assert "(Compliance 20/25, Proof of Address 8/10, Proof of Identity 6/10, Legal/AML 3/5)" in txt
    for header in ("# Acme & Sons -- Verified Seller Profile",
                   "## Entity Details",
                   "## Registration & Compliance IDs",
                   "## Business Profile",
                   "## Declared Goods & Services (HSN) -- 1 row(s)",
                   "## Findings & Observations",
                   "## Compliance & KYC Scoring Detail",
                   "### COMPLIANCE -- 20/25",
                   "### PROOF OF ADDRESS -- 8/10",
                   "### PROOF OF IDENTITY -- 6/10",
                   "### LEGAL / AML CHECK -- 3/5",
                   "### ON-SITE VERIFICATION / 3B & 2B ANALYSIS / ITR ANALYSIS -- Pending (not scored)"):
        assert header in txt, header


def test_build_text_collapses_markup_and_keeps_evidence_sub_line():
    txt = build_text(_minimal_context())
    assert "<" not in txt and "&amp;" not in txt and "&mdash;" not in txt
    assert "Location: Coimbatore, Tamil Nadu" in txt          # @LOC@ chip prefix translated
    assert "Makes widgets — since 2019." in txt               # entity unescaped, tags dropped
    assert "- C1 GSTIN Active: Yes -- Active -- Ongrid fetch-detailed: status=Active" in txt
    assert "- [OK] GST registration is active." in txt and "- [!] Bank account not verified." in txt
    # The whole point: same content, a fraction of the size the HTML was.
    assert len(txt) * 4 < len(build(_minimal_context()))


def test_repeated_identical_tool_call_returns_pointer_not_payload():
    calls = []

    def probe(x: str, n: int = 1) -> dict:
        """probe"""
        calls.append((x, n))
        return {"x": x, "n": n}

    seen: dict = {}
    capped = _cap(probe, seen)
    assert capped("a", n=2) == {"x": "a", "n": 2}
    again = capped("a", n=2)
    assert again["repeated_call"] is True and "call #1" in again["note"]
    assert calls == [("a", 2)]                                # the underlying tool ran once
    assert capped("b", n=2) == {"x": "b", "n": 2}            # different args -> real call


def test_oversized_tool_result_is_truncated():
    def big() -> dict:
        """big"""
        return {"blob": "z" * (_MAX_TOOL_RESULT_CHARS * 3)}

    out = _cap(big, {})()
    assert out["truncated"] is True and out["total_chars"] > _MAX_TOOL_RESULT_CHARS
    assert len(out["preview"]) == _MAX_TOOL_RESULT_CHARS


def test_seen_is_per_make_tools_call():
    """A repeat is only a repeat within one pass -- the next pass's make_tools()
    gets a fresh `seen`, so the same recheck runs again and can pick up a
    corrected value."""
    a = {t.__name__: t for t in make_tools(None)}["recheck_gstin_live"]
    b = {t.__name__: t for t in make_tools(None)}["recheck_gstin_live"]
    assert "error" in a("33AAAAA0000A1Z5")                    # no client -> unavailable, but a real result
    assert a("33AAAAA0000A1Z5").get("repeated_call") is True
    assert "repeated_call" not in b("33AAAAA0000A1Z5")


def test_wrapping_keeps_tool_schemas_identical():
    """functools.wraps sets __wrapped__, which inspect.signature follows, so
    create_agent infers the exact same schema from the wrapped tool as from
    the plain function."""
    expected = {
        "recheck_sanctions": ["entity_name"],
        "recheck_pep": ["person_names"],
        "recheck_drt_sarfaesi": ["names"],
        "recheck_gstin_live": ["gstin"],
        "recheck_bank_verification": ["account_number", "ifsc"],
        "recheck_zigram_screening": ["entity_name", "type_", "pan", "cin", "llpin"],
        "web_search": ["query"],
    }
    schemas = [convert_to_openai_tool(t)["function"] for t in make_tools(None)]
    assert [s["name"] for s in schemas] == list(expected)
    for s in schemas:
        assert list(s["parameters"]["properties"]) == expected[s["name"]], s["name"]
        assert s["description"], s["name"]                     # docstring survived the wrap
    assert review_tools._WEB_SNIPPET_CHARS < _MAX_TOOL_RESULT_CHARS
