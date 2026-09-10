"""
CLI: generate a VDD report for one vendor's document folder.

Usage:
    python run_vendor.py --docs "path/to/vendor folder" --out out/
    python run_vendor.py --docs "..." --out out/ --no-api      # doc-extraction only, skip Finoscale API calls
    python run_vendor.py --docs "..." --out out/ --no-review   # skip the LLM review loop
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
    ap.add_argument("--no-review", action="store_true",
                     help="Skip the LLM review loop regardless of GEMINI_API_KEY/QWEN_BASE_URL/OPENAI_API_KEY -- deterministic-only report")
    ap.add_argument("--max-review-iterations", type=int, default=None,
                     help="Safety cap on review passes (default: vdd.review.graph.DEFAULT_MAX_ITERATIONS)")
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
                         ocr_cache_dir=args.cache_dir, review=not args.no_review,
                         max_review_iterations=args.max_review_iterations)

    print(f"\n=== {result.vendor_name} ===")
    print(f"Score: {result.score}/100 (No-Consent categories only -- "
          f"On-Site Verification/3B/2B/ITR are always 'Pending' in v1)")
    if result.html_path:
        print(f"HTML:  {result.html_path}")
    if result.pdf_path:
        print(f"PDF:   {result.pdf_path}")

    if result.reviewed:
        status = "approved by the reviewer" if result.approved else "safety cap reached, not approved"
        print(f"\nLLM review: {result.review_iterations} pass(es), {status}")
        if result.token_usage:
            tu = result.token_usage
            print(f"LLM token usage: {tu['input_tokens']} in + {tu['output_tokens']} out = "
                  f"{tu['total_tokens']} total across {tu['llm_call_count']} call(s)")
        if result.review_trace_path:
            print(f"Review trace: {result.review_trace_path}")
        if result.corrections_applied:
            print(f"Corrections applied ({len(result.corrections_applied)}):")
            for c in result.corrections_applied:
                print(f"  - {c.get('parameter_id') or c.get('field')}: {c.get('issue')}")
        if result.escalations_for_human:
            print(f"Escalated for analyst review ({len(result.escalations_for_human)}):")
            for e in result.escalations_for_human:
                print(f"  - {e.get('parameter_id') or e.get('field')}: {e.get('issue')}")
    elif result.review_error:
        print(f"\nLLM review failed, deterministic-only report generated: {result.review_error}")

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
        print("\nAPI errors:")
        for e in result.api_errors:
            print(f"  - {e}")
    if result.extraction_warnings:
        print("\nExtraction warnings:")
        for w in result.extraction_warnings:
            print(f"  - {w}")


if __name__ == "__main__":
    main()
