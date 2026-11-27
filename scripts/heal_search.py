from __future__ import annotations

import argparse
import sys

from selfheal.config import ConfigError, load_config, resolve_project_path, validate_config
from selfheal.garak_scoring import ScoringError
from selfheal.logging_setup import get_logger, setup_logging
from selfheal.ollama_client import OllamaError
from selfheal.search_healer import PHASES, SearchHealer, format_search_table, validate_search_cfg

logger = get_logger("scripts.heal_search")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Search-based self-healing loop.")
    parser.add_argument("--config", default="configs/healing_search.yaml")
    parser.add_argument("--phase", choices=PHASES, default="all")
    parser.add_argument("--max-iterations", type=int)
    parser.add_argument("--start-config", help="start from this config's mitigations/learned rules")
    parser.add_argument("--log-level", help="override logging.level, e.g. DEBUG")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.log_level:
            cfg["logging"]["level"] = args.log_level.upper()
        validate_config(cfg)
        validate_search_cfg(cfg)
        start_cfg = load_config(args.start_config) if args.start_config else None
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    log_file = setup_logging(cfg["logging"]["level"], resolve_project_path(cfg["logging"]["dir"]),
                             f"heal_search_{args.phase}")
    logger.info("Log file: %s", log_file)
    try:
        summary = SearchHealer(cfg, resolve_project_path(cfg["paths"]["results_dir"]),
                               phase=args.phase, max_iterations=args.max_iterations,
                               start_cfg=start_cfg).run()
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    except ConfigError as exc:
        logger.error("Config error: %s", exc)
        return 2
    except OllamaError:
        logger.exception("Step 'Ollama generation' failed during the search")
        return 1
    except ScoringError:
        logger.exception("Step 'garak detector scoring' failed during the search")
        return 1
    except Exception:
        logger.exception("Search loop failed with an unexpected error")
        return 1
    logger.info("Search iterations\n%s", format_search_table(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())