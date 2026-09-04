---
name: "finoscale-vdd-report"
description: "Convert a Finoscale VDD (Vendor Due Diligence) .docx into a branded, single-page \"Finoscale Seller Intelligence Report\" PDF. Use whenever the user provides a VDD .docx (or a folder of them) and asks to \"generate the report\", \"make the PDF\", \"make it like the other reports\", or re-run a report after editing a docx. Triggers: VDD report, seller intelligence report, Finoscale report, scorebar docx."
---

# Finoscale VDD Report Generator

Turns a VDD `.docx` into the branded Finoscale Seller Intelligence Report PDF
(dark header, Finoscale Basic Score out of 100, unlock cards, findings grid,
full KYC scoring table) sized to exactly one page with no footer gap.

## When to use
- The user shares one or more VDD `.docx` files and wants the branded PDF.
- The user edited a docx and asks to re-run it.
- The user asks to change a score and re-render.

## Requirements
```
pip install python-docx weasyprint pdf2image --break-system-packages
# poppler-utils must be installed (provides pdftoppm for pdf2image)
```

## How to run (one file)
```
python3 scripts/vdd_report.py "<path/to/FIRM - VDD Report.docx>" --out "<output_dir>" --html-only
```
- Prints the score breakdown (COM + POA + POI + AML = total) — **always confirm this with the user before/after generating.**
- Always use `--html-only`. Do **not** rely on the script's own built-in `fix_css`/PDF
  render — it uses an older, more conservative CSS-fixing approach. Instead, apply the
  `fix_css_v6` function below yourself to the generated HTML, then run your own
  tight-height-search render (see "Tight-height search" below). This is required to get
  the current styling (header glow, brighter colors, no gradient-corruption bugs).

## How to run (a folder / batch)
Loop over each `.docx`, running the command once per file. Render one file per
step to stay within execution time limits.

## What the pipeline does
1. **Parse & verify score** — reads the Basic Score table (Compliance/Proof of
   Address/Proof of Identity/Legal-AML). The total is the sum of these four
   (the `Finoscale Score` row, when present, is authoritative). Two docx layouts
   are supported: with a dedicated `Finoscale Score` table, and without it.
   Handles both Proprietor (legal name ≠ trade name) and Company entities.
2. **Build HTML** — the script maps docx tables into the branded template.
3. **Apply `fix_css_v6`** (below) to the raw HTML — do this yourself in a Python
   step, not via the script's internal fix_css.
4. **Tight-height search** — render at decreasing page heights (coarse −120px,
   then fine −30px) until the smallest height that is still ONE page is found
   (typically 3300–4200px). Never leave a footer gap. Render one height per bash
   call to stay under the 45s timeout.
5. **Save** — `FIRM_NAME_VDD_Report.pdf` (and `.html`) in a firm-named subfolder,
   same location as the input docx.

## `fix_css_v6` — CSS-fixing function (apply to every HTML before rendering)

Two real weasyprint bugs drove this version (confirmed via isolation testing on
2026-07-24, test firm SAJAD SCRAP SHOP):
1. `radial-gradient()` is never rendered by weasyprint — not even a plain solid-color
   one. This is why the header's `.hdr::before` glow needs a `linear-gradient()`
   approximation instead of being disabled.
2. The large KYC "Compliance & Scoring Detail" annex table (dozens of repeated
   `.code-tag` / `.section-header-row` elements) trips a weasyprint resource-limit bug
   that corrupts *unrelated* CSS gradients elsewhere on the same page to a muddy
   brown/gray — even though those same gradients render correctly in isolation or in
   a shorter document. This is why the "Scope of Improvement" bar is built from real
   DOM `<span>` dashes instead of a CSS gradient pattern — that's what makes it immune
   to the bug regardless of total page complexity.

