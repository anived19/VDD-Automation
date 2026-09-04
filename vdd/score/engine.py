"""
Generic evaluator for `config/scoring_model.json` (Finoscale Generic Scoring
Model v8.0). Deliberately does NOT use `eval()` on the model's `expr`
strings -- they come from a config file, but a small dedicated parser is
still safer and keeps the supported syntax explicit.

Supported expr forms (all seen in the actual model):
    value == "literal"        string equality (case-insensitive)
    value == 100               numeric equality
    value < N | > N | <= N | >= N
    value IN [a, b] | (a, b] | [a, b) | (a, b)      inclusive/exclusive range
"""
import json
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional


class ExprError(ValueError):
    pass


_EQ_STR = re.compile(r'^value\s*==\s*"([^"]*)"$')
_EQ_NUM = re.compile(r'^value\s*==\s*(-?\d+\.?\d*)$')
_CMP = re.compile(r'^value\s*(<=|>=|<|>)\s*(-?\d+\.?\d*)$')
_RANGE = re.compile(r'^value\s+IN\s+([\[\(])\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*([\]\)])$')


def eval_expr(expr: str, value: Any) -> bool:
    if value is None:
        return False
    m = _EQ_STR.match(expr)
    if m:
        return str(value).strip().lower() == m.group(1).strip().lower()
    m = _EQ_NUM.match(expr)
    if m:
        try:
            return float(value) == float(m.group(1))
        except (TypeError, ValueError):
            return False
    m = _CMP.match(expr)
    if m:
        op, num_s = m.group(1), m.group(2)
        try:
            v, num = float(value), float(num_s)
        except (TypeError, ValueError):
            return False
        return {"<": v < num, ">": v > num, "<=": v <= num, ">=": v >= num}[op]
    m = _RANGE.match(expr)
    if m:
        lo_b, lo_s, hi_s, hi_b = m.groups()
        try:
            v, lo, hi = float(value), float(lo_s), float(hi_s)
        except (TypeError, ValueError):
            return False
        lo_ok = v >= lo if lo_b == "[" else v > lo
        hi_ok = v <= hi if hi_b == "]" else v < hi
        return lo_ok and hi_ok
    raise ExprError(f"Unrecognized scoring expr: {expr!r}")


@dataclass
class ParamScore:
    parameter_id: str
    parameter_name: str
    value: Optional[Any]
    matched_condition: Optional[str]
    assigned_score: float
    max_score: float          # 0 if unresolved-and-conditional (excluded, not penalized)
    unresolved: bool = False
    note: str = ""             # resolver's Resolved.note -- evidence/source detail for the report


@dataclass
class CategoryScore:
    category_id: str
    category_name: str
    earned: float
    max_score: float          # dynamic: sum of params actually counted this run
    params: List[ParamScore] = field(default_factory=list)


@dataclass
class ScoreResult:
    total: float
    max_total: float
    categories: List[CategoryScore]
    grade_band: Optional[dict]


NO_CONSENT_CATEGORY_IDS = {"compliance", "proof_of_address", "proof_of_identity", "legal_aml"}


class ScoringEngine:
    def __init__(self, model_path: str):
        with open(model_path, encoding="utf-8") as f:
            self.model = json.load(f)

    def _score_category(self, category: dict, resolved: dict) -> CategoryScore:
        params: List[ParamScore] = []
        for p in category["parameters"]:
            pid = p["parameterId"]
            r = resolved.get(pid)
            is_conditional = p.get("conditional", False)

            if r is None or getattr(r, "unresolved", True) or r.value is None:
                # Conditional/N-A params that couldn't be resolved are excluded
                # entirely (max_score=0) rather than penalized -- they may
                # genuinely not apply (e.g. no PF-registered employees).
                # Required params that couldn't be resolved score 0 against
                # their full max: missing data is never given the benefit of
                # the doubt in a KYC/compliance context.
                max_score = 0.0 if is_conditional else p["maxParameterScore"]
                note = r.note if r is not None else ""
                params.append(ParamScore(pid, p["parameterName"], None, None, 0.0, max_score,
                                          unresolved=True, note=note))
                continue

            matched = next((sm for sm in p["scoreMappings"] if eval_expr(sm["expr"], r.value)), None)
            if matched is None:
                max_score = 0.0 if is_conditional else p["maxParameterScore"]
                params.append(ParamScore(pid, p["parameterName"], r.value, None, 0.0, max_score,
                                          unresolved=True, note=r.note))
                continue

            params.append(ParamScore(
                pid, p["parameterName"], r.value, matched["condition"],
                matched["assignedScore"], p["maxParameterScore"], note=r.note,
            ))

        earned = sum(p.assigned_score for p in params)
        max_score = sum(p.max_score for p in params)
        return CategoryScore(category["categoryId"], category["categoryName"], earned, max_score, params)

    def score_no_consent(self, resolved: dict) -> ScoreResult:
        """Score only the 4 categories that don't require GST-portal consent
        (Compliance/POA/POI/AML). On-Site Verification / 3B / 2B / ITR stay
        at 0/"Pending" for v1 -- callers render those separately, unchanged."""
        cats = [c for c in self.model["categories"] if c["categoryId"] in NO_CONSENT_CATEGORY_IDS]
        cat_scores = [self._score_category(c, resolved) for c in cats]
        total = sum(c.earned for c in cat_scores)

        # Grade bands are defined against the full /100 scale, and existing
        # reports already display a genuinely partial no-consent-only total
        # against /100 (matches vdd_report.py's `score = com+poa+poi+aml`) --
        # no rescaling here, a partial score legitimately lands in a lower band.
        # No hard-reject override: the live Finoscale scoring model (confirmed
        # against the platform's own parameter view) scores every condition,
        # including sanctions hits / cancelled GST / PAN mismatches, as a plain
        # numeric value -- the grade band comes purely from the total score.
        band = None
        for g in sorted(self.model["grades"], key=lambda g: -g["minScore"]):
            if total >= g["minScore"]:
                band = g
                break

        return ScoreResult(
            total=total,
            max_total=sum(c.max_score for c in cat_scores),
            categories=cat_scores,
            grade_band=band,
        )
