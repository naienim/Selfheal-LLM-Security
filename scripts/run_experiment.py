from __future__ import annotations

import argparse
import sys
from pathlib import Path

from selfheal.config import ConfigError, load_config, resolve_project_path, validate_config
from selfheal.final_test import TestAlreadyUsedError, format_final_table, run_final_test
from selfheal.garak_runner import ScanError
from selfheal.garak_scoring import ScoringError
from selfheal.logging_setup import get_logger, setup_logging
from selfheal.ollama_client import OllamaError

logger = get_logger("scripts.run_experiment")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate one experiment on held-out data.")
    parser.add_argument("--config", required=True, help="experiment YAML file")
    parser.add_argument("--skip-heldout", action="store_true",
                        help="test split only (skip the held-out garak family scans)")
    parser.add_argument("--force", action="store_true",
                        help="allow re-testing a config that changed since its last test")
    parser.add_argument("--log-level", help="override logging.level, e.g. DEBUG")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.log_level:
            cfg["logging"]["level"] = args.log_level.upper()
        validate_config(cfg)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    name = cfg["name"]
    log_file = setup_logging(cfg["logging"]["level"], resolve_project_path(cfg["logging"]["dir"]),
                             f"experiment_{name}")
    logger.info("Log file: %s", log_file)
    try:
        summary = run_final_test(cfg, {name: (Path(cfg["_source"]), cfg)},
                                 force=args.force, skip_heldout=args.skip_heldout)
    except TestAlreadyUsedError as exc:
        logger.error("%s", exc)
        return 3
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    except ConfigError as exc:
        logger.error("Config error: %s", exc)
        return 2
    except ScanError:
        logger.exception("Step 'held-out garak scan' failed in experiment '%s'", name)
        return 1
    except (OllamaError, ScoringError):
        logger.exception("Step 'test-split evaluation' failed in experiment '%s'", name)
        return 1
    except Exception:
        logger.exception("Experiment '%s' failed with an unexpected error", name)
        return 1
    logger.info("All held-out results so far:\n%s", format_final_table(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())