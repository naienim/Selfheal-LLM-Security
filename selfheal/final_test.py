from __future__ import annotations

import copy
import logging
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from selfheal.cache import make_key
from selfheal.config import ConfigError, describe_mitigations, project_relative, resolve_project_path
from selfheal.datasets import load_manifest, load_split
from selfheal.evaluator import EvalResult, Evaluator
from selfheal.experiment import read_json, run_scans, write_json
from selfheal.moves import normalize

logger = logging.getLogger(__name__)

Z95 = 1.959963984540054
CONFIG_ORDER = ["baseline", "prompt_only", "output_only", "both", "healed_search"]


class TestAlreadyUsedError(RuntimeError):
    """Raised when a config name was already tested with a different configuration."""


def wilson(successes: int, n: int, z: float = Z95) -> list[float]:
    """95% Wilson score interval for a proportion (works well for small n and 0 or n)."""
    if n <= 0:
        return [0.0, 0.0]
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return [max(0.0, centre - half), min(1.0, centre + half)]


def fingerprint(cfg: dict[str, Any]) -> str:
    """Identity of a defended system: model + mitigations + every system-prompt text."""
    prompts = cfg.get("system_prompts") or {}
    texts = {key: str(prompts.get(key, "")) for key in
             ("baseline", "hardened", "spotlight_note", "learned_rules")}
    return make_key(cfg["target"]["model"], cfg["mitigations"], texts)[:16]

def _pct_ci(rate: float, ci: list[float]) -> str:
    return f"{rate * 100:.1f}% [{ci[0] * 100:.1f}, {ci[1] * 100:.1f}]"

def format_final_table_old(summary: dict[str, Any]) -> str:
    """Markdown tables for the held-out results (overall + per category)."""
    lines = [
        "| config | test ASR (macro) | test ASR, all prompts [95% CI] | benign FPR [95% CI] | "
        "held-out ASR [95% CI] |",
        "|---|---|---|---|---|",
    ]
    for name, entry in summary["configs"].items():
        test = entry["test"]
        heldout = entry.get("heldout")
        if heldout:
            held_text = f"{_pct_ci(heldout['asr'], heldout['ci'])} ({heldout['fails']}/{heldout['total']})"
        else:
            held_text = "not run"
        attacks_text = f"{test['attack_hits']}/{test['attack_total']}"
        benign_text = f"{test['benign_fp']}/{test['benign_n']}"
        lines.append(
            f"| {name} | {test['asr_macro'] * 100:.1f}% | "
            f"{_pct_ci(test['asr_micro'], test['asr_micro_ci'])} ({attacks_text}) | "
            f"{_pct_ci(test['fpr'], test['fpr_ci'])} ({benign_text}) | {held_text} |"
        )
    lines += ["", "| config | data | category | ASR [95% CI] | n |", "|---|---|---|---|---|"]
    for name, entry in summary["configs"].items():
        for category, values in sorted(entry["test"]["categories"].items()):
            lines.append(f"| {name} | test split | {category} | "
                         f"{_pct_ci(values['asr'], values['ci'])} | {values['total']} |")
        if entry.get("heldout"):
            for category, values in sorted(entry["heldout"]["categories"].items()):
                lines.append(f"| {name} | held-out garak | {category} | "
                             f"{_pct_ci(values['asr'], values['ci'])} | {values['total']} |")
    return "\n".join(lines)

def _macro(categories: dict[str, Any]) -> float:
    """Average ASR over categories that have prompts (each category counts equally)."""
    rates = [values["asr"] for values in categories.values() if values.get("total")]
    return sum(rates) / len(rates) if rates else 0.0


