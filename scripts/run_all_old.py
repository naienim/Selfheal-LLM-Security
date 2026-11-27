from __future__ import annotations

import argparse
import sys
import time
from typing import Callable

from scripts.analyze import run_analysis
from selfheal.config import (
    PROJECT_ROOT,
    ConfigError,
    load_config,
    resolve_project_path,
    validate_config,
)
from selfheal.experiment import format_summary_table, run_experiment
from selfheal.healer import HEALED_CONFIG_PATH, HealingLoop
from selfheal.logging_setup import get_logger, setup_logging

logger = get_logger("scripts.run_all")

EXPERIMENTS_DIR = PROJECT_ROOT / "configs" / "experiments"
ABLATIONS = ["baseline", "prompt_only", "output_only", "both"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run every experiment, healing and analysis.")
    parser.add_argument("--skip-existing", action="store_true",
                        help="skip steps whose results already exist")
    parser.add_argument("--log-level", help="override logging.level, e.g. DEBUG")
    args = parser.parse_args(argv)

    try:
        base = load_config(PROJECT_ROOT / "configs" / "base.yaml")
        validate_config(base)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    level = (args.log_level or base["logging"]["level"]).upper()
    log_file = setup_logging(level, resolve_project_path(base["logging"]["dir"]), "run_all")
    logger.info("Log file: %s", log_file)
    results_root = resolve_project_path(base["paths"]["results_dir"])

    def load(path) -> dict:
        cfg = load_config(path)
        cfg["logging"]["level"] = level
        validate_config(cfg)
        return cfg

    def experiment_step(name: str) -> Callable[[], None]:
        def run() -> None:
            summary_path = results_root / "experiments" / name / "summary.json"
            fresh = summary_path.exists() and (
                name != "healed"
                or summary_path.stat().st_mtime > HEALED_CONFIG_PATH.stat().st_mtime
            )
            if args.skip_existing and fresh:
                logger.info("Skipping experiment '%s' (results exist: %s)", name, summary_path)
                return
            summary = run_experiment(load(EXPERIMENTS_DIR / f"{name}.yaml"), results_root)
            logger.info("Summary\n%s", format_summary_table(summary))
        return run

    def healing_step() -> None:
        healing_summary = results_root / "healing" / "summary.json"
        if args.skip_existing and healing_summary.exists() and HEALED_CONFIG_PATH.exists():
            logger.info("Skipping healing (results exist: %s)", healing_summary)
            return
        baseline_summary = results_root / "experiments" / "baseline" / "summary.json"
        HealingLoop(load(PROJECT_ROOT / "configs" / "healing.yaml"), results_root,
                    reuse_baseline=baseline_summary).run()

    steps: list[tuple[str, Callable[[], object]]] = [
        (f"experiment:{name}", experiment_step(name)) for name in ABLATIONS
    ]
    steps += [
        ("healing", healing_step),
        ("experiment:healed", experiment_step("healed")),
        ("analysis", lambda: run_analysis(results_root)),
    ]

    timings: dict[str, float] = {}
    for step_name, step in steps:
        start = time.perf_counter()
        logger.info("=== STEP %s START ===", step_name)
        try:
            step()
        except Exception:
            logger.exception(
                "Step '%s' failed; stopping. Completed steps are kept - rerun with "
                "--skip-existing to resume.", step_name
            )
            return 1
        timings[step_name] = time.perf_counter() - start
        logger.info("=== STEP %s END (%.1fs) ===", step_name, timings[step_name])

    table = "\n".join(f"  {name:<24}{seconds / 60:>7.1f} min" for name, seconds in timings.items())
    logger.info("All steps done in %.1f min\n%s", sum(timings.values()) / 60, table)
    return 0


if __name__ == "__main__":
    sys.exit(main())