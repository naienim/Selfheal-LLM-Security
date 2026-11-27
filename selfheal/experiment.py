from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from selfheal.benign_eval import evaluate_benign, load_benign_prompts
from selfheal.config import (
    describe_mitigations,
    flatten,
    project_relative,
    resolve_project_path,
    save_yaml,
)
from selfheal.garak_runner import run_category_scan
from selfheal.ollama_client import OllamaClient
from selfheal.pipeline import DefendedPipeline
from selfheal.report_parser import parse_report, summarize_category

logger = logging.getLogger(__name__)


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def write_json(data: Any, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def garak_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """Settings that must match for two scans to be comparable."""
    return {
        "categories": cfg["garak"]["categories"],
        "prompt_cap": cfg["garak"].get("prompt_cap"),
        "eval_threshold": cfg["garak"].get("eval_threshold", 0.5),
        "seed": cfg.get("seed"),
    }


def overall_asr(categories: dict[str, dict[str, Any]]) -> tuple[float, float]:
    """(macro, micro) ASR: macro averages categories equally, micro pools all outputs."""
    rates = [c["asr"] for c in categories.values() if c.get("total", 0) > 0]
    macro = sum(rates) / len(rates) if rates else 0.0
    fails = sum(c.get("fails", 0) for c in categories.values())
    total = sum(c.get("total", 0) for c in categories.values())
    return macro, (fails / total if total else 0.0)


def run_scans(
    cfg: dict[str, Any],
    run_dir: Path,
    categories: list[str] | None = None,
    label: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Scan each category with garak and return parsed per-category metrics."""
    run_dir.mkdir(parents=True, exist_ok=True)
    resolved = save_yaml(cfg, run_dir / "resolved_config.yaml")
    log_dir = resolve_project_path(cfg["logging"]["dir"])
    threshold = float(cfg["garak"].get("eval_threshold", 0.5))
    results: dict[str, dict[str, Any]] = {}
    for category in categories or list(cfg["garak"]["categories"]):
        scan = run_category_scan(cfg, category, resolved, run_dir, log_dir, label or cfg["name"])
        metrics = summarize_category(parse_report(scan.report_path, threshold))
        metrics.update(
            {
                "duration_s": scan.duration_s,
                "generations": scan.generations,
                "probes_requested": scan.probes,
                "report_path": project_relative(scan.report_path),
                "hitlog_path": project_relative(scan.hitlog_path) if scan.hitlog_path else None,
                "html_path": project_relative(scan.html_path) if scan.html_path else None,
                "trace_path": project_relative(scan.trace_path),
                "console_log": project_relative(scan.console_log),
                "target_log": project_relative(scan.target_log),
            }
        )
        logger.info(
            "Category %-16s ASR=%5.1f%% (%d/%d outputs, %d prompts) in %.1fs",
            category,
            metrics["asr"] * 100,
            metrics["fails"],
            metrics["total"],
            metrics["attempts"],
            scan.duration_s,
        )
        results[category] = metrics
    return results


def run_benign(cfg: dict[str, Any], out_path: Path | None = None) -> dict[str, Any]:
    """Measure the benign false-positive rate of the configured pipeline."""
    prompts = load_benign_prompts(
        resolve_project_path(cfg["benign"]["prompts_file"]), cfg["benign"].get("limit")
    )
    pipeline = DefendedPipeline(cfg)
    pipeline.client.check_ready()
    pipeline.client.warmup()
    logger.info(
        "Benign evaluation START | %d prompts | mitigations: %s",
        len(prompts),
        ", ".join(describe_mitigations(cfg["mitigations"])),
    )
    report = evaluate_benign(pipeline, prompts)
    items = report.pop("items")
    if out_path is not None:
        write_json({"metrics": report, "items": items}, out_path)
    logger.info(
        "Benign evaluation END | FPR=%.1f%% (%d/%d) | blocked=%d replaced=%d refusals=%d | %.1fs",
        report["fpr"] * 100,
        report["false_positives"],
        report["n"],
        report["blocked_input"],
        report["replaced_output"],
        report["model_refusals"],
        report["duration_s"],
    )
    return report


def run_experiment(
    cfg: dict[str, Any], results_root: Path, categories: list[str] | None = None
) -> dict[str, Any]:
    """Full experiment: garak scans for every category + benign FPR -> summary.json."""
    name = cfg["name"]
    run_dir = results_root / "experiments" / name / f"run_{timestamp()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    enabled = describe_mitigations(cfg["mitigations"])
    logger.info(
        "Experiment '%s' START | model=%s | mitigations: %s | run_dir=%s",
        name,
        cfg["target"]["model"],
        ", ".join(enabled),
        project_relative(run_dir),
    )
    OllamaClient.from_config(cfg["target"]).check_ready()

    start = time.perf_counter()
    category_metrics = run_scans(cfg, run_dir, categories=categories, label=name)
    scan_s = time.perf_counter() - start
    benign_start = time.perf_counter()
    benign = run_benign(cfg, out_path=run_dir / "benign_results.json")
    benign_s = time.perf_counter() - benign_start
    macro, micro = overall_asr(category_metrics)

    summary = {
        "name": name,
        "model": cfg["target"]["model"],
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "config_source": project_relative(cfg.get("_source", "")),
        "run_dir": project_relative(run_dir),
        "mitigations": cfg["mitigations"],
        "enabled_mitigations": enabled,
        "garak_settings": garak_settings(cfg),
        "categories": category_metrics,
        "overall_asr_macro": macro,
        "overall_asr_micro": micro,
        "benign": benign,
        "timings": {"scan_s": scan_s, "benign_s": benign_s, "total_s": scan_s + benign_s},
    }
    write_json(summary, run_dir / "summary.json")
    write_json(summary, results_root / "experiments" / name / "summary.json")
    logger.info(
        "Experiment '%s' END | overall ASR (macro)=%.1f%% | benign FPR=%.1f%% | total %.1fs",
        name,
        macro * 100,
        benign["fpr"] * 100,
        scan_s + benign_s,
    )
    return summary


def load_summary_if_compatible(path: Path, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Return a previous experiment summary if it matches ``cfg`` (same model/settings)."""
    if not path.exists():
        logger.warning("No summary to reuse at %s; running a fresh baseline", path)
        return None
    summary = read_json(path)
    problems = []
    if summary.get("model") != cfg["target"]["model"]:
        problems.append("model differs")
    if flatten(summary.get("mitigations", {})) != flatten(cfg["mitigations"]):
        problems.append("mitigations differ")
    if summary.get("garak_settings") != garak_settings(cfg):
        problems.append("garak probe settings differ")
    if problems:
        logger.warning("Cannot reuse %s (%s); running a fresh baseline", path, "; ".join(problems))
        return None
    return summary


def format_summary_table(summary: dict[str, Any]) -> str:
    """Plain-text summary printed at the end of an experiment."""
    lines = [
        f"Experiment: {summary['name']}  (model: {summary['model']})",
        f"Mitigations: {', '.join(summary['enabled_mitigations'])}",
        f"{'category':<18}{'ASR':>8}{'hits/total':>14}{'time (s)':>10}",
    ]
    for category, metrics in summary["categories"].items():
        hits = f"{metrics['fails']}/{metrics['total']}"
        lines.append(
            f"{category:<18}{metrics['asr'] * 100:>7.1f}%{hits:>14}{metrics['duration_s']:>10.1f}"
        )
    lines.append(f"{'overall (macro)':<18}{summary['overall_asr_macro'] * 100:>7.1f}%")
    benign = summary["benign"]
    lines.append(
        f"Benign false-positive rate: {benign['fpr'] * 100:.1f}% "
        f"({benign['false_positives']}/{benign['n']})"
    )
    lines.append(f"Total time: {summary['timings']['total_s']:.1f}s")
    return "\n".join(lines)