"""
CLI: generate a VDD report for one vendor's document folder.

Usage:
    python run_vendor.py --docs "path/to/vendor folder" --out out/
    python run_vendor.py --docs "..." --out out/ --no-api      # doc-extraction only, skip Finoscale API calls
"""
import argparse
import os
import sys

from dotenv import load_dotenv

from vdd.finoscale_api.client import FinoscaleClient
from vdd.pipeline import run_vendor

load_dotenv()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--docs", required=True, help="Path to the vendor's document folder")
    ap.add_argument("--out", default="out", help="Output directory for the report (default: out/)")
    ap.add_argument("--scoring-model", default="config/scoring_model.json")
    ap.add_argument("--no-api", action="store_true", help="Skip Finoscale Data API calls (doc extraction only)")
    ap.add_argument("--cache-dir", default="cache", help="Directory to cache API responses (default: cache/)")
    args = ap.parse_args()

    client = None
    if not args.no_api:
        api_key = os.environ.get("FINOSCALE_API_KEY")
        if not api_key:
            print("[warn] FINOSCALE_API_KEY not set -- running doc-extraction only. "
                  "Set it in .env or pass --no-api to silence this.", file=sys.stderr)
        else:
            base_url = os.environ.get("FINOSCALE_API_BASE", "https://api-ppe.finoscale.ai")
            client = FinoscaleClient(api_key=api_key, base_url=base_url, cache_dir=args.cache_dir)

    result = run_vendor(args.docs, args.out, client=client, scoring_model_path=args.scoring_model,
                         ocr_cache_dir=args.cache_dir)

    print(f"\n=== {result.vendor_name} ===")
    print(f"Score: {result.score}/100 (No-Consent categories only -- "
          f"On-Site Verification/3B/2B/ITR are always 'Pending' in v1)")
    if result.html_path:
        print(f"HTML:  {result.html_path}")
    if result.pdf_path:
        print(f"PDF:   {result.pdf_path}")

    if result.missing_documents:
        print(f"\nMissing document types: {', '.join(result.missing_documents)}")
    if result.unresolved_fields:
        print(f"\nUnresolved scoring fields ({len(result.unresolved_fields)}): "
              f"{', '.join(result.unresolved_fields)}")
    if result.cross_check_items:
        print(f"\nNeeds manual cross-check ({len(result.cross_check_items)}) -- resolved and scored, "
              f"but worth a human double-check; review after the fact, doesn't block generation:")
        for item in result.cross_check_items:
            print(f"  - {item}")
    if result.api_errors:
        print(f"\nAPI errors:")
        for e in result.api_errors:
            print(f"  - {e}")
    if result.extraction_warnings:
        print(f"\nExtraction warnings:")
        for w in result.extraction_warnings:
            print(f"  - {w}")


if __name__ == "__main__":
    main()
