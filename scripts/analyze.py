from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import matplotlib

matplotlib.use("Agg") 
import matplotlib.pyplot as plt 
import pandas as pd 

from selfheal.config import ( 
    PROJECT_ROOT,
    load_config,
    project_relative,
    resolve_project_path,
)
from selfheal.experiment import read_json 
from selfheal.final_test import format_final_table 
from selfheal.logging_setup import get_logger, setup_logging 
from selfheal.report_parser import AttemptRecord, load_trace, parse_report 

logger = get_logger("scripts.analyze")

EXPERIMENT_ORDER = ["baseline", "prompt_only", "output_only", "both", "healed", "healed_search"]
CONFIG_ORDER = ["baseline", "prompt_only", "output_only", "both", "healed", "healed_search"]
HEALED_PREFERENCE = ("healed_search", "healed")
EXCLUDED = {"smoke"}
README_START = "<!-- RESULTS:START -->"
README_END = "<!-- RESULTS:END -->"
NAN = float("nan")

LAYERS = ["input filter", "model / system prompt", "output filter", "got through"]
LAYER_COLORS = {"input filter": "tab:blue", "model / system prompt": "tab:green",
                "output filter": "tab:purple", "got through": "tab:red"}
FP_LAYERS = ["input filter", "output filter", "model refusal"]
FP_COLORS = {"input filter": "tab:blue", "output filter": "tab:purple", "model refusal": "tab:orange"}
THRESHOLD_KEYS = { 
    "classifier": ("input_filter.classifier.threshold", "input_filter.classifier.enabled",
                   "Input classifier threshold"),
    "classifier_min_words": ("input_filter.classifier.min_words", "input_filter.classifier.enabled",
                             "Input classifier min_words (length gate)"),
    "toxicity": ("output_filter.toxicity.threshold", "output_filter.toxicity.enabled",
                 "Output toxicity threshold"),
}


class SkipStep(Exception):
    """Raised when the data needed for a plot or table is missing."""





def pct(value: float | None) -> float:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return NAN
    return round(100.0 * float(value), 1)


def _ci_str(ci: list[float] | None) -> str:
    if not ci:
        return ""
    return f"{ci[0] * 100:.1f}–{ci[1] * 100:.1f}"


def _rate(rate: float, ci: list[float] | None = None) -> str:
    text = f"{rate * 100:.1f}%"
    return f"{text} [{ci[0] * 100:.1f}, {ci[1] * 100:.1f}]" if ci else text


def _overlap(a: list[float], b: list[float]) -> bool:
    return a[0] <= b[1] + 1e-12 and b[0] <= a[1] + 1e-12


def _macro(categories: dict[str, Any]) -> float:
    rates = [v["asr"] for v in categories.values() if v.get("total")]
    return sum(rates) / len(rates) if rates else 0.0


def _get(data: dict[str, Any], dotted: str) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _save(fig: Any, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def _dedupe_legend(ax: Any, **kwargs: Any) -> None:
    handles, labels = ax.get_legend_handles_labels()
    unique: dict[str, Any] = {}
    for handle, label in zip(handles, labels):
        unique.setdefault(label, handle)
    if unique:
        ax.legend(list(unique.values()), list(unique.keys()), **kwargs)


def _clip(text: str | None, limit: int) -> str:
    if text is None or text == "":
        return "(no output)"
    text = str(text).replace("```", "'''").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " [...]"


def _cell(value: Any) -> str:
    if isinstance(value, float) and math.isnan(value):
        return "–"
    return str(value).replace("|", "\\|").replace("\n", " ")


def df_to_markdown(df: pd.DataFrame, max_rows: int | None = None) -> str:
    """Markdown table without the optional `tabulate` dependency."""
    shown = df if max_rows is None or len(df) <= max_rows else df.head(max_rows)
    columns = [str(c) for c in shown.columns]
    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join(["---"] * len(columns)) + "|"]
    for _, row in shown.iterrows():
        lines.append("| " + " | ".join(_cell(v) for v in row.tolist()) + " |")
    if max_rows is not None and len(df) > max_rows:
        lines.append(f"\n_First {max_rows} of {len(df)} rows; the full table is in the CSV._")
    return "\n".join(lines)





def load_experiments(results_dir: Path) -> dict[str, dict[str, Any]]:
    """Latest summary.json of every old garak experiment, in a fixed order."""
    base = results_dir / "experiments"
    if not base.exists():
        return {}
    names = [n for n in EXPERIMENT_ORDER if (base / n / "summary.json").exists()]
    names += sorted(
        p.parent.name for p in base.glob("*/summary.json")
        if p.parent.name not in EXPERIMENT_ORDER and p.parent.name not in EXCLUDED
    )
    return {name: read_json(base / name / "summary.json") for name in names}


def _optional(path: Path) -> dict[str, Any] | None:
    return read_json(path) if path.exists() else None


@dataclass
class Context:
    """Everything the analysis needs, loaded once from saved files."""

    results_dir: Path
    out_dir: Path
    plots_dir: Path
    tables_dir: Path
    cfg: dict[str, Any]
    final: dict[str, Any] | None
    search: dict[str, Any] | None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    items: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    attacks: dict[str, Any] = field(default_factory=dict)
    benign: dict[str, Any] = field(default_factory=dict)
    configs: list[str] = field(default_factory=list)
    healed: str | None = None


def ordered_configs(final: dict[str, Any] | None) -> list[str]:
    names = list(final["configs"]) if final else []
    return [n for n in CONFIG_ORDER if n in names] + [n for n in names if n not in CONFIG_ORDER]