def format_final_table(summary: dict[str, Any]) -> str:
    lines = [
        "| config | test ASR (macro) | test ASR, all prompts [95% CI] | benign FPR [95% CI] | "
        "held-out ASR (macro) | held-out ASR, all prompts [95% CI] |",
        "|---|---|---|---|---|---|",
    ]
    for name, entry in summary["configs"].items():
        test = entry["test"]
        heldout = entry.get("heldout")
        if heldout:
            held_macro = f"{_macro(heldout['categories']) * 100:.1f}%"
            held_all = f"{_pct_ci(heldout['asr'], heldout['ci'])} ({heldout['fails']}/{heldout['total']})"
        else:
            held_macro = held_all = "not run"
        attacks_text = f"{test['attack_hits']}/{test['attack_total']}"
        benign_text = f"{test['benign_fp']}/{test['benign_n']}"
        lines.append(
            f"| {name} | {test['asr_macro'] * 100:.1f}% | "
            f"{_pct_ci(test['asr_micro'], test['asr_micro_ci'])} ({attacks_text}) | "
            f"{_pct_ci(test['fpr'], test['fpr_ci'])} ({benign_text}) | {held_macro} | {held_all} |"
        )
    lines += ["", "| config | data | category | ASR [95% CI] | n |", "|---|---|---|---|---|"]
    for name, entry in summary["configs"].items():
        for category, values in sorted(entry["test"]["categories"].items()):
            lines.append(f"| {name} | test split | {category} | "
                         f"{_pct_ci(values['asr'], values['ci'])} | {values['total']} |")
        if entry.get("heldout"):
            for category, values in sorted(entry["heldout"]["categories"].items()):
                lines.append(f"| {name} | held-out garak | {category} | "
                             f"{_pct_ci(values['asr'], values['ci'])} | {values['total']} |")
    return "\n".join(lines)


def _load_log(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"configs": {}}
    data = read_json(path)
    return data if isinstance(data.get("configs"), dict) else {"configs": {}}


def _check_test_log(log: dict[str, Any], fingerprints: dict[str, str], force: bool) -> set[str]:
    """Return the names whose configuration changed since they were last tested."""
    changed = {name for name, fp in fingerprints.items()
               if name in log["configs"] and log["configs"][name]["fingerprint"] != fp}
    if changed and not force:
        raise TestAlreadyUsedError(
            f"Config(s) {sorted(changed)} were already tested on the held-out data with a "
            "different configuration. Re-testing a changed config lets the test set steer "
            "decisions, so its numbers would no longer be held-out. Use --force only if you "
            "accept this; the result is then marked as forced."
        )
    for name in sorted(changed):
        logger.warning("--force: re-testing changed config '%s'; its result is marked as forced",
                       name)
    for name in sorted(set(fingerprints) - changed):
        if name in log["configs"]:
            logger.info("Config '%s' unchanged since its last held-out test: re-running is safe",
                        name)
    return changed


def _test_entry(result: EvalResult) -> dict[str, Any]:
    categories = {
        category: {**values, "ci": wilson(values["hits"], values["total"])}
        for category, values in result.categories.items()
    }
    return {
        "asr_macro": result.asr_macro,
        "attack_hits": result.attack_hits,
        "attack_total": result.attack_total,
        "asr_micro": result.asr_micro,
        "asr_micro_ci": wilson(result.attack_hits, result.attack_total),
        "categories": categories,
        "benign_fp": result.benign_fp,
        "benign_n": result.benign_n,
        "fpr": result.fpr,
        "fpr_ci": wilson(result.benign_fp, result.benign_n),
        "guard_fp": result.guard_fp,
        "model_refusals": result.model_refusals,
        "fp_by_reason": result.fp_by_reason,
        "blocked_by_rule": result.blocked_by_rule,
    }


def _heldout(state: dict[str, Any], final_cfg: dict[str, Any], run_dir: Path,
             name: str) -> dict[str, Any]:
    cfg = copy.deepcopy(state)
    cfg["garak"]["prompt_cap"] = int(final_cfg.get("heldout_prompt_cap", 20))
    generations = int(final_cfg.get("heldout_generations", 1))
    cfg["garak"]["categories"] = {
        category: {"generations": generations, "probes": list(probes)}
        for category, probes in final_cfg["heldout_categories"].items()
    }
    scans = run_scans(cfg, run_dir / f"{name}_heldout", label=f"heldout_{name}")
    categories: dict[str, Any] = {}
    fails = total = 0
    for category, metrics in scans.items():
        categories[category] = {
            "fails": metrics["fails"], "total": metrics["total"], "asr": metrics["asr"],
            "ci": wilson(metrics["fails"], metrics["total"]), "probes": metrics["probes"],
            "report_path": metrics["report_path"],
        }
        fails += metrics["fails"]
        total += metrics["total"]
    return {"categories": categories, "fails": fails, "total": total,
            "asr": fails / total if total else 0.0, "ci": wilson(fails, total)}


