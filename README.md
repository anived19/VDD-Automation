# vdd-report-agent

Automates Finoscale's Vendor Due Diligence (VDD) / Seller Intelligence report
generation. Given a vendor's KYC document folder (and/or its PAN/GSTIN/CIN),
it resolves the "No-Consent" half of the Finoscale scoring model (Compliance,
Proof of Address, Proof of Identity, Legal/AML — 50 of 100 points) using a
mix of document extraction and the internal Finoscale Data API
(`api.finoscale.ai` — Ongrid, Digitap, Probe42), scores them per
`config/scoring_model.json`, and renders the finished branded HTML/PDF
report directly — no manual dropdown entry in the platform UI.

On-Site Verification, GSTR-3B/2B analysis and ITR analysis stay
manual/"Pending" in v1 (they are the consent-gated half of the model).

### Parameters that stay manual, and why

These four resolve to an explicit "not scored" with the blocker spelled out in
the report, rather than being assumed clean. Each is a documented finding from
a live investigation of the source, not an assumption:

| Parameter | Blocker |
|---|---|
| `com_bank_verification` (COM-07, 5 pts) | Every bucket in the scoring model asserts a **penny-drop** outcome, and the Finoscale Data API exposes no bank-account-verification endpoint. The one equivalent signal is the GST portal's own Bank Account Status (GSTN validates accounts on the NPCI rail against the registered taxpayer name); when a screenshot shows **Validated** and the account agrees with the cancelled cheque, the resolver *does* score the full 5. A **NotValidated** flag is neither scored as verified nor as "penny drop unsuccessful" (-5) — it is commonly a benign CC/OD account-type failure. |
| `legal_rbi_wilful_defaulter` (AML-03) | RBI no longer publishes the list (Nov-2025 Directions route it lender→credit information companies, with no public-website clause). CIBIL's free public search is behind a Cloudflare Turnstile challenge and its terms forbid automated/commercial reuse. |
| `legal_ecourts` (AML-04) | Server-side Securimage image captcha (answer held in the portal's PHP session) plus a rotating `app_token` request-validation layer. NJDG has no party-name search; no eCourts API on API Setu. |
| `com_pf_filing_status` (COM-09, conditional) | Only exposed on Probe42's `compliance` page, which returns 403 on the current API key. Treated as N/A when no EPFO establishment exists (per the model's own CONDITIONAL rule), never as non-compliant. |

`legal_pep` (MyNeta/ECI + Wikidata) and `legal_drt_sarfaesi` (drt.gov.in's own
JSON API, all 44 tribunals, incl. SARFAESI s.17 applications) *are* automated
— see `vdd/aml/india_legal.py`. Sanctions (OFAC / UN / EU FSF / World Bank)
are in `vdd/aml/screening.py`. Every screener fails closed to
"unscreened — not counted as clean" on any error. OpenSanctions is wired up
as a fifth source in that sweep but currently returns 401 (its free-tier
search endpoints now require a paid key), so it contributes nothing — the
free EU FSF / UN Consolidated List screeners carry that coverage instead.

The Finoscale Data API also exposes a Zigram screening endpoint; the client
supports it (`vdd/finoscale_api/client.py`) but `vdd/pipeline.py` does not
call it, and no resolver reads its result. Live testing found its composite
verdict didn't reconcile with the one check block it actually returned data
for — see the `Zigram status` note in `resolve_aml`'s docstring
(`vdd/resolve/resolvers.py`) for the full investigation. Re-enabling it needs
an answer from whoever manages the Finoscale/Zigram account first.

## Setup

```
pip install -r requirements.txt
cp .env.example .env   # fill in FINOSCALE_API_KEY
```

**Windows PDF rendering note:** `weasyprint` needs the GTK3 runtime
(Pango/Cairo/GObject) to render PDFs, which isn't present by default on
Windows. Without it, `run_vendor.py` still produces the `.html` report and
prints a warning instead of failing. To get PDF output, install the GTK3
runtime for Windows (e.g. the installer from
`https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer`)
and re-run, or use WSL/Docker where GTK3 is a normal package install.

## Usage

```
python run_vendor.py --docs "path/to/vendor folder" --out out/
python run_vendor.py --docs "..." --out out/ --no-api        # doc-extraction only, skip Finoscale API calls
python run_vendor.py --docs "..." --scoring-model config/scoring_model.json --cache-dir cache
```

`--cache-dir` (default `cache/`) caches both Finoscale API responses and OCR
output across runs. Optional OCR fallbacks (`vdd/extract/ocr.py`) degrade
gracefully when unavailable: Tesseract needs the binary on `PATH`
(`pytesseract`), and the vision fallback needs `ANTHROPIC_API_KEY` set.

## Layout

- `vdd/pipeline.py` — end-to-end orchestration: folder → extracted entity → API calls → resolved values → scored, rendered report. Never pauses for human input; every field resolves to a value or to "unresolved" with a reason, and anything resolved via inference or a judgment call is flagged in `cross_check_items` for post-hoc human review instead of blocking the run.
- `vdd/extract/` — filename classification, OCR/text extraction, per-doc-type regex parsers
- `vdd/finoscale_api/` — typed client for the internal Data API
- `vdd/resolve/` — maps extracted docs + API responses to scoring-model canonical values
- `vdd/score/` — generic scoring-model evaluator (expr-based, hard-reject aware)
- `vdd/aml/screening.py` — OFAC / UN / EU FSF / World Bank sanctions sweep (`legal_sanctions`)
- `vdd/aml/india_legal.py` — India-specific registers: DRT/SARFAESI + PEP; documents the eCourts and RBI-wilful-defaulter blockers
- `vdd/report/` — assembles report context and renders HTML/PDF
- `config/scoring_model.json` — Finoscale Generic Scoring Model v8.0