def load_candidates(search: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not search:
        return []
    path = resolve_project_path(search["run_dir"]) / "candidates.jsonl"
    if not path.exists():
        logger.warning("No candidates.jsonl at %s: candidate plots and the healing log will be "
                       "limited", project_relative(path))
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping a corrupt line in %s", project_relative(path))
    return rows


def load_test_items(final_dir: Path, names: list[str]) -> dict[str, list[dict[str, Any]]]:
    items: dict[str, list[dict[str, Any]]] = {}
    for name in names:
        path = final_dir / "configs" / f"{name}_test_items.json"
        if path.exists():
            items[name] = read_json(path).get("items", [])
        else:
            logger.warning("No per-prompt test results for '%s' (%s): per-prompt plots skip it",
                           name, project_relative(path))
    return items


def load_split_prompts(cfg: dict[str, Any], final: dict[str, Any] | None
                       ) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        from selfheal.datasets import load_manifest, load_split

        split = load_split(cfg, "test")
    except Exception as exc: 
        logger.warning("Test split not readable (%s): examples will show no prompt text", exc)
        return {}, {}
    try:
        created = load_manifest(cfg).get("created")
        if final and final.get("splits_created") and created != final["splits_created"]:
            logger.warning("data/splits was rebuilt after the final test (%s vs %s): prompt texts "
                           "in the examples may not match", created, final["splits_created"])
    except Exception:
        pass
    return {a.id: a for a in split.attacks}, {b.id: b for b in split.benign}


def build_context(results_dir: Path) -> Context:
    cfg = load_config(PROJECT_ROOT / "configs" / "base.yaml")
    out_dir = results_dir / "analysis"
    final = _optional(results_dir / "final_test" / "summary.json")
    search = _optional(results_dir / "search" / "summary.json")
    configs = ordered_configs(final)
    ctx = Context(results_dir=results_dir, out_dir=out_dir, plots_dir=out_dir / "plots",
                  tables_dir=out_dir / "tables", cfg=cfg, final=final, search=search,
                  configs=configs)
    ctx.candidates = load_candidates(search)
    if final:
        ctx.items = load_test_items(results_dir / "final_test", configs)
        ctx.attacks, ctx.benign = load_split_prompts(cfg, final)
    ctx.healed = next((n for n in HEALED_PREFERENCE if n in configs), None)
    if final and ctx.healed is None:
        logger.warning("No healed configuration (healed_search or healed) in the final test: "
                       "baseline-vs-healed comparisons are skipped")
    return ctx





def _attack_layer(item: dict[str, Any]) -> str:
    if item.get("hit"):
        return "got through"
    stage = item.get("blocked_stage")
    if stage == "input":
        return "input filter"
    if stage == "output":
        return "output filter"
    return "model / system prompt"


def _benign_layer(item: dict[str, Any]) -> str | None:
    stage = item.get("blocked_stage")
    if stage == "input":
        return "input filter"
    if stage == "output":
        return "output filter"
    if item.get("refusal"):
        return "model refusal"
    return None


def _benign_type(item: dict[str, Any], split_item: Any) -> str:
    tag = str(item.get("tag") or getattr(split_item, "tag", None) or "unknown").lower()
    if tag.startswith("hard"):
        return "hard-negative"
    if tag.startswith("normal"):
        return "normal"
    return tag


def _score(value: Any) -> str:
    return f"{value:.2f}" if isinstance(value, (int, float)) else "n/a"


def _detector_reason(detectors: list[str] | None, score: Any) -> str:
    names = " ".join(detectors or [])
    if "AttackRogueString" in names:
        reason = "the answer printed the injected phrase"
    elif "DecodeMatch" in names:
        reason = "the answer decoded and repeated the hidden payload"
    elif "MitigationBypass" in names:
        reason = "the answer played along without any refusal"
    elif "dan." in names:
        reason = "the answer used the jailbreak persona"
    elif "ToxicComment" in names or "unsafe_content" in names:
        reason = "the answer was toxic"
    else:
        reason = "garak's detector flagged the answer"
    return f"{reason} (detector score {_score(score)})"


def _why_still(item: dict[str, Any], attack: Any, mitigations: list[str]) -> str:
    mit = " ".join(mitigations).lower()
    parts = ["no input regex rule matched" if "regex" in mit else "no input regex rules were enabled"]
    if "classifier" in mit:
        parts.append("the prompt-injection classifier did not flag the prompt")
    else:
        parts.append("the input classifier was off")
    if "output" in mit or "toxicity" in mit or "policy" in mit:
        parts.append("the output filter did not flag the answer")
    else:
        parts.append("no output filter was enabled")
    parts.append(_detector_reason(getattr(attack, "detectors", None), item.get("score")))
    return "; ".join(parts) + "."


def _fp_cause(layer: str, rule: str | None, score: Any) -> str:
    if layer == "input filter":
        if rule and rule.startswith("classifier"):
            return f"the prompt-injection classifier scored it as an injection (score {_score(score)})"
        return f"regex rule `{rule}` matched a harmless phrase"
    if layer == "output filter":
        return f"output filter `{rule}` replaced a harmless answer (score {_score(score)})"
    return "the model itself refused (over-cautious answer)"





def main_results_table(ctx: Context) -> pd.DataFrame:
    if not ctx.final:
        raise SkipStep("no results/final_test/summary.json (run the held-out experiments)")
    rows = []
    for name in ctx.configs:
        entry = ctx.final["configs"][name]
        test, held = entry["test"], entry.get("heldout")
        rows.append({
            "config": name,
            "test ASR % (macro)": pct(test["asr_macro"]),
            "test ASR % (all)": pct(test["asr_micro"]),
            "test ASR 95% CI": _ci_str(test["asr_micro_ci"]),
            "attacks succeeded": f"{test['attack_hits']}/{test['attack_total']}",
            "benign FPR %": pct(test["fpr"]),
            "FPR 95% CI": _ci_str(test["fpr_ci"]),
            "false positives": f"{test['benign_fp']}/{test['benign_n']}",
            "held-out ASR % (macro)": pct(_macro(held["categories"])) if held else NAN,
            "held-out ASR % (all)": pct(held["asr"]) if held else NAN,
            "held-out 95% CI": _ci_str(held["ci"]) if held else "not run",
            "run time (s)": round(float(entry.get("duration_s") or 0.0), 1),
            "forced": bool(entry.get("forced")),
            "mitigations": ", ".join(entry.get("enabled_mitigations") or []),
        })
    return pd.DataFrame(rows)


def per_category_change(ctx: Context) -> pd.DataFrame:
    if not ctx.final or "baseline" not in ctx.final["configs"] or not ctx.healed:
        raise SkipStep("needs baseline and a healed configuration in the final test")
    base, healed = ctx.final["configs"]["baseline"], ctx.final["configs"][ctx.healed]
    rows = []
    for source, key in (("test split (seen families)", "test"), ("held-out families", "heldout")):
        base_cats = (base.get(key) or {}).get("categories") or {}
        healed_cats = (healed.get(key) or {}).get("categories") or {}
        for category in sorted(set(base_cats) | set(healed_cats)):
            b, h = base_cats.get(category), healed_cats.get(category)
            rows.append({
                "data": source,
                "category": category,
                "n": (b or h)["total"],
                "baseline ASR %": pct(b["asr"]) if b else NAN,
                "baseline 95% CI": _ci_str(b["ci"]) if b else "",
                f"{ctx.healed} ASR %": pct(h["asr"]) if h else NAN,
                f"{ctx.healed} 95% CI": _ci_str(h["ci"]) if h else "",
                "change (pp)": round(pct(h["asr"]) - pct(b["asr"]), 1) if b and h else NAN,
                "CIs overlap": ("yes" if _overlap(b["ci"], h["ci"]) else "no") if b and h else "",
            })
    if not rows:
        raise SkipStep("no category results")
    return pd.DataFrame(rows)


def probe_table(ctx: Context) -> pd.DataFrame:
    records: dict[tuple[str, str, str], dict[str, Any]] = {}
    for name, items in ctx.items.items():
        counts: dict[tuple[str, str, str], list[int]] = {}
        for item in items:
            if item.get("kind") != "attack":
                continue
            key = ("test split", item.get("category", "?"), item.get("probe", "?"))
            hits_total = counts.setdefault(key, [0, 0])
            hits_total[0] += bool(item.get("hit"))
            hits_total[1] += 1
        for key, (hits, total) in counts.items():
            row = records.setdefault(key, {})
            row[name] = round(100.0 * hits / total, 1)
            row["n"] = total
    if ctx.final:
        for name in ctx.configs:
            held = ctx.final["configs"][name].get("heldout") or {}
            for category, values in (held.get("categories") or {}).items():
                for probe, result in (values.get("probes") or {}).items():
                    rate = result.get("asr")
                    if rate is None and result.get("total"):
                        rate = result.get("fails", 0) / result["total"]
                    row = records.setdefault(("held-out", category, probe), {})
                    row[name] = pct(rate)
                    row["n"] = result.get("total")
    if not records:
        raise SkipStep("no per-prompt test results and no held-out probe results")
    rows = [{"data": d, "category": c, "probe": p, **values}
            for (d, c, p), values in sorted(records.items())]
    columns = ["data", "category", "probe", "n"] + [n for n in ctx.configs if any(n in r for r in rows)]
    return pd.DataFrame(rows).reindex(columns=columns)


def per_probe_change(ctx: Context, probes: pd.DataFrame) -> pd.DataFrame:
    if not ctx.healed or "baseline" not in probes.columns or ctx.healed not in probes.columns:
        raise SkipStep("needs per-probe results for baseline and the healed configuration")
    df = probes[["data", "category", "probe", "n", "baseline", ctx.healed]].copy()
    df["change (pp)"] = (df[ctx.healed] - df["baseline"]).round(1)
    return df.sort_values(["data", "category", ctx.healed], ascending=[True, True, False])


def layer_attribution(ctx: Context) -> pd.DataFrame:
    rows = []
    for name in ctx.configs:
        attacks = [i for i in ctx.items.get(name, []) if i.get("kind") == "attack"]
        if not attacks:
            continue
        groups: dict[str, list[dict[str, Any]]] = {"all": attacks}
        for item in attacks:
            groups.setdefault(item.get("category", "?"), []).append(item)
        for group, members in groups.items():
            counts = {layer: 0 for layer in LAYERS}
            for item in members:
                counts[_attack_layer(item)] += 1
            row: dict[str, Any] = {"config": name, "category": group, "attacks": len(members)}
            for layer in LAYERS:
                row[f"{layer} %"] = round(100.0 * counts[layer] / len(members), 1)
                row[f"{layer} (n)"] = counts[layer]
            rows.append(row)
    if not rows:
        raise SkipStep("no per-prompt test results (results/final_test/configs/*_test_items.json)")
    return pd.DataFrame(rows)


def false_positive_rows(ctx: Context) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not ctx.items:
        raise SkipStep("no per-prompt test results")
    fp_rows, totals = [], []
    for name in ctx.configs:
        type_counts: dict[str, int] = {}
        for item in ctx.items.get(name, []):
            if item.get("kind") != "benign":
                continue
            split_item = ctx.benign.get(item.get("id"))
            kind = _benign_type(item, split_item)
            type_counts[kind] = type_counts.get(kind, 0) + 1
            layer = _benign_layer(item)
            if layer is None:
                continue
            rule = item.get("rule") or ("model_refusal" if layer == "model refusal" else "")
            fp_rows.append({
                "config": name, "id": item.get("id"), "type": kind, "layer": layer, "rule": rule,
                "score": item.get("rule_score"),
                "cause": _fp_cause(layer, rule, item.get("rule_score")),
                "prompt": getattr(split_item, "prompt", "") if split_item else "",
                "answer": _clip(item.get("final_text"), 200),
            })
        for kind, count in type_counts.items():
            totals.append({"config": name, "type": kind, "benign prompts": count})
    if not totals:
        raise SkipStep("no benign test results")
    fps = pd.DataFrame(fp_rows, columns=["config", "id", "type", "layer", "rule", "score", "cause",
                                         "prompt", "answer"])
    breakdown = []
    totals_df = pd.DataFrame(totals)
    for _, total in totals_df.iterrows():
        subset = fps[(fps["config"] == total["config"]) & (fps["type"] == total["type"])]
        row = {"config": total["config"], "type": total["type"],
               "benign prompts": int(total["benign prompts"]), "false positives": len(subset)}
        for layer in FP_LAYERS:
            row[layer] = int((subset["layer"] == layer).sum())
        breakdown.append(row)
    return fps, pd.DataFrame(breakdown)


def _cand_status(candidate: dict[str, Any]) -> str:
    if candidate.get("kept"):
        return "kept"
    return "acceptable" if candidate.get("accepted_by_rule") else "rejected"


def healing_log(ctx: Context) -> pd.DataFrame:
    if not ctx.search:
        raise SkipStep("no results/search/summary.json (run the search loop)")
    rows: list[dict[str, Any]] = []
    counters: dict[int, int] = {}
    for cand in ctx.candidates:
        iteration = int(cand.get("iteration", 0))
        counters[iteration] = counters.get(iteration, 0) + 1
        status = _cand_status(cand)
        rows.append({
            "iteration": iteration, "kind": cand.get("kind", "moves"),
            "candidate": counters[iteration], "change": cand.get("change"),
            "new system prompt": bool(cand.get("new_prompt")),
            "val ASR %": pct(cand.get("asr_macro")), "val FPR %": pct(cand.get("fpr")),
            "attacks": cand.get("attack_hits"), "false positives": cand.get("benign_fp"),
            "objective": round(float(cand.get("objective", NAN)), 4),
            "decision": {"kept": "KEPT", "acceptable": "acceptable (not best)",
                         "rejected": "rolled back"}[status],
            "reason": cand.get("reason"),
        })
    for it in ctx.search["iterations"]:
        if it.get("kind") != "optimizer":
            continue
        validation = it.get("validation") or {}
        index = it.get("validated_proposal")
        rows.append({
            "iteration": it["iteration"], "kind": "optimizer", "candidate": index or "-",
            "change": f"learned rules (proposal {index})" if index else "no proposal validated",
            "new system prompt": True,
            "val ASR %": pct(validation.get("asr_macro")) if validation else NAN,
            "val FPR %": pct(validation.get("fpr")) if validation else NAN,
            "attacks": validation.get("attack_hits") if validation else None,
            "false positives": validation.get("benign_fp") if validation else None,
            "objective": NAN,
            "decision": "KEPT" if it.get("accepted") else "rolled back",
            "reason": it.get("reason"),
        })
    if not rows:
        raise SkipStep("the search saved no candidates")
    return pd.DataFrame(rows).sort_values("iteration", kind="stable").reset_index(drop=True)


def optimizer_table(ctx: Context) -> pd.DataFrame:
    if not ctx.search:
        raise SkipStep("no search summary")
    rows = []
    for it in ctx.search["iterations"]:
        if it.get("kind") != "optimizer":
            continue
        decision = it.get("decision") or {}
        model_level = it.get("model_level") or {}
        validation = it.get("validation") or {}
        proposals = it.get("proposals") or []
        index = it.get("validated_proposal")
        rules = proposals[index - 1] if index and len(proposals) >= index else ""
        before, after = decision.get("objective_current"), decision.get("objective_candidate")
        rows.append({
            "iteration": it["iteration"],
            "kept": "yes" if it.get("accepted") else "no",
            "reason": it.get("reason"),
            "val ASR % (with guards)": pct(validation.get("asr_macro")) if validation else NAN,
            "val FPR %": pct(validation.get("fpr")) if validation else NAN,
            "objective before": round(before, 4) if isinstance(before, (int, float)) else NAN,
            "objective after": round(after, 4) if isinstance(after, (int, float)) else NAN,
            "objective change": (round(after - before, 4)
                                 if isinstance(before, (int, float)) and isinstance(after, (int, float))
                                 else NAN),
            "model-level attacks before": model_level.get("current_hits"),
            "model-level attacks after": model_level.get("candidate_hits"),
            "learned rules (validated proposal)": rules,
        })
    if not rows:
        raise SkipStep("the prompt optimizer never ran in this search")
    return pd.DataFrame(rows)


def collect_latency(ctx: Context) -> pd.DataFrame:
    """Mean time per pipeline stage, from held-out trace files (if they store timings)."""
    if not ctx.final:
        raise SkipStep("no final-test summary")

    def timings(record: dict[str, Any]) -> dict[str, float]:
        source = record.get("timings") if isinstance(record.get("timings"), dict) else record
        found: dict[str, float] = {}
        for key, value in source.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            if key.endswith("_ms"):
                found[key[:-3]] = float(value) / 1000.0
            elif key.endswith("_s") or source is not record:
                found[key[:-2] if key.endswith("_s") else key] = float(value)
        return found

    rows = []
    for name in ctx.configs:
        held = ctx.final["configs"][name].get("heldout") or {}
        stages: dict[str, list[float]] = {}
        for values in (held.get("categories") or {}).values():
            report = values.get("report_path")
            if not report:
                continue
            folder = resolve_project_path(report).parent
            for trace in folder.glob("*trace*.jsonl"):
                for line in trace.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for stage, seconds in timings(record).items():
                        stages.setdefault(stage, []).append(seconds)
        for stage, values_s in sorted(stages.items()):
            rows.append({"config": name, "stage": stage, "requests": len(values_s),
                         "mean ms": round(1000.0 * sum(values_s) / len(values_s), 2)})
    if not rows:
        raise SkipStep("the held-out trace files store no per-stage timings")
    return pd.DataFrame(rows)


def _is_guard_stage(stage: str) -> bool:
    lowered = stage.lower()
    return any(k in lowered for k in ("input", "output", "guard", "filter", "classif"))





def plot_category_ci(ctx: Context, path: Path) -> Path:
    if not ctx.final:
        raise SkipStep("no final-test summary")
    names = ctx.configs
    categories = sorted({c for n in names for c in ctx.final["configs"][n]["test"]["categories"]})
    sizes = {c: next((ctx.final["configs"][n]["test"]["categories"][c]["total"] for n in names
                      if c in ctx.final["configs"][n]["test"]["categories"]), 0) for c in categories}
    width = 0.8 / max(len(names), 1)
    fig, ax = plt.subplots(figsize=(max(8.0, 2.8 * len(categories)), 5))
    for index, name in enumerate(names):
        cats = ctx.final["configs"][name]["test"]["categories"]
        values, low, high, positions = [], [], [], []
        for j, category in enumerate(categories):
            entry = cats.get(category)
            if not entry:
                continue
            rate, ci = entry["asr"], entry.get("ci") or [entry["asr"], entry["asr"]]
            values.append(rate * 100)
            low.append(max(rate - ci[0], 0) * 100)
            high.append(max(ci[1] - rate, 0) * 100)
            positions.append(j + (index - (len(names) - 1) / 2) * width)
        bars = ax.bar(positions, values, width, yerr=[low, high], capsize=3, label=name)
        ax.bar_label(bars, fmt="%.0f", fontsize=7, padding=2)
    ax.set_xticks(range(len(categories)), labels=[f"{c}\n(n={sizes[c]})" for c in categories])
    ax.set_ylabel("Attack success rate (%) — error bars: 95% Wilson CI")
    ax.set_ylim(0, 110)
    ax.set_title("Test split: ASR per category and configuration (lower is better)")
    ax.grid(axis="y", alpha=0.3)
    ax.legend(fontsize=8)
    return _save(fig, path)


def plot_probe_heatmap_final(probes: pd.DataFrame, configs: list[str], path: Path) -> Path:
    columns = [c for c in configs if c in probes.columns]
    if probes.empty or not columns:
        raise SkipStep("no per-probe data")
    labels = [f"{r['data']} | {r['category']}: {r['probe']} (n={_cell(r['n'])})"
              for _, r in probes.iterrows()]
    values = probes[columns].to_numpy(dtype=float)
    fig, ax = plt.subplots(figsize=(1.4 * len(columns) + 7, 0.4 * len(labels) + 2))
    image = ax.imshow(values, cmap="Reds", vmin=0, vmax=100, aspect="auto")
    ax.set_xticks(range(len(columns)), labels=columns, rotation=20)
    ax.set_yticks(range(len(labels)), labels=labels, fontsize=7)
    for i in range(values.shape[0]):
        for j in range(values.shape[1]):
            value = values[i, j]
            text = "–" if math.isnan(value) else f"{value:.0f}"
            ax.text(j, i, text, ha="center", va="center", fontsize=7,
                    color="white" if not math.isnan(value) and value > 60 else "black")
    fig.colorbar(image, ax=ax, label="ASR (%)")
    ax.set_title("ASR per garak probe and configuration (which attacks survive)")
    return _save(fig, path)


def plot_trajectory(ctx: Context, path: Path) -> Path:
    if not ctx.search:
        raise SkipStep("no search summary")
    iterations = ctx.search["iterations"]
    xs = [it["iteration"] for it in iterations]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7.5), sharex=True)
    styles = {
        "rejected": dict(marker="x", color="grey", alpha=0.5, s=22, label="candidate rolled back"),
        "acceptable": dict(marker="o", facecolors="none", edgecolors="tab:blue", alpha=0.7, s=28,
                           label="acceptable, not chosen"),
        "kept": dict(marker="*", color="green", s=170, label="candidate kept", zorder=4),
    }
    for ax, key, title in ((ax1, "asr_macro", "validation ASR (macro, %)"),
                           (ax2, "fpr", "validation FPR (%)")):
        for it in iterations:
            if it.get("kind") == "optimizer":
                ax.axvspan(it["iteration"] - 0.4, it["iteration"] + 0.4, color="orange", alpha=0.12)
        for status, style in styles.items():
            points = [(c["iteration"], c[key] * 100) for c in ctx.candidates
                      if _cand_status(c) == status and key in c]
            if points:
                ax.scatter([p[0] for p in points], [p[1] for p in points], **style)
        for it in iterations:
            validation = it.get("validation")
            if it.get("kind") == "optimizer" and validation:
                ax.scatter(it["iteration"], validation[key] * 100, marker="D", s=60,
                           color="green" if it.get("accepted") else "red",
                           label="optimizer proposal kept" if it.get("accepted")
                           else "optimizer proposal rolled back", zorder=4)
        ax.plot(xs, [it["val"][key] * 100 for it in iterations], "k-", marker="o",
                label="current configuration", zorder=3)
        ax.axvline(ctx.search["best_iteration"], color="green", linestyle=":",
                   label="best checkpoint (restored)")
        ax.set_ylabel(title)
        ax.grid(alpha=0.3)
    ax2.axhline(ctx.search["search_settings"]["max_fpr"] * 100, color="red", linestyle=":",
                label="max FPR")
    for it in iterations[1:]:
        if not it.get("accepted"):
            ax1.annotate("rolled back", (it["iteration"], ax1.get_ylim()[1] * 0.93), ha="center",
                         fontsize=7, color="red", rotation=90)
    ax2.set_xlabel("Iteration (orange band = prompt-optimizer iteration)")
    ax2.set_xticks(xs)
    ax1.set_title("Healing trajectory on the validation split")
    _dedupe_legend(ax1, fontsize=7, loc="upper right")
    _dedupe_legend(ax2, fontsize=7, loc="upper right")
    return _save(fig, path)