def _clip(text: str | None, limit: int) -> str:
    if not text:
        return "(no output)"
    text = text.replace("```", "'''").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " [...]"


def _action(item: dict[str, Any]) -> str:
    score = item.get("rule_score")
    score_text = f", score {score:.2f}" if isinstance(score, (int, float)) else ""
    if item["blocked_stage"] == "input":
        return f"input filter **blocked** the prompt (rule `{item['rule']}`{score_text})"
    if item["blocked_stage"] == "output":
        return f"output filter **replaced** the answer (rule `{item['rule']}`{score_text})"
    if item["hit"]:
        return "passed every guard and the attack still succeeded"
    return "passed every guard; the model's own answer was judged safe"


def build_examples(base_cfg: dict[str, Any], out_dir: Path, entries: dict[str, Any],
                   per_category: int = 2) -> str | None:
    """Baseline vs patched answers on the held-out test split, including attacks that still work."""
    if "baseline" not in entries:
        return None
    patched = next((n for n in ("healed_search", "both", "prompt_only", "output_only")
                    if n in entries), None)
    configs_dir = out_dir / "configs"
    if patched is None or not (configs_dir / f"{patched}_test_items.json").exists():
        return None
    try:
        test = load_split(base_cfg, "test")
    except FileNotFoundError:
        return None
    prompts = {a.id: a.prompt for a in test.attacks}
    base_items = read_json(configs_dir / "baseline_test_items.json").get("items", [])
    patched_items = {i["id"]: i for i in read_json(configs_dir / f"{patched}_test_items.json")
                     .get("items", []) if i["kind"] == "attack"}
    lines = [
        "# Qualitative examples (held-out test split)",
        "",
        "> Content warning: baseline answers can contain offensive text produced by the model "
        "under attack. They are shown truncated, for research purposes.",
        "",
        f"Baseline vs **{patched}** on the same test prompts and seeds.",
        "",
    ]
    for category in sorted({i["category"] for i in base_items if i["kind"] == "attack"}):
        fixed, still = [], []
        for item in base_items:
            if item["kind"] != "attack" or item["category"] != category or not item["hit"]:
                continue
            other = patched_items.get(item["id"])
            if other is not None:
                (still if other["hit"] else fixed).append((item, other))
        lines += [f"## {category}", "",
                  f"Successful baseline attacks: {len(fixed) + len(still)}; fixed by {patched}: "
                  f"{len(fixed)}; still succeeding: {len(still)}.", ""]
        for kind, pairs in (("Fixed", fixed[:per_category]), ("Still failing", still[:1])):
            for base_item, other in pairs:
                lines += [
                    f"### {kind}: `{base_item['probe']}`", "",
                    "**Attack prompt** (truncated):", "", "```text",
                    _clip(prompts.get(base_item["id"]), 700), "```", "",
                    f"**Baseline answer** (detector score {base_item['score']:.2f}):", "",
                    "```text", _clip(base_item["final_text"], 400), "```", "",
                    f"**{patched} answer** (detector score {other['score']:.2f}): {_action(other)}",
                    "", "```text", _clip(other["final_text"], 400), "```", "",
                ]
    return "\n".join(lines)


def rebuild_summary(base_cfg: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """Merge every per-config result into summary.json, final_test.md and examples.md."""
    configs_dir = out_dir / "configs"
    files = {p.stem: p for p in configs_dir.glob("*.json") if not p.stem.endswith("_test_items")}
    order = [n for n in CONFIG_ORDER if n in files] + sorted(n for n in files
                                                              if n not in CONFIG_ORDER)
    entries = {name: read_json(files[name]) for name in order}
    try:
        splits_created = load_manifest(base_cfg).get("created")
    except FileNotFoundError:
        splits_created = None
    summary = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "model": base_cfg["target"]["model"],
        "splits_created": splits_created,
        "forced": any(entry.get("forced") for entry in entries.values()),
        "heldout_families": (base_cfg.get("final_test") or {}).get("heldout_categories"),
        "configs": entries,
        "total_duration_s": sum(entry.get("duration_s", 0.0) for entry in entries.values()),
    }
    write_json(summary, out_dir / "summary.json")
    (out_dir / "final_test.md").write_text(format_final_table(summary) + "\n", encoding="utf-8")
    examples = build_examples(base_cfg, out_dir, entries)
    if examples:
        (out_dir / "examples.md").write_text(examples, encoding="utf-8")
    return summary


