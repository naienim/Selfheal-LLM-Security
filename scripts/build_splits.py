from __future__ import annotations

import argparse
import sys

from selfheal.config import ConfigError, load_config, resolve_project_path, validate_config
from selfheal.datasets import build_splits, format_manifest, load_manifest
from selfheal.garak_runner import ScanError
from selfheal.logging_setup import get_logger, setup_logging

logger = get_logger("scripts.build_splits")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record garak prompts and build fixed splits.")
    parser.add_argument("--config", default="configs/base.yaml")
    parser.add_argument("--force", action="store_true", help="overwrite existing splits")
    parser.add_argument("--show", action="store_true", help="only print the existing manifest")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        validate_config(cfg)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    log_file = setup_logging(cfg["logging"]["level"], resolve_project_path(cfg["logging"]["dir"]),
                             "build_splits")
    logger.info("Log file: %s", log_file)
    try:
        if args.show:
            logger.info("\n%s", format_manifest(load_manifest(cfg)))
            return 0
        build_splits(cfg, resolve_project_path(cfg["paths"]["results_dir"]), force=args.force)
    except FileExistsError as exc:
        logger.error("%s", exc)
        return 2
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    except ScanError:
        logger.exception("Step 'garak recorder run' failed while building splits")
        return 1
    except Exception:
        logger.exception("Building splits failed with an unexpected error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())