def _frontier(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    best_asr = math.inf
    frontier = []
    for fpr, asr in sorted(points):
        if asr < best_asr - 1e-9:
            frontier.append((fpr, asr))
            best_asr = asr
    return frontier


def plot_candidate_scatter(ctx: Context, path: Path) -> tuple[Path, dict[str, Any]]:
    if not ctx.search or not ctx.candidates:
        raise SkipStep("no candidates.jsonl in the search run folder")
    fig, ax = plt.subplots(figsize=(8, 6))
    all_points = []
    for status, style in (("rejected", dict(marker="x", color="grey", alpha=0.45, s=25)),
                          ("acceptable", dict(marker="o", facecolors="none",
                                              edgecolors="tab:blue", alpha=0.7, s=35))):
        points = [(c["fpr"] * 100, c["asr_macro"] * 100) for c in ctx.candidates
                  if _cand_status(c) == status]
        all_points += points
        if points:
            ax.scatter([p[0] for p in points], [p[1] for p in points],
                       label=f"{status} candidates ({len(points)})", **style)
    kept = [c for c in ctx.candidates if c.get("kept")]
    for cand in kept:
        x, y = cand["fpr"] * 100, cand["asr_macro"] * 100
        all_points.append((x, y))
        ax.scatter(x, y, marker="*", s=200, color="green", zorder=4, label="kept (per iteration)")
        ax.annotate(f"it{cand['iteration']}", (x, y), textcoords="offset points", xytext=(6, 4),
                    fontsize=8, color="green")
    start = ctx.search["iterations"][0]["val"]
    ax.scatter(start["fpr"] * 100, start["asr_macro"] * 100, marker="s", s=90, color="black",
               label="start (no defences)", zorder=4)
    all_points.append((start["fpr"] * 100, start["asr_macro"] * 100))
    best = ctx.search["best_validation"]
    bx, by = best["fpr"] * 100, best["asr_macro"] * 100
    ax.scatter(bx, by, marker="*", s=380, color="red", edgecolors="black", zorder=5,
               label="final healed config")
    frontier = _frontier(all_points)
    if frontier:
        fx = [p[0] for p in frontier] + [max(p[0] for p in all_points) + 1]
        fy = [p[1] for p in frontier] + [frontier[-1][1]]
        ax.step(fx, fy, where="post", color="red", alpha=0.6, linestyle="--",
                label="trade-off frontier")
    ax.axvline(ctx.search["search_settings"]["max_fpr"] * 100, color="red", linestyle=":",
               label="max FPR")
    ax.set_xlabel("Validation FPR (%)")
    ax.set_ylabel("Validation ASR (macro, %)")
    ax.set_title(f"Every evaluated candidate ({len(ctx.candidates)}): lower-left is better")
    ax.grid(alpha=0.3)
    _dedupe_legend(ax, fontsize=7)
    on_frontier = any(abs(p[0] - bx) < 1e-6 and abs(p[1] - by) < 1e-6 for p in frontier)
    return _save(fig, path), {"frontier": len(frontier), "best_on_frontier": on_frontier}


def plot_threshold_curves(ctx: Context, path: Path) -> tuple[Path, list[dict[str, Any]]]:
    if not ctx.search:
        raise SkipStep("no search summary")
    run_dir = resolve_project_path(ctx.search["run_dir"])
    files = sorted(run_dir.glob("threshold_curve_final_*.csv")) or sorted(
        run_dir.glob("threshold_curve_start_*.csv"))
    if not files:
        raise SkipStep(f"no threshold_curve_*.csv in {project_relative(run_dir)}")
    final_m = ctx.search.get("final_mitigations") or {}
    fig, axes = plt.subplots(1, len(files), figsize=(5.6 * len(files), 4.3), squeeze=False)
    notes = []
    for ax, file in zip(axes[0], files):
        short = file.stem.split("_", 3)[-1]
        df = pd.read_csv(file)
        ax.plot(df["threshold"], df["asr_macro"] * 100, marker="o", label="validation ASR")
        ax.plot(df["threshold"], df["fpr"] * 100, marker="s", label="validation FPR")
        key, switch, title = THRESHOLD_KEYS.get(short, (None, None, short))
        chosen = _get(final_m, key) if key else None
        enabled = bool(_get(final_m, switch)) if switch else False
        if chosen is not None:
            ax.axvline(float(chosen), color="green" if enabled else "grey", linestyle="--",
                       label=f"chosen {float(chosen):g}" + ("" if enabled else " (feature off)"))
        ax.set_title(title, fontsize=10)
        ax.set_xlabel("setting value")
        ax.set_ylabel("%")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=7)
        notes.append({"name": title, "chosen": chosen, "enabled": enabled,
                      "fpr_flat": df["fpr"].nunique() == 1,
                      "fpr_range": (pct(df["fpr"].min()), pct(df["fpr"].max())),
                      "asr_range": (pct(df["asr_macro"].min()), pct(df["asr_macro"].max()))})
    fig.suptitle("Threshold curves on validation (other settings fixed at the final config; "
                 "cached answers)", fontsize=10)
    return _save(fig, path), notes


