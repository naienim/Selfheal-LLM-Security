from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from selfheal.evaluator import EvalResult

OBJECTIVES = ("weighted", "f1")


@dataclass
class Decision:
    """Keep or roll back a candidate, with the reason."""

    accepted: bool
    reason: str
    asr_change: str
    fpr_change: str
    d_hits: int
    d_fp: int
    objective_current: float
    objective_candidate: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def objective(result: "EvalResult", search_cfg: dict[str, Any]) -> float:
    kind = search_cfg.get("objective", "weighted")
    if kind == "weighted":
        return result.asr_macro + float(search_cfg.get("fpr_weight", 1.0)) * result.fpr
    if kind == "f1":
        recall = 1.0 - result.asr_macro
        denominator = recall + result.fpr
        precision = recall / denominator if denominator > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall > 0 else 0.0
        return 1.0 - f1
    raise ValueError(f"search.objective must be one of {OBJECTIVES}, not {kind!r}")


def _change(delta: int, min_delta: int) -> str:
    if delta <= -min_delta:
        return "better"
    if delta >= min_delta:
        return "worse"
    return "same"


def decide(current: "EvalResult", candidate: "EvalResult", search_cfg: dict[str, Any],
           prompt_change: bool = False) -> Decision:
    """Apply the acceptance table to a paired comparison."""
    if (current.split != candidate.split or current.attack_total != candidate.attack_total
            or current.benign_n != candidate.benign_n):
        raise ValueError("Paired comparison needs both results on the same split")
    min_attacks = int(search_cfg["min_delta_attacks_prompt"] if prompt_change
                      else search_cfg["min_delta_attacks"])
    min_benign = int(search_cfg["min_delta_benign"])
    d_hits = candidate.attack_hits - current.attack_hits
    d_fp = candidate.benign_fp - current.benign_fp
    asr = _change(d_hits, min_attacks)
    fpr = _change(d_fp, min_benign)
    obj_cur = objective(current, search_cfg)
    obj_cand = objective(candidate, search_cfg)

    def make(accepted: bool, reason: str) -> Decision:
        return Decision(accepted, reason, asr, fpr, d_hits, d_fp, obj_cur, obj_cand)

    max_fpr = float(search_cfg["max_fpr"])
    if candidate.fpr > max_fpr + 1e-9:
        return make(False, f"FPR {candidate.fpr:.1%} above max_fpr {max_fpr:.1%}")
    if asr == "worse":
        return make(False, f"ASR worse ({d_hits:+d} successful attacks)")
    if asr == "better" and fpr in ("better", "same"):
        return make(True, f"ASR better ({d_hits:+d} attacks), FPR {fpr} ({d_fp:+d})")
    if asr == "better":
        return make(True, f"ASR better ({d_hits:+d} attacks); FPR worse ({d_fp:+d}) "
                          f"but within max_fpr")
    if fpr == "better":
        return make(True, f"FPR better ({d_fp:+d} false positives) at the same ASR")
    if fpr == "worse":
        return make(False, f"FPR worse ({d_fp:+d}) without an ASR gain")
    return make(False, f"no meaningful change (attacks {d_hits:+d}, false positives {d_fp:+d})")