```python
import re

def fix_css_v6(c):
    c = c.replace("--mono: 'DM Mono', monospace; --serif: 'Playfair Display', serif; --sans: 'DM Sans', sans-serif;",
                  "--mono: 'Noto Sans Mono',monospace; --sans: 'Lato',Arial,sans-serif;")

    GLOW = "linear-gradient(115deg, rgba(25,175,160,.45) 0%, rgba(30,41,59,0) 35%, rgba(30,41,59,0) 55%, rgba(55,125,255,.7) 100%)"

    # Header glow (radial-gradient not rendered by weasyprint -> linear-gradient approximation)
    c = re.sub(
        r'\.hdr::before\s*\{[^}]*radial-gradient[^}]*\}',
        ".hdr::before { content:''; position:absolute; inset:0; background: " + GLOW + "; }",
        c
    )

    # .prog-wrap: base stays solid (proven safe -- keeping it a gradient washes the
    # section out), brightness added via a ::before overlay instead (same technique as header).
    c = c.replace("background: linear-gradient(135deg,#0F172A,#1E293B)", "background: #131C2E")
    c = re.sub(
        r'\.prog-wrap\s*\{([^}]*)\}',
        lambda m: '.prog-wrap { position:relative; overflow:hidden;' + m.group(1) + '}'
                  ' .prog-wrap::before { content:\'\'; position:absolute; inset:0; background: ' + GLOW + '; }'
                  ' .prog-wrap > * { position:relative; }',
        c
    )
    c = re.sub(
        r'\.unlock-sec\s*\{([^}]*)\}',
        lambda m: '.unlock-sec { position:relative; overflow:hidden;' + m.group(1) + '}'
                  ' .unlock-sec::before { content:\'\'; position:absolute; inset:0; background: ' + GLOW + '; }'
                  ' .unlock-sec > * { position:relative; }',
        c
    )

    # This exact gradient (used by .unlock-badge, .unlock-card.partial::before, .unlock-cta-arrow)
    # renders correctly in isolation but corrupts to muddy brown once the large KYC table is
    # also present on the page. Flatten to solid (matches original production behavior for
    # this specific string already).
    c = c.replace("background:linear-gradient(135deg,#D97706,#F59E0B)", "background:#E8930A")

    # --- Green (Current Score bar + legend dot) ---
    c = c.replace("linear-gradient(90deg,#059669,#34D399)", "#3B9154")

    # --- Amber/orange (Scope of Improvement + unlock cards + pending tags) ---
    # DOM-based dashes instead of a CSS gradient pattern -- immune to the KYC-table
    # corruption bug regardless of total page complexity. Turn .prog-bar-scope into a
    # flex row of solid-color dash elements.
    c = c.replace(
        "background:repeating-linear-gradient(90deg,rgba(251,191,36,.4) 0px,rgba(251,191,36,.4) 5px,rgba(251,191,36,.1) 5px,rgba(251,191,36,.1) 10px)",
        "background:#6B4E2A; display:flex; align-items:center; overflow:hidden;"
    )
    dash = '<span style="flex:0 0 8px;height:100%;background:#FFB800;margin-right:6px;border-radius:1px;"></span>'
    dashes_html = dash * 60
    c = re.sub(
        r'(<div class="prog-bar-scope"[^>]*>)(</div>)',
        lambda m: m.group(1) + dashes_html + m.group(2),
        c
    )
    # Legend dot icon: was a repeating-gradient stripe -- switch to plain solid gold
    # (same fragile gradient-pattern class; not worth the risk for a 10x10px icon).
    c = re.sub(
        r'background:repeating-linear-gradient\(90deg,rgba\(251,191,36,\.5\)[^)]*\)[^"]*',
        'background:#FFB800',
        c
    )
    # Pending-tag text color: brighten slightly
    c = c.replace("color: #FBBF24; margin-top: 5px;", "color: #FFC93C; margin-top: 5px;")

    # Unlock pts-pill (non-partial): 12%/30% translucent -> brighter solid
    c = c.replace(
        "background:rgba(251,191,36,.12); border:1px solid rgba(251,191,36,.3); color:#FBBF24;",
        "background:#3D2E00; border:1px solid #F0B429; color:#FFC93C;"
    )
    # Unlock pts-pill partial: 12%/30% -> brighter solid
    c = c.replace(
        "background:rgba(217,119,6,.12); border-color:rgba(217,119,6,.3); color:#F59E0B;",
        "background:#3D2200; border-color:#E8930A; color:#FFA733;"
    )
    # Unlock CTA button bg/border: 8%/20% -> lighter brown (matches dash-gap tone)
    c = c.replace(
        "background:rgba(251,191,36,.08); border:1px solid rgba(251,191,36,.2);",
        "background:#6B4E2A; border:1px solid #B8860B;"
    )
    # .pending-card was only ever a 4%/20% barely-there tint by design -- keep it subtle,
    # do NOT brighten it the same way as the pts-pill/CTA (that was tried once and was
    # an over-correction -- it should stay a faint amber-tinted dark card, not solid brown).
    c = c.replace("border-color: rgba(251,191,36,.2); background: rgba(251,191,36,.04);",
                  "border-color: #4A4736; background: #282F3A;")
    c = c.replace('style="color:rgba(255,255,255,.35);white-space:nowrap"', 'style="color:#D4A800;white-space:nowrap"')
    c = c.replace("color: rgba(255,255,255,.35)", "color: #7A8A9A")
    c = c.replace("color: rgba(255,255,255,.4)", "color: #8A9AAA")
    c = c.replace("background: rgba(255,255,255,.05);", "background: #243044;")
    c = c.replace("background: rgba(251,191,36,.04);", "background: #241C00;")

    c = re.sub(r'(\.ftr\s*\{[^}]*?)background\s*:\s*var\(--slate\)', r'\1background:#1E293B', c)
    c = re.sub(r'(\.section-header-row\s+td\s*\{[^}]*?)background\s*:\s*var\(--slate\)\s*!important', r'\1background:#1E293B !important', c)
    c = re.sub(r'\.chip\s*\{([^}]*?)border\s*:\s*1px solid\s*;([^}]*)\}',
               lambda m: '.chip {' + m.group(1) + 'display: inline-block; white-space: nowrap;' + m.group(2) + '}', c)
    c = re.sub(r'\.chip-teal\s*\{[^}]*\}', '.chip-teal { background: #0D4A40; border: 1.5px solid #0D4A40; color: #5EEAD4; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }', c)
    c = re.sub(r'\.chip-blue\s*\{[^}]*\}', '.chip-blue { background: #1A3A70; border: 1.5px solid #1A3A70; color: #93C5FD; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }', c)
    c = re.sub(r'\.chip-white\s*\{[^}]*\}', '.chip-white { background: #1E2D42; border: 1.5px solid #1E2D42; color: #B8C8D8; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }', c)
    c = re.sub(r'(\.brand-tag\s*\{[^}]*?color\s*:\s*)rgba\(255,255,255,\.45\)', r'\1#8A9AAA', c)
    c = re.sub(r'(\.brand-tag\s*\{)([^}]*)\}', lambda m: m.group(1) + m.group(2) + ' white-space: nowrap;}', c)
    c = c.replace('.unlock-card { background:#1E293B;', '.unlock-card { background:#1A2840;')
    return c
```