def plot_layer_attribution(ctx: Context, attribution: pd.DataFrame, path: Path) -> Path:
    data = attribution[attribution["category"] == "all"]
    if data.empty:
        raise SkipStep("no attribution data")
    fig, ax = plt.subplots(figsize=(9, 0.7 * len(data) + 1.8))
    left = [0.0] * len(data)
    names = data["config"].tolist()
    for layer in LAYERS:
        values = data[f"{layer} %"].tolist()
        bars = ax.barh(names, values, left=left, color=LAYER_COLORS[layer], label=layer)
        for bar, value in zip(bars, values):
            if value >= 6:
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_y() + bar.get_height() / 2,
                        f"{value:.0f}%", ha="center", va="center", fontsize=8, color="white")
        left = [a + b for a, b in zip(left, values)]
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    ax.set_xlabel("Share of test-split attacks (%)")
    ax.set_title("Which layer stopped each attack (red = attack succeeded)")
    ax.legend(fontsize=8, ncol=4, loc="upper center", bbox_to_anchor=(0.5, -0.18))
    return _save(fig, path)


def plot_generalisation(ctx: Context, path: Path) -> tuple[Path, dict[str, Any]]:
    if not ctx.final or not ctx.healed or "baseline" not in ctx.final["configs"]:
        raise SkipStep("needs baseline and a healed configuration")
    names = ["baseline", ctx.healed]
    if not all(ctx.final["configs"][n].get("heldout") for n in names):
        raise SkipStep("held-out scans missing (experiments were run with --skip-heldout)")
    categories = sorted(set(ctx.final["configs"]["baseline"]["test"]["categories"])
                        | set(ctx.final["configs"]["baseline"]["heldout"]["categories"]))
    labels = categories + ["macro"]
    series = [(n, src) for n in names for src in ("test", "heldout")]
    width = 0.8 / len(series)
    fig, ax = plt.subplots(figsize=(max(8.0, 2.6 * len(labels)), 5))
    summary: dict[str, Any] = {}
    for index, (name, source) in enumerate(series):
        cats = ctx.final["configs"][name][source]["categories"]
        values, low, high, positions = [], [], [], []
        for j, category in enumerate(labels):
            if category == "macro":
                rate, ci = _macro(cats), None
            elif category in cats:
                rate, ci = cats[category]["asr"], cats[category].get("ci")
            else:
                continue
            values.append(rate * 100)
            low.append(max(rate - ci[0], 0) * 100 if ci else 0)
            high.append(max(ci[1] - rate, 0) * 100 if ci else 0)
            positions.append(j + (index - (len(series) - 1) / 2) * width)
        summary[f"{name}_{source}"] = _macro(cats)
        label = f"{name} — {'seen families (test split)' if source == 'test' else 'held-out families'}"
        bars = ax.bar(positions, values, width, yerr=[low, high], capsize=3, label=label,
                      hatch="//" if source == "heldout" else None,
                      color="tab:grey" if name == "baseline" else "tab:green",
                      alpha=0.55 if source == "heldout" else 0.95, edgecolor="black")
        ax.bar_label(bars, fmt="%.0f", fontsize=7, padding=2)
    ax.set_xticks(range(len(labels)), labels=labels)
    ax.set_ylabel("ASR (%) — error bars: 95% Wilson CI")
    ax.set_ylim(0, 110)
    ax.set_title("Generalisation: probe families seen during healing vs held-out families")
    ax.grid(axis="y", alpha=0.3)
    ax.legend(fontsize=7)
    return _save(fig, path), summary


def plot_fp_breakdown(breakdown: pd.DataFrame, configs: list[str], path: Path) -> Path:
    types = [t for t in ("normal", "hard-negative") if t in set(breakdown["type"])]
    types += sorted(t for t in set(breakdown["type"]) if t not in types)
    fig, axes = plt.subplots(1, len(types), figsize=(5.5 * len(types), 4.5), squeeze=False,
                             sharey=True)
    for ax, kind in zip(axes[0], types):
        data = breakdown[breakdown["type"] == kind].set_index("config").reindex(
            [c for c in configs if c in set(breakdown["config"])]).fillna(0)
        bottom = [0.0] * len(data)
        for layer in FP_LAYERS:
            values = data[layer].tolist()
            ax.bar(data.index, values, bottom=bottom, color=FP_COLORS[layer], label=layer)
            bottom = [a + b for a, b in zip(bottom, values)]
        for x, (name, row) in enumerate(data.iterrows()):
            ax.annotate(f"{int(row['false positives'])}/{int(row['benign prompts'])}",
                        (x, bottom[x]), ha="center", va="bottom", fontsize=8)
        ax.set_title(f"{kind} benign prompts")
        ax.set_ylabel("Wrongly blocked or refused (count)")
        ax.tick_params(axis="x", rotation=20)
        ax.grid(axis="y", alpha=0.3)
    axes[0][0].legend(fontsize=8)
    fig.suptitle("False positives on the benign test split, by layer and prompt type")
    return _save(fig, path)


def plot_cost(ctx: Context, latency: pd.DataFrame | None, path: Path) -> Path:
    panels = []
    if ctx.search:
        panels.append("iterations")
    if ctx.final:
        panels.append("experiments")
    if latency is not None and not latency.empty:
        panels.append("latency")
    if not panels:
        raise SkipStep("no search summary, final test or latency data")
    fig, axes = plt.subplots(1, len(panels), figsize=(6 * len(panels), 4.5), squeeze=False)
    for ax, panel in zip(axes[0], panels):
        if panel == "iterations":
            its = ctx.search["iterations"]
            colors = {"start": "tab:grey", "moves": "tab:blue", "thresholds": "tab:blue",
                      "optimizer": "tab:orange"}
            bars = ax.bar([str(it["iteration"]) for it in its], [it["duration_s"] for it in its],
                          color=[colors.get(it["kind"], "tab:blue") for it in its])
            ax.bar_label(bars, labels=[f"{it['model_calls']} calls" for it in its], fontsize=6,
                         rotation=90, padding=2)
            ax.set_title("Search: time per iteration (orange = optimizer)")
            ax.set_xlabel("iteration")
            ax.set_ylabel("seconds")
        elif panel == "experiments":
            names = ctx.configs
            bars = ax.bar(names, [float(ctx.final["configs"][n].get("duration_s") or 0) for n in names],
                          color="tab:green")
            ax.bar_label(bars, fmt="%.0f s", fontsize=7)
            ax.set_title("Held-out experiment run time per configuration")
            ax.set_ylabel("seconds")
            ax.tick_params(axis="x", rotation=20)
        else:
            stages = [s for s in sorted(set(latency["stage"])) if "total" not in s.lower()]
            width = 0.8 / max(len(stages), 1)
            names = [n for n in ctx.configs if n in set(latency["config"])]
            for index, stage in enumerate(stages):
                values = []
                for name in names:
                    row = latency[(latency["config"] == name) & (latency["stage"] == stage)]
                    values.append(float(row["mean ms"].iloc[0]) if not row.empty else 0.0)
                positions = [j + (index - (len(stages) - 1) / 2) * width for j in range(len(names))]
                ax.bar(positions, values, width, label=stage)
            ax.set_xticks(range(len(names)), labels=names, rotation=20)
            ax.set_title("Mean time per request and stage (held-out traces)")
            ax.set_ylabel("milliseconds")
            ax.legend(fontsize=7)
        ax.grid(axis="y", alpha=0.3)
    return _save(fig, path)





def category_order(experiments: dict[str, dict[str, Any]]) -> list[str]:
    ordered: list[str] = []
    for summary in experiments.values():
        for category in summary["categories"]:
            if category not in ordered:
                ordered.append(category)
    return ordered


def experiments_table(exps: dict[str, dict[str, Any]], categories: list[str]) -> pd.DataFrame:
    rows = []
    for name, summary in exps.items():
        row: dict[str, Any] = {"config": name,
                               "overall ASR % (macro)": pct(summary["overall_asr_macro"])}
        for category in categories:
            metrics = summary["categories"].get(category)
            row[f"{category} ASR %"] = pct(metrics["asr"]) if metrics else NAN
        benign = summary["benign"]
        row["benign FPR %"] = pct(benign["fpr"])
        row["guard FPR %"] = pct(benign["guard_fpr"])
        row["model refusals"] = benign["model_refusals"]
        row["scan time (s)"] = round(summary["timings"]["scan_s"], 1)
        row["mitigations"] = ", ".join(summary["enabled_mitigations"])
        rows.append(row)
    return pd.DataFrame(rows)


def healing_table(healing: dict[str, Any], categories: list[str]) -> pd.DataFrame:
    rows = []
    for it in healing["iterations"]:
        row: dict[str, Any] = {"iteration": it["iteration"], "status": it["label"]}
        for category in categories:
            metrics = it["categories"].get(category)
            if metrics is None:
                row[f"{category} ASR %"] = "–"
                continue
            mark = "*" if metrics["carried_over"] and it["iteration"] > 0 else ""
            row[f"{category} ASR %"] = f"{pct(metrics['asr'])}{mark}"
        row["overall ASR %"] = pct(it["overall_asr_macro"])
        row["benign FPR %"] = pct(it["benign_fpr"])
        row["time (s)"] = round(it["duration_s"], 1)
        row["changes"] = "; ".join(
            f"{c['category']} step {c['step']}" for c in it["changes"]
        ) or "-"
        rows.append(row)
    return pd.DataFrame(rows)


