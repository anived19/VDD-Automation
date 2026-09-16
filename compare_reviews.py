"""
CLI: compare LLM review traces (out/*_review_trace.json) side by side --
the same vendor reviewed by different providers/models/prompt versions.

    python compare_reviews.py out/*SKANDAN*_review_trace.json
    python compare_reviews.py out/a.json out/b.json --expect com_bank_verification,legal_sanctions

Answers "is this reviewer any good" with numbers instead of eyeballing:
  - recall against --expect: the parameter ids a competent reviewer MUST
    flag for this vendor (take them from run_vendor.py's own "Needs manual
    cross-check" list plus anything the analyst's reference report caught)
  - what it actually did: passes, model calls, which tools, findings by
    action/confidence, corrections vs escalations
  - hallucination signal: findings marked confidence='verified' in a pass
    that made no tool call at all -- the system prompt forbids exactly that
  - cost: tokens in/out (usage_metadata as reported by the provider)
"""
import argparse
import collections
import json
import os


def summarize(path: str, expect: set[str]) -> dict:
    d = json.load(open(path, encoding="utf-8"))
    tools = collections.Counter()
    verified_without_tool = 0
    for pass_msgs, ps in zip(d.get("message_traces", []), d.get("passes", [])):
        pass_tools = collections.Counter(tc["name"] for m in pass_msgs if m["type"] == "ai" for tc in m["tool_calls"])
        tools.update(pass_tools)
        real_calls = sum(v for k, v in pass_tools.items() if k != "ReviewReport")
        if real_calls == 0:
            verified_without_tool += sum(1 for f in ps["findings"] if f.get("confidence") == "verified")
    findings = [f for ps in d.get("passes", []) for f in ps["findings"]]
    flagged = {f.get("parameter_id") or f.get("field") for f in findings}
    usage = d.get("token_usage", {}).get("total", {})
    return {
        "file": os.path.basename(path), "provider": d.get("provider"),
        "passes": d.get("iterations_run"), "approved": d.get("approved"),
        "model_calls": tools.get("ReviewReport", 0) + sum(v for k, v in tools.items() if k != "ReviewReport"),
        "tools": {k: v for k, v in tools.items() if k != "ReviewReport"},
        "findings": len(findings),
        "by_action": dict(collections.Counter(f.get("action") for f in findings)),
        "by_confidence": dict(collections.Counter(f.get("confidence") for f in findings)),
        "corrections": len(d.get("corrections_applied", [])), "escalations": len(d.get("escalations", [])),
        "flagged": sorted(p for p in flagged if p),
        "expected_hit": sorted(expect & flagged), "expected_missed": sorted(expect - flagged),
        "verified_without_tool": verified_without_tool,
        "tokens_in": usage.get("input_tokens"), "tokens_out": usage.get("output_tokens"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("traces", nargs="+")
    ap.add_argument("--expect", default="", help="comma-separated parameter ids a good reviewer must flag")
    args = ap.parse_args()
    expect = {p.strip() for p in args.expect.split(",") if p.strip()}

    rows = [summarize(p, expect) for p in args.traces]
    for r in rows:
        print(f"\n{r['file']}")
        print(f"  provider={r['provider']}  passes={r['passes']}  approved={r['approved']}  "
              f"model_calls={r['model_calls']}  tokens={r['tokens_in']} in / {r['tokens_out']} out")
        print(f"  tools: {r['tools'] or '(none)'}")
        print(f"  findings={r['findings']}  by_action={r['by_action']}  by_confidence={r['by_confidence']}  "
              f"corrections={r['corrections']}  escalations={r['escalations']}")
        print(f"  flagged: {', '.join(r['flagged']) or '(nothing)'}")
        if expect:
            print(f"  RECALL {len(r['expected_hit'])}/{len(expect)}  missed: {', '.join(r['expected_missed']) or '-'}")
        if r["verified_without_tool"]:
            print(f"  !! {r['verified_without_tool']} finding(s) marked 'verified' in a pass with zero tool calls")


if __name__ == "__main__":
    main()
