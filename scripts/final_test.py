from __future__ import annotations

import argparse
import sys
from pathlib import Path

from selfheal.config import (
    PROJECT_ROOT,
    ConfigError,
    load_config,
    resolve_project_path,
    validate_config,
)
from selfheal.final_test import TestAlreadyUsedError, run_final_test
from selfheal.garak_runner import ScanError
from selfheal.garak_scoring import ScoringError
from selfheal.logging_setup import get_logger, setup_logging
from selfheal.ollama_client import OllamaError

logger = get_logger("scripts.final_test")

DEFAULT_CONFIGS = ["baseline", "prompt_only", "output_only", "both", "healed_search"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One-shot final test on held-out data.")
    parser.add_argument("--base-config", default="configs/base.yaml")
    parser.add_argument("--configs", nargs="+", default=DEFAULT_CONFIGS,
                        help="names of files in configs/experiments/")
    parser.add_argument("--force", action="store_true",
                        help="allow re-testing changed configs (marked as forced)")
    parser.add_argument("--skip-heldout", action="store_true",
                        help="skip the real garak scans of held-out probe families")
    args = parser.parse_args(argv)
    try:
        base = load_config(args.base_config)
        validate_config(base)
        configs: dict[str, tuple[Path, dict]] = {}
        for name in args.configs:
            path = PROJECT_ROOT / "configs" / "experiments" / f"{name}.yaml"
            if not path.exists():
                raise ConfigError(f"{path} not found (run the healing loop first for healed configs)")
            cfg = load_config(path)
            validate_config(cfg)
            configs[name] = (path, cfg)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    log_file = setup_logging(base["logging"]["level"], resolve_project_path(base["logging"]["dir"]),
                             "final_test")
    logger.info("Log file: %s", log_file)
    try:
        run_final_test(base, configs, force=args.force, skip_heldout=args.skip_heldout)
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
        logger.exception("Step 'held-out garak scan' failed in the final test")
        return 1
    except (OllamaError, ScoringError):
        logger.exception("Step 'test-split evaluation' failed in the final test")
        return 1
    except Exception:
        logger.exception("Final test failed with an unexpected error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())