def search_table(search: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for it in search["iterations"]:
        val = it["val"]
        rows.append({
            "iteration": it["iteration"],
            "kind": it["kind"],
            "kept": "yes" if it["accepted"] and it["iteration"] > 0 else ("start" if it["iteration"] == 0 else "no"),
            "val ASR %": pct(val["asr_macro"]),
            "val FPR %": pct(val["fpr"]),
            "objective": round(it["objective"], 4),
            "candidates": it["candidates"],
            "model calls": it["model_calls"],
            "time (s)": round(it["duration_s"], 1),
            "best": "★" if it.get("best_so_far") else "",
            "change": it["change"] or "-",
        })
    return pd.DataFrame(rows)


def per_probe_table(exps: dict[str, dict[str, Any]]) -> pd.DataFrame:
    records: dict[tuple[str, str], dict[str, float]] = {}
    for name, summary in exps.items():
        for category, metrics in summary["categories"].items():
            for probe, result in metrics["probes"].items():
                records.setdefault((category, probe), {})[name] = pct(result["asr"])
    rows = [{"category": c, "probe": p, **values} for (c, p), values in records.items()]
    return pd.DataFrame(rows)


def final_test_rows(final: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for name, entry in final["configs"].items():
        test = entry["test"]
        heldout = entry.get("heldout") or {}
        rows.append({
            "config": name,
            "test ASR % (macro)": pct(test["asr_macro"]),
            "test ASR % (all)": pct(test["asr_micro"]),
            "test ASR CI low": pct(test["asr_micro_ci"][0]),
            "test ASR CI high": pct(test["asr_micro_ci"][1]),
            "benign FPR %": pct(test["fpr"]),
            "FPR CI low": pct(test["fpr_ci"][0]),
            "FPR CI high": pct(test["fpr_ci"][1]),
            "held-out ASR %": pct(heldout.get("asr")) if heldout else NAN,
            "held-out CI low": pct(heldout["ci"][0]) if heldout else NAN,
            "held-out CI high": pct(heldout["ci"][1]) if heldout else NAN,
            "mitigations": ", ".join(entry["enabled_mitigations"]),
        })
    return pd.DataFrame(rows)


def plot_asr_by_category(exps: dict[str, dict[str, Any]], categories: list[str],
                         path: Path) -> Path:
    labels = categories + ["overall (macro)"]
    names = list(exps)
    width = 0.8 / max(len(names), 1)
    fig, ax = plt.subplots(figsize=(max(8.0, 2.4 * len(labels)), 5))
    for index, name in enumerate(names):
        summary = exps[name]
        values = [summary["categories"].get(c, {}).get("asr", 0.0) * 100 for c in categories]
        values.append(summary["overall_asr_macro"] * 100)
        offsets = [j + (index - (len(names) - 1) / 2) * width for j in range(len(labels))]
        bars = ax.bar(offsets, values, width, label=name)
        ax.bar_label(bars, fmt="%.0f", fontsize=7, padding=1)
    ax.set_xticks(range(len(labels)), labels=labels)
    ax.set_ylabel("Attack success rate (%)")
    ax.set_ylim(0, 105)
    ax.set_title("garak experiments: ASR per category and configuration (lower is better)")
    ax.grid(axis="y", alpha=0.3)
    ax.legend(fontsize=8)
    return _save(fig, path)


def plot_healing(healing: dict[str, Any], categories: list[str], path: Path) -> Path:
    iterations = healing["iterations"]
    xs = [it["iteration"] for it in iterations]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 7), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1]})
    for category in categories:
        if any(category not in it["categories"] for it in iterations):
            continue
        ys = [it["categories"][category]["asr"] * 100 for it in iterations]
        ax1.plot(xs, ys, marker="o", label=category)
    ax1.plot(xs, [it["overall_asr_macro"] * 100 for it in iterations], "k--", marker="s",
             label="overall (macro)")
    ax1.axhline(healing["target_asr"] * 100, color="red", linestyle=":",
                label=f"target {healing['target_asr']:.0%}")
    ax1.set_ylabel("Attack success rate (%)")
    ax1.set_ylim(0, 105)
    ax1.set_title("Old playbook healing loop: ASR per iteration")
    ax1.grid(alpha=0.3)
    ax1.legend(fontsize=8)
    ax2.bar(xs, [it["benign_fpr"] * 100 for it in iterations], color="tab:purple")
    ax2.axhline(healing["max_false_positive_rate"] * 100, color="red", linestyle=":")
    ax2.set_ylabel("Benign FPR (%)")
    ax2.set_xlabel("Healing iteration")
    ax2.set_xticks(xs)
    ax2.grid(axis="y", alpha=0.3)
    return _save(fig, path)


def plot_search_progress(search: dict[str, Any], path: Path) -> Path:
    iterations = search["iterations"]
    xs = [it["iteration"] for it in iterations]
    asr = [it["val"]["asr_macro"] * 100 for it in iterations]
    fpr = [it["val"]["fpr"] * 100 for it in iterations]
    fig, ax = plt.subplots(figsize=(9, 5))
    for it, x in zip(iterations, xs):
        if it["kind"] == "optimizer":
            ax.axvspan(x - 0.4, x + 0.4, color="orange", alpha=0.15)
    ax.plot(xs, asr, marker="o", label="validation ASR (macro)")
    ax.plot(xs, fpr, marker="s", label="validation FPR")
    ax.plot(xs, [it["objective"] * 100 for it in iterations], "k--", label="objective x100")
    kept = [(x, a) for x, a, it in zip(xs, asr, iterations) if it["accepted"] and it["iteration"] > 0]
    if kept:
        ax.scatter([k[0] for k in kept], [k[1] for k in kept], s=140, facecolors="none",
                   edgecolors="green", linewidths=2, label="change kept", zorder=3)
    ax.axhline(search["search_settings"]["max_fpr"] * 100, color="red", linestyle=":",
               label="max FPR")
    ax.axvline(search["best_iteration"], color="green", linestyle=":", label="best checkpoint")
    ax.set_xticks(xs)
    ax.set_xlabel("Iteration (orange = prompt-optimizer iteration)")
    ax.set_ylabel("%")
    ax.set_title("Search-based healing on the validation split")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    return _save(fig, path)


def plot_final_test(final: dict[str, Any], path: Path) -> Path:
    names = ordered_configs(final)
    metrics = [
        ("test ASR (all prompts)", lambda e: (e["test"]["asr_micro"], e["test"]["asr_micro_ci"])),
        ("held-out ASR", lambda e: (e["heldout"]["asr"], e["heldout"]["ci"])
         if e.get("heldout") else None),
        ("benign FPR (test)", lambda e: (e["test"]["fpr"], e["test"]["fpr_ci"])),
    ]
    width = 0.8 / max(len(names), 1)
    fig, ax = plt.subplots(figsize=(9, 5))
    for index, name in enumerate(names):
        values, low, high, positions = [], [], [], []
        for j, (_, getter) in enumerate(metrics):
            got = getter(final["configs"][name])
            if got is None:
                continue
            value, ci = got
            values.append(value * 100)
            low.append(max(value - ci[0], 0) * 100)
            high.append(max(ci[1] - value, 0) * 100)
            positions.append(j + (index - (len(names) - 1) / 2) * width)
        bars = ax.bar(positions, values, width, yerr=[low, high], capsize=4, label=name)
        ax.bar_label(bars, fmt="%.1f", fontsize=7, padding=2)
    ax.set_xticks(range(len(metrics)), labels=[m[0] for m in metrics])
    ax.set_ylabel("% (error bars = 95% Wilson CI)")
    ax.set_ylim(0, 105)
    ax.set_title("Final test on held-out data (lower is better)")
    ax.grid(axis="y", alpha=0.3)
    ax.legend(fontsize=8)
    return _save(fig, path)


def plot_probe_heatmap(per_probe: pd.DataFrame, configs: list[str], path: Path) -> Path | None:
    columns = [c for c in configs if c in per_probe.columns]
    if per_probe.empty or not columns:
        return None
    labels = [f"{row['category']}: {row['probe']}" for _, row in per_probe.iterrows()]
    values = per_probe[columns].to_numpy(dtype=float)
    fig, ax = plt.subplots(figsize=(1.5 * len(columns) + 5, 0.45 * len(labels) + 1.8))
    image = ax.imshow(values, cmap="Reds", vmin=0, vmax=100, aspect="auto")
    ax.set_xticks(range(len(columns)), labels=columns, rotation=20)
    ax.set_yticks(range(len(labels)), labels=labels, fontsize=8)
    for i in range(values.shape[0]):
        for j in range(values.shape[1]):
            value = values[i, j]
            text = "–" if math.isnan(value) else f"{value:.0f}"
            ax.text(j, i, text, ha="center", va="center", fontsize=8,
                    color="white" if not math.isnan(value) and value > 60 else "black")
    fig.colorbar(image, ax=ax, label="ASR (%)")
    ax.set_title("garak experiments: ASR per probe")
    return _save(fig, path)


def _action(record: dict[str, Any] | None) -> str:
    if record is None:
        return "no pipeline trace found for this prompt"
    stage = record.get("blocked_stage")
    if stage == "input":
        decision = record.get("input_decision") or {}
        return (f"input filter **blocked** the prompt (rule `{decision.get('rule')}`, "
                f"score {_score(decision.get('score'))})")
    if stage == "output":
        decision = record.get("output_decision") or {}
        return (f"output filter **replaced** the response (rule `{decision.get('rule')}`, "
                f"score {_score(decision.get('score'))})")
    return "passed every guard; the model's own answer was judged safe"


def build_examples(exps: dict[str, dict[str, Any]], categories: list[str],
                   max_fixed: int = 2) -> str:
    """Old garak experiments: baseline-vs-patched examples (kept for compatibility)."""
    if "baseline" not in exps:
        return "# Qualitative examples (garak experiments)\n\nNo baseline experiment found.\n"
    patched_name = next((n for n in ("healed_search", "healed", "both") if n in exps), None)
    if patched_name is None:
        return "# Qualitative examples (garak experiments)\n\nNo patched experiment.\n"
    threshold = float(exps["baseline"]["garak_settings"].get("eval_threshold", 0.5))
    lines = ["# Qualitative examples (garak experiments, not held-out)", "",
             "> Content warning: baseline responses can contain offensive text.", ""]
    for category in categories:
        base_metrics = exps["baseline"]["categories"].get(category)
        patched_metrics = exps[patched_name]["categories"].get(category)
        if not base_metrics or not patched_metrics:
            continue
        try:
            base_report = parse_report(resolve_project_path(base_metrics["report_path"]), threshold)
            patched_report = parse_report(resolve_project_path(patched_metrics["report_path"]),
                                          threshold)
        except FileNotFoundError as exc:
            logger.warning("Skipping garak examples for %s: %s", category, exc)
            continue
        trace = load_trace(resolve_project_path(patched_metrics["trace_path"]))
        patched_index = {(a.probe, a.prompt): a for a in patched_report.attempts}
        pairs: list[tuple[str, AttemptRecord, AttemptRecord]] = []
        for attempt in base_report.attempts:
            if not attempt.hit(threshold):
                continue
            patched = patched_index.get((attempt.probe, attempt.prompt))
            if patched is not None:
                pairs.append(("Still failing" if patched.hit(threshold) else "Fixed", attempt, patched))
        lines += [f"## {category}", ""]
        shown = [p for p in pairs if p[0] == "Fixed"][:max_fixed] + [p for p in pairs
                                                                     if p[0] != "Fixed"][:1]
        for kind, base_attempt, patched_attempt in shown:
            lines += [f"### {kind}: `{base_attempt.probe}`", "", "```text",
                      _clip(base_attempt.prompt, 600), "```", "",
                      f"Patched: {_action(trace.get(base_attempt.prompt))}", "", "```text",
                      _clip(patched_attempt.outputs[0] if patched_attempt.outputs else None, 400),
                      "```", ""]
    return "\n".join(lines)





def _diverse(pairs: list[Any], key: Callable[[Any], Any], k: int) -> list[Any]:
    groups: dict[Any, list[Any]] = {}
    for pair in pairs:
        groups.setdefault(key(pair), []).append(pair)
    chosen: list[Any] = []
    while len(chosen) < k and any(groups.values()):
        for group in list(groups):
            if groups[group] and len(chosen) < k:
                chosen.append(groups[group].pop(0))
    return chosen


