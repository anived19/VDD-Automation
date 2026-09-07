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

After the deterministic report is built, an LLM review loop
(`vdd/review/`, LangGraph) reads it, independently re-checks anything it's
suspicious of via tools, and corrects (with cited, verified evidence) or
escalates findings for the analyst — looping under its own judgment, up to
a safety cap, before the final report is written. See "LLM review loop"
below.

### Parameters that stay manual, and why

`com_bank_verification` (COM-07, 5 pts) is now live-verified: `fetch_api_data`
calls Ongrid's real bank-verification penny-drop endpoint
(`ongrid_bank_verification_verify`, `vdd/finoscale_api/client.py`) whenever a
bank account number + IFSC were extracted, and
`resolve_com_bank_verification` (`vdd/resolve/resolvers.py`) scores off its
result (matching the returned account-holder name against the GST-registered
legal name). The GST portal's own Bank Account Status is only the fallback
signal now, used when no account/IFSC could be extracted or the live call
itself errored.

These three resolve to an explicit "not scored" with the blocker spelled out
in the report, rather than being assumed clean. Each is a documented finding
from a live investigation of the source, not an assumption:

| Parameter | Blocker |
|---|---|
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
python run_vendor.py --docs "..." --out out/ --no-review     # skip the LLM review loop
python run_vendor.py --docs "..." --scoring-model config/scoring_model.json --cache-dir cache
```

`--cache-dir` (default `cache/`) caches both Finoscale API responses and OCR
output across runs. The optional OCR fallback (`vdd/extract/ocr.py`) degrades
gracefully when unavailable: Tesseract needs the binary on `PATH`
(`pytesseract`). No document image/bytes are ever sent to any LLM — there is
deliberately no vision-based OCR fallback tier.

## LLM review loop

`vdd/review/` (LangGraph) runs automatically whenever `GEMINI_API_KEY` (or
`OPENAI_API_KEY`, see below) is set and `--no-review` wasn't passed — it
degrades to a clear console note and the deterministic-only report
otherwise, the same graceful-degrade idiom used everywhere else in this
pipeline. It reads the rendered report plus the resolved parameter values
and `cross_check_items`, uses tools (re-run a sanctions/PEP/DRT-SARFAESI
screen, re-fetch a live GSTIN/bank-verification result, web search) to
independently check anything it's suspicious of, and either corrects a
finding — only with verified, cited evidence, patching the underlying
`resolved`/`entity` values so the deterministic scorer/renderer regenerates
the report, never by editing HTML directly — or escalates it for the
analyst. It loops under its own judgment (a `verdict` it sets each pass,
not a "ran out of things to fix" heuristic) until it approves the report or
a safety cap (`--max-review-iterations`, default 4) is hit. Every pass's
findings, tool calls, and corrections are written to a local
`<VENDOR>_review_trace.json` file next to the report — never transmitted
anywhere.

**Provider**: Gemini (`gemini-3.5-flash-lite`) is the tested path. Set
`OPENAI_API_KEY` (and `OPENAI_MODEL`) instead to swap providers — see
`vdd/review/model.py` — but that path is untested; no OpenAI key was
available while building it.

**Tracing must stay off.** This pipeline handles consented but highly
sensitive personal financial/KYC data — LangChain/LangSmith tracing must
never be enabled. Never set `LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`, or
any `LANGSMITH_*` variable; `vdd/review/__init__.py` raises immediately at
import time if it detects tracing has been turned on. (The `langsmith`
package itself will still show up in `pip freeze` — it's a required
transitive dependency of `langchain-core`, unavoidable while using
LangChain/LangGraph at all — but it makes no network call on its own; it's
only ever invoked if the env vars above are set, which the guard blocks.)
The local review trace file above is the supported audit mechanism
instead.

## Layout

- `vdd/pipeline.py` — end-to-end orchestration: folder → extracted entity → API calls → resolved values → scored, rendered report. Never pauses for human input; every field resolves to a value or to "unresolved" with a reason, and anything resolved via inference or a judgment call is flagged in `cross_check_items` for post-hoc human review instead of blocking the run.
- `vdd/extract/` — filename classification, OCR/text extraction, per-doc-type regex parsers
- `vdd/finoscale_api/` — typed client for the internal Data API
- `vdd/resolve/` — maps extracted docs + API responses to scoring-model canonical values
- `vdd/score/` — generic scoring-model evaluator (expr-based, hard-reject aware)
- `vdd/aml/screening.py` — OFAC / UN / EU FSF / World Bank sanctions sweep (`legal_sanctions`)
- `vdd/aml/india_legal.py` — India-specific registers: DRT/SARFAESI + PEP; documents the eCourts and RBI-wilful-defaulter blockers
- `vdd/report/` — assembles report context and renders HTML/PDF
- `vdd/review/` — LangGraph LLM review loop over the deterministic report (see "LLM review loop" above)
- `config/scoring_model.json` — Finoscale Generic Scoring Model v8.0
