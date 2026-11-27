from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT_LOGGER = "selfheal"
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _force_utf8_console() -> None:
    """Avoid UnicodeEncodeError on Windows consoles that default to cp1252."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def setup_logging(
    level: str = "INFO",
    log_dir: str | Path = "logs",
    run_name: str = "run",
    log_file: str | Path | None = None,
    console: bool = True,
) -> Path:
    logger = logging.getLogger(ROOT_LOGGER)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    if log_file is None:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = directory / f"{stamp}_{run_name}.log"
    else:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if console:
        _force_utf8_console()
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    return log_path


def get_logger(name: str) -> logging.Logger:
    if name != ROOT_LOGGER and not name.startswith(ROOT_LOGGER + "."):
        name = f"{ROOT_LOGGER}.{name}"
    return logging.getLogger(name)