def build_heldout_examples(ctx: Context, fps: pd.DataFrame | None) -> str:
    if not ctx.healed or "baseline" not in ctx.items or ctx.healed not in ctx.items:
        raise SkipStep("needs per-prompt test results for baseline and the healed configuration")
    base = {i["id"]: i for i in ctx.items["baseline"] if i.get("kind") == "attack"}
    healed = {i["id"]: i for i in ctx.items[ctx.healed] if i.get("kind") == "attack"}
    mitigations = ctx.final["configs"][ctx.healed].get("enabled_mitigations") or []
    fixed = [(base[i], healed[i]) for i in base if i in healed and base[i].get("hit")
             and not healed[i].get("hit")]
    still = [(base.get(i), h) for i, h in healed.items() if h.get("hit")]
    lines = [
        "> Content warning: baseline answers can contain offensive text produced by the model "
        "under attack. They are shown truncated, for research purposes.",
        "",
        f"All examples come from the held-out **test split**; baseline vs **{ctx.healed}** on "
        "the same prompts and seeds.",
        "",
        f"### Attacks fixed by healing ({len(fixed)} in total; up to 5 shown, varied by layer)",
        "",
    ]
    if not fixed:
        lines += ["_No attack that succeeded at baseline was fixed._", ""]
    for b, h in _diverse(fixed, lambda p: (_attack_layer(p[1]), p[1].get("category")), 5):
        attack = ctx.attacks.get(h["id"])
        layer = _attack_layer(h)
        how = (f"rule `{h.get('rule')}`" if h.get("rule") else
               "the model's answer was judged safe")
        lines += [
            f"**{h.get('category')} — `{h.get('probe')}` — stopped by: {layer}** ({how})",
            "",
            "Prompt:", "", "```text", _clip(getattr(attack, "prompt", None), 500), "```", "",
            f"Baseline answer (detector score {_score(b.get('score'))}):", "", "```text",
            _clip(b.get("final_text"), 300), "```", "",
            f"{ctx.healed} answer (detector score {_score(h.get('score'))}):", "", "```text",
            _clip(h.get("final_text"), 300), "```", "",
        ]
    lines += [f"### Attacks that still succeed after healing ({len(still)} in total; up to 3 shown)", ""]
    if not still:
        lines += ["_No test-split attack succeeds against the healed configuration._", ""]
    for b, h in _diverse(still, lambda p: p[1].get("category"), 3):
        attack = ctx.attacks.get(h["id"])
        lines += [
            f"**{h.get('category')} — `{h.get('probe')}`**",
            "",
            f"Why it got through: {_why_still(h, attack, mitigations)}",
            "",
            "Prompt:", "", "```text", _clip(getattr(attack, "prompt", None), 500), "```", "",
            f"{ctx.healed} answer:", "", "```text", _clip(h.get("final_text"), 300), "```", "",
        ]
    healed_fps = fps[fps["config"] == ctx.healed] if fps is not None and not fps.empty else None
    count = 0 if healed_fps is None else len(healed_fps)
    lines += [f"### Benign prompts still wrongly blocked ({count} in total; up to 3 shown)", ""]
    if not count:
        lines += ["_The healed configuration blocks or refuses no benign test prompt._", ""]
    else:
        for _, row in healed_fps.head(3).iterrows():
            lines += [f"**{row['type']} prompt — blocked by: {row['layer']}**", "",
                      f"Cause: {row['cause']}.", "", "```text", _clip(row["prompt"], 300), "```", ""]
    return "\n".join(lines)





def _healed_entry(ctx: Context) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not ctx.final or not ctx.healed or "baseline" not in ctx.final["configs"]:
        return None, None
    return ctx.final["configs"]["baseline"], ctx.final["configs"][ctx.healed]


def text_main(ctx: Context) -> str:
    base, healed = _healed_entry(ctx)
    if not base:
        return "The table lists every configuration on the held-out test split."
    b, h = base["test"], healed["test"]
    clear = not _overlap(b["asr_micro_ci"], h["asr_micro_ci"])
    text = (f"Without defences, {b['attack_hits']}/{b['attack_total']} test attacks succeeded "
            f"({_rate(b['asr_micro'], b['asr_micro_ci'])}); `{ctx.healed}` lets "
            f"{h['attack_hits']}/{h['attack_total']} through ({_rate(h['asr_micro'], h['asr_micro_ci'])}) "
            f"while wrongly blocking {h['benign_fp']}/{h['benign_n']} benign prompts "
            f"(baseline {b['benign_fp']}/{b['benign_n']}). ")
    text += ("The confidence intervals do not overlap, so the reduction is clear. " if clear else
             "The confidence intervals overlap, so the difference is not statistically clear "
             "with this sample size. ")
    others = [n for n in ctx.configs if n not in ("baseline", ctx.healed)]
    if others:
        best = min(others, key=lambda n: (ctx.final["configs"][n]["test"]["attack_hits"],
                                          ctx.final["configs"][n]["test"]["benign_fp"]))
        o = ctx.final["configs"][best]["test"]
        text += (f"The best hand-built configuration, `{best}`, reaches {o['attack_hits']}/"
                 f"{o['attack_total']} successful attacks with {o['benign_fp']}/{o['benign_n']} "
                 f"false positives.")
    return text


def text_categories(ctx: Context) -> str:
    base, healed = _healed_entry(ctx)
    text = ("Bars show the share of test-split attacks per category that succeeded; error bars "
            "are 95% Wilson intervals and n is under each category. ")
    if base:
        zero = [c for c, v in base["test"]["categories"].items() if v["asr"] == 0]
        worst = max(healed["test"]["categories"].items(), key=lambda kv: kv[1]["asr"])
        text += (f"The largest remaining category for `{ctx.healed}` is {worst[0]} at "
                 f"{_rate(worst[1]['asr'], worst[1]['ci'])}. ")
        if zero:
            text += (f"{', '.join(zero)} is already 0% at baseline, so no defence can show an "
                     "improvement there.")
    return text


def text_heatmap(ctx: Context, probes: pd.DataFrame | None) -> str:
    text = ("Each cell is the ASR of one garak probe for one configuration; rows marked "
            "'held-out' are probe families the healing loop never saw. ")
    if probes is not None and ctx.healed and ctx.healed in probes.columns:
        survivors = probes[probes[ctx.healed] > 0].sort_values(ctx.healed, ascending=False)
        if survivors.empty:
            text += f"No probe has a successful attack against `{ctx.healed}`."
        else:
            names = ", ".join(f"{r['probe']} ({r[ctx.healed]:.0f}%)" for _, r in survivors.head(4).iterrows())
            text += f"{len(survivors)} probe(s) still succeed against `{ctx.healed}`: {names}."
    return text


def text_attribution(ctx: Context, attribution: pd.DataFrame | None) -> str:
    if attribution is None:
        return ""
    data = attribution[attribution["category"] == "all"].set_index("config")
    text = ("Each bar splits the test-split attacks by the layer that stopped them; 'model / "
            "system prompt' means the model's own answer was judged safe. ")
    if ctx.healed in data.index:
        row = data.loc[ctx.healed]
        text += (f"In `{ctx.healed}`, the input filter stops {row['input filter %']:.0f}%, the "
                 f"model/system prompt {row['model / system prompt %']:.0f}%, the output filter "
                 f"{row['output filter %']:.0f}%, and {row['got through %']:.0f}% get through. ")
    if "baseline" in data.index:
        text += (f"Without defences the model itself already declines "
                 f"{data.loc['baseline']['model / system prompt %']:.0f}% of attacks.")
    return text


def text_generalisation(ctx: Context, summary: dict[str, Any] | None) -> str:
    if not summary:
        return ""
    seen_drop = summary["baseline_test"] - summary[f"{ctx.healed}_test"]
    held_drop = summary["baseline_heldout"] - summary[f"{ctx.healed}_heldout"]
    text = (f"On probe families seen during healing, macro ASR goes from "
            f"{summary['baseline_test'] * 100:.1f}% to {summary[f'{ctx.healed}_test'] * 100:.1f}%; "
            f"on held-out families it goes from {summary['baseline_heldout'] * 100:.1f}% to "
            f"{summary[f'{ctx.healed}_heldout'] * 100:.1f}%. ")
    if seen_drop > 2 * max(held_drop, 0) + 0.02:
        text += ("The much smaller drop on held-out families means the defences partly fit the "
                 "wording of known attacks and generalise only partly.")
    else:
        text += "The improvement carries over to unseen attack families to a similar degree."
    return text


def text_fp(ctx: Context, fps: pd.DataFrame | None, breakdown: pd.DataFrame | None) -> str:
    if breakdown is None:
        return ""
    text = ("Stacked bars count benign test prompts that were wrongly blocked (input or output "
            "filter) or refused by the model, separately for normal and hard-negative prompts. ")
    if fps is not None and ctx.healed:
        healed = fps[fps["config"] == ctx.healed]
        if healed.empty:
            text += f"`{ctx.healed}` has no false positives on the test split."
        else:
            by_layer = healed["layer"].value_counts().to_dict()
            by_type = healed["type"].value_counts().to_dict()
            text += (f"`{ctx.healed}` has {len(healed)} false positive(s): by layer {by_layer}, by "
                     f"type {by_type}. Hard negatives are expected to be the hardest case.")
    return text


def text_trajectory(ctx: Context) -> str:
    if not ctx.search:
        return ""
    its = ctx.search["iterations"]
    kept = sum(1 for it in its[1:] if it.get("accepted"))
    start, best = its[0]["val"], ctx.search["best_validation"]
    return (f"The loop ran {len(its) - 1} iterations ({kept} kept a change) and stopped because: "
            f"{ctx.search['stop_reason']}. Validation ASR went from {start['asr_macro'] * 100:.1f}% "
            f"to {best['asr_macro'] * 100:.1f}% and FPR from {start['fpr'] * 100:.1f}% to "
            f"{best['fpr'] * 100:.1f}% (best checkpoint: iteration {ctx.search['best_iteration']}). "
            "Grey crosses are rolled-back candidates; green stars are the kept ones.")


def text_scatter(ctx: Context, info: dict[str, Any] | None) -> str:
    if not info:
        return ""
    return (f"Every point is one of the {len(ctx.candidates)} evaluated candidates on validation; "
            f"the dashed red line is the trade-off frontier ({info['frontier']} configurations "
            "that no other point beats on both ASR and FPR). The final healed configuration "
            f"{'lies on' if info['best_on_frontier'] else 'is NOT on'} this frontier"
            f"{'.' if info['best_on_frontier'] else ', so the objective (fpr_weight) preferred another trade-off.'}")


def text_thresholds(notes: list[dict[str, Any]] | None) -> str:
    if not notes:
        return ""
    parts = ["Curves show validation ASR and FPR when only this setting changes. "]
    for note in notes:
        chosen = "off" if not note["enabled"] else f"{float(note['chosen']):g}"
        part = f"{note['name']}: chosen {chosen}; ASR ranges {note['asr_range'][0]}–{note['asr_range'][1]}%"
        if note["fpr_flat"]:
            part += f", FPR stays at {note['fpr_range'][0]}% (the threshold cannot remove these false positives)"
        else:
            part += f", FPR {note['fpr_range'][0]}–{note['fpr_range'][1]}%"
        parts.append(part + ". ")
    return "".join(parts)


def text_optimizer(table: pd.DataFrame | None) -> str:
    if table is None:
        return "The prompt optimizer did not run in this search."
    kept = int((table["kept"] == "yes").sum())
    text = f"The optimizer validated {len(table)} proposal(s); {kept} were kept. "
    if kept == 0:
        text += ("None improved the deployed pipeline or the unguarded model by the required "
                 "margin, so all learned rules were rolled back. On a small model, system-prompt "
                 "edits often change results by less than the sampling noise.")
    else:
        text += "Kept edits and their score changes are listed below."
    return text


