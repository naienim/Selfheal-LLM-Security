from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from selfheal.config import PROJECT_ROOT, save_yaml
from selfheal.garak_target import ENV_CONFIG, ENV_LOG, ENV_TRACE

logger = logging.getLogger(__name__)

TARGET_TYPE = "function.Single"
TARGET_NAME = "selfheal.garak_target#generate"


class ScanError(RuntimeError):
    """Raised when a garak scan fails or produces no report."""


@dataclass
class ScanResult:
    """Files and timing of one garak category scan."""

    category: str
    probes: list[str]
    generations: int
    report_path: Path
    hitlog_path: Path | None
    html_path: Path | None
    console_log: Path
    trace_path: Path
    target_log: Path
    duration_s: float
    returncode: int


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text)


def build_garak_config(cfg: dict[str, Any], report_dir: Path) -> dict[str, Any]:
    """garak YAML for one scan: sequential (no multiprocessing on Windows), capped, fixed dir."""
    garak_cfg = cfg["garak"]
    return {
        "system": {
            "parallel_attempts": False,
            "parallel_requests": False,
            "lite": True,
            "show_z": False,
            "narrow_output": True,
        },
        "run": {
            "eval_threshold": float(garak_cfg.get("eval_threshold", 0.5)),
            "soft_probe_prompt_cap": int(garak_cfg.get("prompt_cap", 20)),
            "deprefix": True,
        },
        "plugins": {"extended_detectors": False},
        "reporting": {"report_dir": report_dir.resolve().as_posix()},
    }


def _build_env(
    resolved_config: Path,
    trace_path: Path,
    target_log: Path,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(PROJECT_ROOT) + (os.pathsep + existing if existing else "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    env.setdefault("TOKENIZERS_PARALLELISM", "false")
    env[ENV_CONFIG] = str(resolved_config)
    env[ENV_TRACE] = str(trace_path)
    env[ENV_LOG] = str(target_log)
    env.update(extra_env or {})
    return env


def _default_garak_dirs() -> list[Path]:
    dirs = [Path.home() / ".local" / "share" / "garak" / "garak_runs"]
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        dirs.insert(0, Path(xdg) / "garak" / "garak_runs")
    return dirs


def _find_report(scan_dir: Path, prefix: str) -> Path | None:
    expected = scan_dir / f"{prefix}.report.jsonl"
    if expected.exists():
        return expected
    local = sorted(scan_dir.glob("*.report.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if local:
        return local[0]
    for base in _default_garak_dirs():
        candidate = base / f"{prefix}.report.jsonl"
        if candidate.exists():
            logger.warning("garak ignored report_dir; using report at %s", candidate)
            return candidate
    return None


def _tail(path: Path, lines: int = 40) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(console log unavailable)"
    return "\n".join(content[-lines:])


def run_category_scan(
    cfg: dict[str, Any],
    category: str,
    resolved_config: Path,
    run_dir: Path,
    log_dir: Path,
    label: str,
    extra_env: dict[str, str] | None = None,
) -> ScanResult:
    """Run garak for one category of probes and return the paths of its outputs."""
    category_cfg = cfg["garak"]["categories"][category]
    probes = [str(p) for p in category_cfg["probes"]]
    generations = int(category_cfg.get("generations", 1))
    scan_dir = run_dir / category
    scan_dir.mkdir(parents=True, exist_ok=True)
    prefix = _safe_name(f"{label}_{category}")

    garak_cfg_path = save_yaml(build_garak_config(cfg, scan_dir), scan_dir / "garak_config.yaml")
    console_log = scan_dir / "garak_console.log"
    trace_path = scan_dir / "trace.jsonl"
    if trace_path.exists():
        trace_path.unlink()
    log_dir.mkdir(parents=True, exist_ok=True)
    target_log = log_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{prefix}_target.log"

    cmd = [
        sys.executable, "-m", "garak",
        "--config", str(garak_cfg_path),
        "--target_type", TARGET_TYPE,
        "--target_name", TARGET_NAME,
        "--probes", ",".join(probes),
        "--generations", str(generations),
        "--seed", str(int(cfg.get("seed", 42))),
        "--report_prefix", prefix,
    ]
    env = _build_env(resolved_config, trace_path, target_log, extra_env)
    timeout = float(cfg["garak"].get("scan_timeout_s", 3600))

    logger.info(
        "Garak scan START | category=%s | probes=%s | generations=%d | prompt_cap=%s%s",
        category,
        ",".join(probes),
        generations,
        cfg["garak"].get("prompt_cap"),
        " | record-only" if extra_env else "",
    )
    logger.debug("Garak command: %s", subprocess.list2cmdline(cmd))
    start = time.perf_counter()
    try:
        with console_log.open("wb") as handle:
            proc = subprocess.run(
                cmd,
                stdout=handle,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=str(PROJECT_ROOT),
                timeout=timeout,
                check=False,
            )
    except subprocess.TimeoutExpired as exc:
        raise ScanError(
            f"garak scan for '{category}' exceeded {timeout:.0f}s; see {console_log}"
        ) from exc
    except OSError as exc:
        raise ScanError(f"Could not start garak ({exc}). Is garak installed in this venv?") from exc
    duration = time.perf_counter() - start

    report = _find_report(scan_dir, prefix)
    if report is None:
        logger.error(
            "Garak scan FAILED | category=%s | returncode=%s | console log %s, last lines:\n%s",
            category,
            proc.returncode,
            console_log,
            _tail(console_log),
        )
        raise ScanError(
            f"garak produced no report for '{category}' (exit code {proc.returncode}); "
            f"see {console_log} and {target_log}"
        )
    if proc.returncode != 0:
        logger.warning(
            "garak exited with code %d for '%s' but wrote a report (may be partial); "
            "see %s",
            proc.returncode,
            category,
            console_log,
        )
    hitlog = report.with_name(report.name.replace(".report.jsonl", ".hitlog.jsonl"))
    html = report.with_name(report.name.replace(".report.jsonl", ".report.html"))
    logger.info(
        "Garak scan END   | category=%s | duration=%.1fs | report=%s", category, duration, report
    )
    return ScanResult(
        category=category,
        probes=probes,
        generations=generations,
        report_path=report,
        hitlog_path=hitlog if hitlog.exists() else None,
        html_path=html if html.exists() else None,
        console_log=console_log,
        trace_path=trace_path,
        target_log=target_log,
        duration_s=duration,
        returncode=proc.returncode,
    )