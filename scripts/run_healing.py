from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from selfheal.config import ConfigError, load_config, resolve_project_path, validate_config
from selfheal.garak_runner import ScanError
from selfheal.healer import HealingLoop
from selfheal.logging_setup import get_logger, setup_logging
from selfheal.ollama_client import OllamaError

logger = get_logger("scripts.run_healing")


def format_healing_table(summary: dict[str, Any]) -> str:
    """Plain-text table: one row per healing iteration."""
    categories = list(summary["iterations"][0]["categories"])
    header = f"{'iter':<5}{'status':<34}" + "".join(f"{c[:17]:>19}" for c in categories)
    header += f"{'overall':>10}{'FPR':>8}{'time(s)':>9}"
    lines = [header]
    for it in summary["iterations"]:
        cells = ""
        for category in categories:
            metrics = it["categories"][category]
            mark = "*" if metrics["carried_over"] and it["iteration"] > 0 else ""
            cells += f"{metrics['asr'] * 100:>18.1f}%"[:-1] + f"%{mark}".ljust(1)
        lines.append(
            f"{it['iteration']:<5}{it['label'][:33]:<34}{cells}"
            f"{it['overall_asr_macro'] * 100:>9.1f}%{it['benign_fpr'] * 100:>7.1f}%"
            f"{it['duration_s']:>9.1f}"
        )
    lines.append("(* = carried over from an earlier iteration, not re-scanned)")
    lines.append(f"converged={summary['converged']} | {summary['stop_reason']} | "
                 f"total {summary['total_duration_s']:.1f}s")
    lines.append(f"healed config: {summary['healed_config_path']}")
    return "\n".join(lines)

def format_healing_table_2(summary: dict[str, Any]) -> str:
    """Plain-text table: one row per healing iteration."""
    categories = list(summary["iterations"][0]["categories"])
    header = f"{'iter':<5}{'status':<34}" + "".join(f"{c[:17]:>19}" for c in categories)
    header += f"{'overall':>10}{'FPR':>8}{'time(s)':>9}"
    lines = [header]
    for it in summary["iterations"]:
        cells = ""
        for category in categories:
            metrics = it["categories"][category]
            mark = "*" if metrics["carried_over"] and it["iteration"] > 0 else " "
            cells += f"{metrics['asr'] * 100:>17.1f}%{mark}"
        lines.append(
            f"{it['iteration']:<5}{it['label'][:33]:<34}{cells}"
            f"{it['overall_asr_macro'] * 100:>9.1f}%{it['benign_fpr'] * 100:>7.1f}%"
            f"{it['duration_s']:>9.1f}"
        )
    lines.append("(* = carried over from an earlier iteration, not re-scanned)")
    lines.append(f"converged={summary['converged']} | {summary['stop_reason']} | "
                 f"total {summary['total_duration_s']:.1f}s")
    lines.append(f"healed config: {summary['healed_config_path']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the self-healing loop.")
    parser.add_argument("--config", default="configs/healing.yaml")
    parser.add_argument("--reuse-baseline", action="store_true",
                        help="reuse results/experiments/baseline/summary.json if compatible")
    parser.add_argument("--baseline-summary", type=Path,
                        help="explicit path of a baseline summary.json to reuse")
    parser.add_argument("--log-level", help="override logging.level, e.g. DEBUG")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
        if args.log_level:
            cfg["logging"]["level"] = args.log_level.upper()
        validate_config(cfg)
        if "healing" not in cfg:
            raise ConfigError(f"{args.config} has no 'healing' section")
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    log_file = setup_logging(cfg["logging"]["level"], resolve_project_path(cfg["logging"]["dir"]),
                             "healing")
    logger.info("Log file: %s", log_file)
    results_root = resolve_project_path(cfg["paths"]["results_dir"])
    reuse = args.baseline_summary
    if reuse is None and args.reuse_baseline:
        reuse = results_root / "experiments" / "baseline" / "summary.json"

    try:
        summary = HealingLoop(cfg, results_root, reuse_baseline=reuse).run()
    except OllamaError:
        logger.exception("Step 'Ollama check/generation' failed during healing")
        return 1
    except ScanError:
        logger.exception("Step 'garak scan' failed during healing")
        return 1
    except Exception:
        logger.exception("Healing loop failed with an unexpected error")
        return 1
    logger.info("Healing iterations\n%s", format_healing_table_2(summary))
    logger.info("Next: python -m scripts.run_experiment --config %s", summary["healed_config_path"])
    return 0


if __name__ == "__main__":
    sys.exit(main())