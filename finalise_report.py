"""
CLI: rebuild a vendor's report from the documents plus an analyst's edited
review workbook (the <VENDOR>_review.xlsx that run_vendor.py wrote).

    python finalise_report.py --docs "path/to/vendor folder" --sheet out/VENDOR_review.xlsx --out out/

The deterministic pipeline re-runs (OCR + cached API calls); the LLM review
does NOT -- its corrections are carried in the sheet. Analyst values win over
everything. A sheet with an invalid value stops here with a list of what to
fix; nothing is generated in that case.
"""
import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from vdd.finoscale_api.client import FinoscaleClient
from vdd.pipeline import finalise_vendor
from vdd.review_sheet import ReviewSheetError

load_dotenv()
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--docs", required=True, help="Path to the vendor's document folder (same one the report was run on)")
    ap.add_argument("--sheet", required=True, help="The edited <VENDOR>_review.xlsx")
    ap.add_argument("--out", default="out", help="Output directory (default: out/)")
    ap.add_argument("--analyst", default=None, help="Analyst name for the audit trail (default: the sheet's Meta cell)")
    ap.add_argument("--scoring-model", default="config/scoring_model.json")
    ap.add_argument("--no-api", action="store_true", help="Skip Finoscale Data API calls")
    ap.add_argument("--cache-dir", default="cache")
    ap.add_argument("--cache-max-age", type=float, default=7.0, metavar="DAYS",
                     help="Refetch cached API responses older than this many days (default 7; 0 = never expire)")
    ap.add_argument("--fresh", action="store_true", help="Ignore the API cache entirely this run")
    ap.add_argument("--html", action="store_true", help="Also write the report as HTML")
    args = ap.parse_args()

    client = None
    if not args.no_api:
        api_key = os.environ.get("FINOSCALE_API_KEY")
        if api_key:
            client = FinoscaleClient(api_key=api_key, base_url=os.environ.get("FINOSCALE_API_BASE", "https://api-ppe.finoscale.ai"),
                                     cache_dir=args.cache_dir,
                                     cache_max_age_days=(0.0001 if args.fresh else args.cache_max_age))
        else:
            print("[warn] FINOSCALE_API_KEY not set -- running doc-extraction only.", file=sys.stderr)

    try:
        result = finalise_vendor(args.docs, args.sheet, args.out, client=client, scoring_model_path=args.scoring_model,
                                 ocr_cache_dir=args.cache_dir, analyst=args.analyst, write_html=args.html)
    except ReviewSheetError as e:
        print(f"\n{e}\n\nFix the cells above in the sheet and submit it again. No report was generated.", file=sys.stderr)
        sys.exit(2)

    print(f"\n=== {result.vendor_name} (finalised from review sheet) ===")
    print(f"Score: {result.score}/100")
    if result.pdf_path:
        print(f"PDF:   {result.pdf_path}")
    if result.html_path:
        print(f"HTML:  {result.html_path}")
    if result.sheet_path:
        print(f"Updated review sheet: {result.sheet_path}")
    analyst = [c for c in result.corrections_applied if c.get("source") == "analyst"]
    carried = [c for c in result.corrections_applied if c.get("source") == "llm-review"]
    if analyst:
        print(f"\nAnalyst changes applied ({len(analyst)}):")
        for c in analyst:
            print(f"  - {c.get('parameter_id') or c.get('field')}: {c['before'].get('value')!r} -> {c.get('proposed_value')!r}"
                  + (f"  ({c['proposed_note']})" if c.get("proposed_note") else ""))
    if carried:
        print(f"\nLLM-review corrections carried forward ({len(carried)}): "
              + ", ".join(c.get("parameter_id") or c.get("field") for c in carried))
    if result.unresolved_fields:
        print(f"\nUnresolved scoring fields ({len(result.unresolved_fields)}): {', '.join(result.unresolved_fields)}")
    if result.cross_check_items:
        print(f"\nNeeds manual cross-check ({len(result.cross_check_items)}):")
        for item in result.cross_check_items:
            print(f"  - {item}")
    if result.api_errors:
        print("\nAPI errors:")
        for e in result.api_errors:
            print(f"  - {e}")
    if result.extraction_warnings:
        print("\nWarnings:")
        for w in result.extraction_warnings:
            print(f"  - {w}")


if __name__ == "__main__":
    main()