def run_final_test(base_cfg: dict[str, Any], configs: dict[str, tuple[Path, dict[str, Any]]],
                   force: bool = False, skip_heldout: bool = False) -> dict[str, Any]:
    """Evaluate each config on the test split and the held-out garak families."""
    final_cfg = base_cfg.get("final_test") or {}
    if not skip_heldout and not final_cfg.get("heldout_categories"):
        raise ConfigError("final_test.heldout_categories is missing (see configs/base.yaml)")
    out_dir = resolve_project_path(final_cfg.get("dir", "results/final_test"))
    configs_dir = out_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    states: dict[str, tuple[Path, dict[str, Any]]] = {}
    for name, (path, cfg) in configs.items():
        state = copy.deepcopy(cfg)
        normalize(state["mitigations"])
        states[name] = (path, state)
    fingerprints = {name: fingerprint(state) for name, (_, state) in states.items()}
    log_path = out_dir / "test_log.json"
    log = _load_log(log_path)
    changed = _check_test_log(log, fingerprints, force)

    run_dir = out_dir / f"run_{datetime.now():%Y%m%d_%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    logger.info("HELD-OUT TEST START | configs: %s | held-out garak families: %s | run dir %s",
                ", ".join(states), "skipped" if skip_heldout else
                sum(len(p) for p in final_cfg["heldout_categories"].values()),
                project_relative(run_dir))
    evaluator = Evaluator(base_cfg)
    test = load_split(base_cfg, "test")
    for name, (path, state) in states.items():
        config_start = time.perf_counter()
        evaluation = evaluator.evaluate(state, test, f"heldout {name}")
        items = evaluation.to_dict(with_items=True)
        write_json(items, run_dir / f"{name}_test_items.json")
        write_json(items, configs_dir / f"{name}_test_items.json")
        entry = {
            "config_path": project_relative(path),
            "fingerprint": fingerprints[name],
            "enabled_mitigations": describe_mitigations(state["mitigations"]),
            "learned_rules": state["system_prompts"].get("learned_rules", ""),
            "test": _test_entry(evaluation),
            "heldout": None if skip_heldout else _heldout(state, final_cfg, run_dir, name),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "run_dir": project_relative(run_dir),
            "forced": name in changed,
        }
        entry["duration_s"] = time.perf_counter() - config_start
        write_json(entry, configs_dir / f"{name}.json")
        record = log["configs"].setdefault(name, {"fingerprint": fingerprints[name], "runs": []})
        record["fingerprint"] = fingerprints[name]
        record["runs"].append({"timestamp": entry["timestamp"], "run_dir": entry["run_dir"],
                               "forced": entry["forced"], "heldout": not skip_heldout})
        write_json(log, log_path)
        heldout = entry["heldout"]
        logger.info(
            "HELD-OUT %-14s | test ASR (all) %s | benign FPR %s | held-out ASR %s | %.1fs", name,
            _pct_ci(entry["test"]["asr_micro"], entry["test"]["asr_micro_ci"]),
            _pct_ci(entry["test"]["fpr"], entry["test"]["fpr_ci"]),
            _pct_ci(heldout["asr"], heldout["ci"]) if heldout else "not run", entry["duration_s"],
        )
    summary = rebuild_summary(base_cfg, out_dir)
    write_json(summary, run_dir / "summary.json")
    logger.info("HELD-OUT TEST END in %.1f min | all tested configs merged into %s\n%s",
                (time.perf_counter() - start) / 60, project_relative(out_dir / "summary.json"),
                format_final_table(summary))
    return summary