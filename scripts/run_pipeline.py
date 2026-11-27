from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from typing import Callable

from selfheal.config import (
    PROJECT_ROOT,
    ConfigError,
    load_config,
    resolve_project_path,
    validate_config,
)
from selfheal.experiment import read_json
from selfheal.final_test import fingerprint
from selfheal.logging_setup import get_logger, setup_logging

logger = get_logger("scripts.run_pipeline")

EXPERIMENTS_DIR = PROJECT_ROOT / "configs" / "experiments"
ABLATIONS = ["baseline", "prompt_only", "output_only", "both"]
HEALED = "healed_search"


def _run(module: str, args: list[str]) -> int:
    """Run one step as `python -m <module> <args>`; output goes straight to this console."""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, "-m", module, *args]
    logger.info("Command: %s", subprocess.list2cmdline(cmd))
    return subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env, check=False).returncode


def _experiment_up_to_date(base: dict, name: str, need_heldout: bool) -> bool:
    """True if this config was already tested on held-out data and has not changed since."""
    config_path = EXPERIMENTS_DIR / f"{name}.yaml"
    out_dir = resolve_project_path((base.get("final_test") or {}).get("dir", "results/final_test"))
    result_path = out_dir / "configs" / f"{name}.json"
    if not config_path.exists() or not result_path.exists():
        return False
    entry = read_json(result_path)
    if entry.get("fingerprint") != fingerprint(load_config(config_path)):
        return False
    return entry.get("heldout") is not None or not need_heldout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the whole new workflow end to end.")
    parser.add_argument("--skip-existing", action="store_true",
                        help="skip steps whose results already exist and are up to date")
    parser.add_argument("--skip-spike", action="store_true", help="skip the garak API spike")
    parser.add_argument("--skip-heldout", action="store_true",
                        help="experiments on the test split only (no held-out garak scans)")
    parser.add_argument("--max-iterations", type=int,
                        help="override search.max_iterations for the search loop")
    parser.add_argument("--log-level", help="override logging.level for every step, e.g. DEBUG")
    args = parser.parse_args(argv)

    try:
        base = load_config(PROJECT_ROOT / "configs" / "base.yaml")
        validate_config(base)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    level = (args.log_level or base["logging"]["level"]).upper()
    log_file = setup_logging(level, resolve_project_path(base["logging"]["dir"]), "run_pipeline")
    logger.info("Log file: %s", log_file)

    level_args = ["--log-level", level] if args.log_level else []
    heldout_args = ["--skip-heldout"] if args.skip_heldout else []
    need_heldout = not args.skip_heldout
    splits_manifest = resolve_project_path(base["splits"]["dir"]) / "manifest.json"
    search_summary = resolve_project_path(base["paths"]["results_dir"]) / "search" / "summary.json"

    def experiment_step(name: str) -> tuple[str, str, list[str], Callable[[], bool]]:
        return (f"experiment:{name}", "scripts.run_experiment",
                ["--config", f"configs/experiments/{name}.yaml", *heldout_args, *level_args],
                lambda: _experiment_up_to_date(base, name, need_heldout))

    heal_args = [*level_args]
    if args.max_iterations:
        heal_args += ["--max-iterations", str(args.max_iterations)]

    steps: list[tuple[str, str, list[str], Callable[[], bool]]] = []
    if not args.skip_spike:
        steps.append(("spike", "scripts.spike_garak_detectors", [], lambda: False))
    steps.append(("build_splits", "scripts.build_splits", [], splits_manifest.exists))
    steps += [experiment_step(name) for name in ABLATIONS]
    steps.append(("heal_search", "scripts.heal_search", heal_args,
                  lambda: search_summary.exists() and (EXPERIMENTS_DIR / f"{HEALED}.yaml").exists()))
    steps.append(experiment_step(HEALED))
    steps.append(("analyze", "scripts.analyze", [], lambda: False))

    if not args.skip_existing and splits_manifest.exists():
        logger.info("Splits already exist: build_splits is skipped (data stays fixed; "
                    "rebuild manually with: python -m scripts.build_splits --force)")

    timings: dict[str, tuple[str, float]] = {}
    pipeline_start = time.perf_counter()
    for name, module, step_args, already_done in steps:
        if name == "build_splits" and splits_manifest.exists():
            timings[name] = ("skipped", 0.0)
            continue
        if args.skip_existing and already_done():
            logger.info("=== STEP %s SKIPPED (results exist and are up to date) ===", name)
            timings[name] = ("skipped", 0.0)
            continue
        logger.info("=== STEP %s START ===", name)
        start = time.perf_counter()
        try:
            code = _run(module, step_args)
        except OSError:
            logger.exception("Step '%s' could not be started", name)
            return 1
        elapsed = time.perf_counter() - start
        if code != 0:
            timings[name] = (f"FAILED (exit {code})", elapsed)
            hint = (" The test guard refused a changed config; see that step's log."
                    if code == 3 else " See that step's log file in logs/.")
            logger.error("=== STEP %s FAILED with exit code %d after %.1f min.%s Completed "
                         "steps are kept; rerun with --skip-existing to resume. ===",
                         name, code, elapsed / 60, hint)
            break
        timings[name] = ("ok", elapsed)
        logger.info("=== STEP %s END (%.1f min) ===", name, elapsed / 60)

    table = "\n".join(f"  {name:<26}{status:<20}{seconds / 60:>7.1f} min"
                      for name, (status, seconds) in timings.items())
    failed = any(status.startswith("FAILED") for status, _ in timings.values())
    logger.info("Pipeline %s in %.1f min\n%s", "STOPPED" if failed else "finished",
                (time.perf_counter() - pipeline_start) / 60, table)
    if not failed:
        logger.info("Results: results\\final_test\\final_test.md (held-out table), "
                    "results\\final_test\\examples.md, results\\analysis\\, README Results section")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())