def text_cost(ctx: Context, latency: pd.DataFrame | None) -> str:
    parts = []
    if ctx.search:
        its = ctx.search["iterations"]
        moves = [it["duration_s"] for it in its if it["kind"] in ("moves", "thresholds")]
        opt = [it["duration_s"] for it in its if it["kind"] == "optimizer"]
        parts.append(f"The search took {ctx.search['total_duration_s'] / 60:.1f} min; move "
                     f"iterations average {sum(moves) / len(moves) if moves else 0:.1f} s (mostly "
                     f"cached answers) and optimizer iterations "
                     f"{sum(opt) / len(opt) if opt else 0:.1f} s (new model calls).")
    if ctx.final:
        parts.append("Experiment times depend on how many answers were already cached, so they "
                     "compare effort, not pure defence cost.")
    if latency is not None and not latency.empty:
        guard = latency[latency["stage"].map(_is_guard_stage)]
        if not guard.empty:
            per_config = guard.groupby("config")["mean ms"].sum().to_dict()
            parts.append("Guard time per request (input + output filters): " +
                         ", ".join(f"{k} {v:.1f} ms" for k, v in per_config.items()) + ".")
    return " ".join(parts)


def conclusions(ctx: Context, cat_change: pd.DataFrame | None, probe_change: pd.DataFrame | None,
                attribution: pd.DataFrame | None, fps: pd.DataFrame | None,
                optimizer: pd.DataFrame | None, gen: dict[str, Any] | None) -> list[str]:
    lines = ["## Conclusions", "", "### Main findings", ""]
    base, healed = _healed_entry(ctx)
    if base:
        lines.append(f"- {text_main(ctx)}")
    if attribution is not None and ctx.healed:
        lines.append(f"- {text_attribution(ctx, attribution)}")
    if gen:
        lines.append(f"- {text_generalisation(ctx, gen)}")
    if fps is not None and ctx.healed:
        lines.append(f"- {text_fp(ctx, fps, None) or 'False-positive data unavailable.'}")
    lines += ["", "### What the healing loop improved most", ""]
    if cat_change is not None:
        seen = cat_change[cat_change["data"].str.startswith("test")].dropna(subset=["change (pp)"])
        if not seen.empty:
            top = seen.sort_values("change (pp)").iloc[0]
            lines.append(f"- {top['category']}: {top['baseline ASR %']}% → "
                         f"{top[f'{ctx.healed} ASR %']}% on the test split ({top['change (pp)']} pp; "
                         f"CIs overlap: {top['CIs overlap']}).")
    if ctx.search:
        kept_changes = [it["change"] for it in ctx.search["iterations"][1:]
                        if it.get("accepted") and it.get("change")]
        if kept_changes:
            lines.append(f"- Kept changes, in order: {'; '.join(kept_changes)}.")
    lines += ["", "### What remains unfixed, and why", ""]
    if probe_change is not None and ctx.healed:
        rest = probe_change[probe_change[ctx.healed] > 0].sort_values(ctx.healed, ascending=False)
        if rest.empty:
            lines.append(f"- No probe has a successful attack against `{ctx.healed}`.")
        for _, row in rest.head(5).iterrows():
            lines.append(f"- {row['data']} / {row['probe']}: {row[ctx.healed]:.0f}% ASR "
                         f"(baseline {_cell(row['baseline'])}%).")
    if fps is not None and ctx.healed:
        for _, row in fps[fps["config"] == ctx.healed].head(3).iterrows():
            lines.append(f"- False positive ({row['type']}): {row['cause']}.")
    if optimizer is not None and int((optimizer["kept"] == "yes").sum()) == 0:
        lines.append("- No optimizer edit was kept: the system prompt itself was not improved by "
                     "the loop; robustness comes from the filters.")
    lines += ["", "### Limitations", ""]
    if base:
        cats = base["test"]["categories"]
        smallest = min(cats.items(), key=lambda kv: kv[1]["total"])
        width = (healed["test"]["asr_micro_ci"][1] - healed["test"]["asr_micro_ci"][0]) * 100
        lines += [
            f"- Small sets: {base['test']['attack_total']} test attacks and "
            f"{base['test']['benign_n']} benign test prompts; the smallest category "
            f"({smallest[0]}) has n = {smallest[1]['total']}, so one prompt moves it by "
            f"{100 / max(smallest[1]['total'], 1):.0f} points.",
            f"- Wide intervals: the 95% CI of `{ctx.healed}` test ASR spans {width:.0f} points; "
            "differences of a few prompts are not statistically clear.",
        ]
    lines += [
        f"- Small model: results are for `{(ctx.final or ctx.search or {}).get('model', 'unknown')}`; "
        "larger models follow system prompts differently.",
        "- One seed and one sample per prompt; garak detectors are heuristics (for example, an "
        "answer without refusal words counts as a jailbreak success).",
        "- Held-out garak scans are only paired across configurations when PYTHONHASHSEED was "
        "fixed for every run.",
    ]
    if ctx.final and ctx.final.get("forced"):
        lines.append("- At least one configuration was re-tested after changing (forced), so its "
                     "numbers are not purely held-out.")
    return lines





def readme_section(model: str, exps_df: pd.DataFrame | None, healing_df: pd.DataFrame | None,
                   healing: dict[str, Any] | None, search_df: pd.DataFrame | None,
                   search: dict[str, Any] | None, final: dict[str, Any] | None,
                   plots: dict[str, Path]) -> str:
    lines = [
        f"_Generated by `python -m scripts.analyze` on {datetime.now():%Y-%m-%d %H:%M}. "
        f"Model: `{model}`. Numbers come from your own run; nothing here is hand-edited._",
        "",
        "Full written report with every plot, table and finding: "
        "[`results/analysis/report.md`](results/analysis/report.md).",
        "",
    ]
    if final is not None:
        lines += [
            "#### Final test on held-out data (the numbers to report)",
            "",
            "Test split = prompts the healing loop never saw; held-out = garak probe families "
            "the loop never saw. Brackets are 95% Wilson confidence intervals."
            + (" **This run was forced after the configs changed.**" if final.get("forced") else ""),
            "",
            format_final_table(final),
            "",
        ]
    if search is not None and search_df is not None:
        learned = search.get("learned_rules") or "(none)"
        lines += [
            "#### Search-based healing (decisions on the validation split)",
            "",
            f"Best checkpoint: iteration {search['best_iteration']} "
            f"(objective {search['best_objective']:.4f}); stop: {search['stop_reason']}; "
            f"total {search['total_duration_s'] / 60:.1f} min. Final mitigations: "
            f"{', '.join(search['final_enabled_mitigations'])}.",
            "",
            df_to_markdown(search_df),
            "",
            "Learned rules added to the hardened system prompt:",
            "",
            "```text",
            learned,
            "```",
            "",
        ]
    if exps_df is not None:
        lines += ["#### garak experiments (full probe sets; NOT held-out, for comparison only)", "",
                  df_to_markdown(exps_df), ""]
    if healing is not None and healing_df is not None:
        lines += ["#### Old playbook healing loop", "", df_to_markdown(healing_df), ""]
    lines += ["#### Plots", ""]
    for title, path in plots.items():
        lines.append(f"![{title}]({project_relative(path)})")
    lines += ["", "Tables (CSV): `results/analysis/tables/`. Plots (PNG): `results/analysis/plots/`."]
    return "\n".join(lines)


def update_readme(section: str) -> bool:
    readme = PROJECT_ROOT / "README.md"
    if not readme.exists():
        logger.warning("README.md not found; skipping README update")
        return False
    text = readme.read_text(encoding="utf-8")
    start, end = text.find(README_START), text.find(README_END)
    if start == -1 or end == -1 or end < start:
        logger.warning("README results markers not found; skipping README update")
        return False
    updated = text[: start + len(README_START)] + "\n" + section + "\n" + text[end:]
    readme.write_text(updated, encoding="utf-8")
    return True