## Tight-height search
```python
import weasyprint
from pdf2image import convert_from_bytes

html_path = "path/to/file.html"
hp = html_path.rsplit('/', 1)[0] + '/'
c = fix_css_v6(open(html_path).read())

best_h, best_pdf = None, None
for h in range(4200, 3400, -120):
    css = f'@page {{ size: 960px {h}px; margin: 0; }} html,body{{width:960px;background:#F1F3F7;}}'
    pdf = weasyprint.HTML(string=c+f'<style>{css}</style>', base_url='file://'+hp).write_pdf(presentational_hints=True)
    pages = len(convert_from_bytes(pdf, dpi=36))
    if pages == 1:
        best_h = h; best_pdf = pdf
    else:
        if best_h: break

for h in range(best_h, best_h - 120, -30):
    css = f'@page {{ size: 960px {h}px; margin: 0; }} html,body{{width:960px;background:#F1F3F7;}}'
    pdf = weasyprint.HTML(string=c+f'<style>{css}</style>', base_url='file://'+hp).write_pdf(presentational_hints=True)
    pages = len(convert_from_bytes(pdf, dpi=36))
    if pages == 1:
        best_h = h; best_pdf = pdf
    else:
        if best_h: break

open(out_pdf, 'wb').write(best_pdf)
```
Run one height-range per bash call to stay under the 45s timeout. Verify the final
height is truly tight by confirming `height - 30` would overflow to 2 pages.

## Score → colour rule (score cards)
green ≥ 75% · amber ≥ 45% · red below 45%. Scope of improvement = 100 − score.

## Findings & unlock cards
Findings are auto-derived from the Checks table (positive results = green
confirmed, problems like delays / mismatches / "not available" = amber note).
The free-text OBSERVATIONS in the docx are NOT pulled in. Unlock cards are the 3
pending advanced areas (On-Site +20, GST 3B&2B +25, ITR +5) plus a partial card
for each real gap (filing delays → Compliance; weak address proof → PoA;
PAN–GST mismatch → Identity).

## Editing a score afterwards
If the user wants a different score, edit the value in the docx and re-run, or
edit the HTML (`--html-only` output) and re-render. Keep every occurrence in sync:
big number, score card + bar width, annex section header, progress legend,
scope (+N), unlock badge (+N). Then re-run the tight-height search.

## Penny-drop / bank check
If penny-drop receipt PDFs are provided (e.g. a `support doc file` folder),
"pass" = a ₹1 credit shown as paid/Completed with a returned banking name; flag
loose name matches (spelling differences, dropped words) even when it passes.

## Quick color reference
| Name | Hex |
|---|---|
| Header/prog-wrap/unlock-sec glow — teal end | `rgba(25,175,160,.45)` |
| Header/prog-wrap/unlock-sec glow — blue end | `rgba(55,125,255,.7)` |
| Current Score green | `#3B9154` |
| Scope of Improvement gold (dash) | `#FFB800` |
| Scope of Improvement brown (gap) | `#6B4E2A` |
| Orange badge / CTA arrow / partial-stripe | `#E8930A` |
| CTA button background | `#6B4E2A` |
| CTA button border | `#B8860B` |
| Pts-pill background | `#3D2E00` |
| Pts-pill border | `#F0B429` |
| Pts-pill text | `#FFC93C` |

## Reference
See `assets/template_head.html` for the full CSS/design tokens. A human-readable
design spec is also reproduced in the project's `VDD_REPORT_BUILD_SPEC.md`. Full
changelog and reasoning for the `fix_css_v6` changes above is in the project's
`VDD_Style_Fix_Changelog.md`.