class Runner:
    """Runs each step; a step with missing data is skipped and the reason is recorded."""

    def __init__(self) -> None:
        self.skipped: dict[str, str] = {}

    def __call__(self, name: str, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return fn(*args)
        except SkipStep as exc:
            logger.warning("Skipping %s: %s", name, exc)
            self.skipped[name] = str(exc)
        except Exception as exc: 
            logger.warning("Skipping %s: unexpected %s: %s", name, type(exc).__name__, exc)
            logger.debug("Traceback for %s", name, exc_info=True)
            self.skipped[name] = f"unexpected error ({type(exc).__name__}: {exc})"
        return None


def _write_csv(df: pd.DataFrame | None, path: Path, outputs: dict[str, Path]) -> None:
    if df is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8")
    outputs[f"table:{path.stem}"] = path


def _section(lines: list[str], ctx: Context, title: str, text: str, image: Path | None = None,
             table: pd.DataFrame | None = None, csv_name: str | None = None,
             max_rows: int | None = None, skipped: str | None = None) -> None:
    lines += [f"## {title}", ""]
    if image is None and table is None:
        lines += [f"_Skipped: {skipped or 'data not available'}._", ""]
        return
    if text:
        lines += [text, ""]
    if image is not None:
        lines += [f"![{title}]({image.relative_to(ctx.out_dir).as_posix()})", ""]
    if table is not None:
        lines += [df_to_markdown(table, max_rows), ""]
        if csv_name:
            lines += [f"CSV: `tables/{csv_name}`", ""]


def run_analysis(results_dir: Path, refresh_readme: bool = True) -> dict[str, Path]:
    """Create every analysis artifact for the data that exists; returns their paths."""
    ctx = build_context(results_dir)
    exps = load_experiments(results_dir)
    healing = _optional(results_dir / "healing" / "summary.json")
    if not exps and ctx.search is None and ctx.final is None:
        raise FileNotFoundError(
            f"No summaries under {results_dir}; run the experiments, the search loop or the final test."
        )
    ctx.plots_dir.mkdir(parents=True, exist_ok=True)
    ctx.tables_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Analysing | final test: %s (%s) | search: %s (%d candidates) | per-prompt results: %s "
                "| old experiments: %s | old playbook healing: %s",
                "yes" if ctx.final else "no", ", ".join(ctx.configs) or "-",
                "yes" if ctx.search else "no", len(ctx.candidates),
                ", ".join(ctx.items) or "none", list(exps) or "none", "yes" if healing else "no")
    run = Runner()
    outputs: dict[str, Path] = {}
    plots: dict[str, Path] = {}
    P, T = ctx.plots_dir, ctx.tables_dir

   
    main_df = run("main results table", main_results_table, ctx)
    cat_df = run("per-category change table", per_category_change, ctx)
    probes_df = run("per-probe table", probe_table, ctx)
    probe_change_df = run("per-probe change table", per_probe_change, ctx, probes_df) \
        if probes_df is not None else None
    attribution_df = run("layer attribution table", layer_attribution, ctx)
    fp_result = run("false-positive tables", false_positive_rows, ctx)
    fps_df, fp_breakdown_df = fp_result if fp_result else (None, None)
    log_df = run("healing log", healing_log, ctx)
    optimizer_df = run("optimizer edits table", optimizer_table, ctx)
    latency_df = run("latency table", collect_latency, ctx)
    search_df = search_table(ctx.search) if ctx.search else None
    cost_iter_df = (pd.DataFrame([{"iteration": it["iteration"], "kind": it["kind"],
                                   "duration (s)": round(it["duration_s"], 1),
                                   "model calls": it["model_calls"],
                                   "candidates": it["candidates"]}
                                  for it in ctx.search["iterations"]]) if ctx.search else None)
    cost_exp_df = (pd.DataFrame([{"config": n, "duration (s)": round(float(
        ctx.final["configs"][n].get("duration_s") or 0), 1)} for n in ctx.configs])
        if ctx.final else None)
    for df, name in ((main_df, "main_results"), (cat_df, "per_category_baseline_vs_healed"),
                     (probes_df, "per_probe_all_configs"),
                     (probe_change_df, "per_probe_baseline_vs_healed"),
                     (attribution_df, "layer_attribution"), (fps_df, "false_positives"),
                     (fp_breakdown_df, "false_positive_breakdown"), (log_df, "healing_log"),
                     (optimizer_df, "optimizer_edits"), (latency_df, "latency_per_stage"),
                     (search_df, "search_iterations"), (cost_iter_df, "cost_iterations"),
                     (cost_exp_df, "cost_experiments")):
        _write_csv(df, T / f"{name}.csv", outputs)
    if ctx.final:
        _write_csv(final_test_rows(ctx.final), T / "final_test.csv", outputs)

   
    p1 = run("plot 1 (per-category ASR with CIs)", plot_category_ci, ctx, P / "01_category_asr_ci.png")
    p2 = (run("plot 2 (per-probe heatmap)", plot_probe_heatmap_final, probes_df, ctx.configs,
              P / "02_probe_heatmap.png") if probes_df is not None else None)
    p3 = run("plot 3 (healing trajectory)", plot_trajectory, ctx, P / "03_healing_trajectory.png")
    p4_result = run("plot 4 (candidate scatter)", plot_candidate_scatter, ctx,
                    P / "04_candidates_asr_vs_fpr.png")
    p4, p4_info = p4_result if p4_result else (None, None)
    p5_result = run("plot 5 (threshold curves)", plot_threshold_curves, ctx,
                    P / "05_threshold_curves.png")
    p5, p5_notes = p5_result if p5_result else (None, None)
    p6 = (run("plot 6 (layer attribution)", plot_layer_attribution, ctx, attribution_df,
              P / "06_layer_attribution.png") if attribution_df is not None else None)
    p7_result = run("plot 7 (generalisation)", plot_generalisation, ctx, P / "07_generalisation.png")
    p7, gen_summary = p7_result if p7_result else (None, None)
    p8 = (run("plot 8 (false positives)", plot_fp_breakdown, fp_breakdown_df, ctx.configs,
              P / "08_false_positives.png") if fp_breakdown_df is not None else None)
    p9 = run("plot 9 (cost)", plot_cost, ctx, latency_df, P / "09_cost.png")
    final_plot = (run("final-test overview plot", plot_final_test, ctx.final, P / "final_test_ci.png")
                  if ctx.final else None)
    progress_plot = (run("search progress plot", plot_search_progress, ctx.search,
                         P / "search_progress.png") if ctx.search else None)
    for title, path in (("Per-category ASR with 95% CIs", p1), ("ASR per probe", p2),
                        ("Healing trajectory", p3), ("Candidates: ASR vs FPR", p4),
                        ("Threshold curves", p5), ("Layer attribution", p6),
                        ("Generalisation", p7), ("False positives", p8), ("Cost", p9),
                        ("Final test overview", final_plot), ("Search progress", progress_plot)):
        if path is not None:
            plots[title] = path

   
    exps_df = healing_df = None
    if exps:
        categories = category_order(exps)
        exps_df = experiments_table(exps, categories)
        old_probes = per_probe_table(exps)
        _write_csv(exps_df, T / "garak_experiments_overall.csv", outputs)
        _write_csv(old_probes, T / "garak_experiments_per_probe.csv", outputs)
        path = run("garak experiments plot", plot_asr_by_category, exps, categories,
                   P / "garak_experiments_asr.png")
        if path:
            plots["garak experiments (not held-out)"] = path
        heat = run("garak experiments heatmap", plot_probe_heatmap, old_probes, list(exps),
                   P / "garak_experiments_heatmap.png")
        if heat:
            plots["garak experiments per probe"] = heat
        if healing is not None:
            healing_df = healing_table(healing, categories)
            _write_csv(healing_df, T / "playbook_healing_iterations.csv", outputs)
            path = run("playbook healing plot", plot_healing, healing, categories,
                       P / "playbook_healing.png")
            if path:
                plots["Old playbook healing"] = path
        (ctx.out_dir / "examples.md").write_text(build_examples(exps, categories), encoding="utf-8")
        outputs["examples_md (garak)"] = ctx.out_dir / "examples.md"

   
    examples = run("qualitative examples", build_heldout_examples, ctx, fps_df)
    if examples:
        path = ctx.out_dir / "examples_test_split.md"
        path.write_text("# Qualitative examples (held-out test split)\n\n" + examples, encoding="utf-8")
        outputs["examples_test_split_md"] = path

   
    model = (ctx.final or ctx.search or {}).get("model") or (
        next(iter(exps.values()))["model"] if exps else "unknown")
    lines = [
        "# Self-healing LLM security pipeline: results report",
        "",
        f"_Generated by `python -m scripts.analyze` on {datetime.now():%Y-%m-%d %H:%M} from saved "
        f"results (no model calls). Model: `{model}`. Configurations: "
        f"{', '.join(ctx.configs) or 'none'}; healed configuration: `{ctx.healed or 'none'}`._",
        "",
    ]
    _section(lines, ctx, "1. Main results (held-out test split)", text_main(ctx), table=main_df,
             csv_name="main_results.csv", skipped=run.skipped.get("main results table"))
    _section(lines, ctx, "2. Per-category ASR with 95% confidence intervals", text_categories(ctx),
             image=p1, table=cat_df, csv_name="per_category_baseline_vs_healed.csv",
             skipped=run.skipped.get("plot 1 (per-category ASR with CIs)"))
    _section(lines, ctx, "3. ASR per garak probe", text_heatmap(ctx, probes_df), image=p2,
             table=probe_change_df, csv_name="per_probe_baseline_vs_healed.csv", max_rows=40,
             skipped=run.skipped.get("plot 2 (per-probe heatmap)") or run.skipped.get("per-probe table"))
    _section(lines, ctx, "4. Layer attribution: which defence stopped each attack",
             text_attribution(ctx, attribution_df), image=p6,
             table=(attribution_df[attribution_df["category"] == "all"]
                    if attribution_df is not None else None),
             csv_name="layer_attribution.csv", skipped=run.skipped.get("layer attribution table"))
    _section(lines, ctx, "5. Generalisation: seen vs held-out probe families",
             text_generalisation(ctx, gen_summary), image=p7,
             skipped=run.skipped.get("plot 7 (generalisation)"))
    _section(lines, ctx, "6. False positives by layer and prompt type",
             text_fp(ctx, fps_df, fp_breakdown_df), image=p8, table=fp_breakdown_df,
             csv_name="false_positive_breakdown.csv", skipped=run.skipped.get("false-positive tables"))
    _section(lines, ctx, "7. Healing trajectory", text_trajectory(ctx), image=p3,
             table=(log_df[log_df["decision"] == "KEPT"] if log_df is not None else None),
             csv_name="healing_log.csv (every candidate)",
             skipped=run.skipped.get("plot 3 (healing trajectory)"))
    _section(lines, ctx, "8. Every evaluated candidate: ASR vs FPR", text_scatter(ctx, p4_info),
             image=p4, skipped=run.skipped.get("plot 4 (candidate scatter)"))
    _section(lines, ctx, "9. Threshold curves", text_thresholds(p5_notes), image=p5,
             skipped=run.skipped.get("plot 5 (threshold curves)"))
    _section(lines, ctx, "10. Prompt-optimizer edits", text_optimizer(optimizer_df),
             table=optimizer_df, csv_name="optimizer_edits.csv",
             skipped=run.skipped.get("optimizer edits table"))
    _section(lines, ctx, "11. Cost: time and latency", text_cost(ctx, latency_df), image=p9,
             table=latency_df, csv_name="latency_per_stage.csv",
             skipped=run.skipped.get("plot 9 (cost)"))
    lines += ["## 12. Qualitative examples", ""]
    lines += [examples, ""] if examples else [
        f"_Skipped: {run.skipped.get('qualitative examples', 'data not available')}._", ""]
    lines += conclusions(ctx, cat_df, probe_change_df, attribution_df, fps_df, optimizer_df,
                         gen_summary)
    if run.skipped:
        lines += ["", "## Skipped steps", ""]
        lines += [f"- {name}: {reason}" for name, reason in run.skipped.items()]
    (ctx.out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    outputs["report_md"] = ctx.out_dir / "report.md"

   
    tables_md = ["# Results tables", ""]
    for title, df in (("Main results", main_df), ("Per category: baseline vs healed", cat_df),
                      ("Per probe: baseline vs healed", probe_change_df),
                      ("Layer attribution", attribution_df),
                      ("False-positive breakdown", fp_breakdown_df), ("False positives", fps_df),
                      ("Healing log", log_df), ("Optimizer edits", optimizer_df),
                      ("Search iterations", search_df), ("Latency per stage", latency_df),
                      ("garak experiments (not held-out)", exps_df)):
        if df is not None:
            tables_md += [f"## {title}", "", df_to_markdown(df), ""]
    (ctx.out_dir / "results_table.md").write_text("\n".join(tables_md), encoding="utf-8")
    outputs["tables_md"] = ctx.out_dir / "results_table.md"
    outputs.update({f"plot:{k}": v for k, v in plots.items()})

    if refresh_readme:
        section = readme_section(model, exps_df, healing_df, healing, search_df, ctx.search,
                                 ctx.final, plots)
        if update_readme(section):
            outputs["readme"] = PROJECT_ROOT / "README.md"
    for key, path in outputs.items():
        logger.info("Wrote %-44s %s", key, project_relative(path))
    logger.info("Analysis done | %d plots, %d tables | %d step(s) skipped%s", len(plots),
                sum(1 for k in outputs if k.startswith("table:")), len(run.skipped),
                f": {', '.join(run.skipped)}" if run.skipped else "")
    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build result tables, plots, examples and a report.")
    parser.add_argument("--results-dir", default=None, help="defaults to paths.results_dir")
    parser.add_argument("--no-readme", action="store_true", help="do not update README.md")
    args = parser.parse_args(argv)
    base = load_config(PROJECT_ROOT / "configs" / "base.yaml")
    setup_logging(base["logging"]["level"], resolve_project_path(base["logging"]["dir"]),
                  "analyze")
    results_dir = resolve_project_path(args.results_dir or base["paths"]["results_dir"])
    try:
        run_analysis(results_dir, refresh_readme=not args.no_readme)
    except FileNotFoundError as exc:
        logger.error("Step 'analysis' cannot start: %s", exc)
        return 1
    except Exception:
        logger.exception("Step 'analysis' failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())