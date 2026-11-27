from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from selfheal.config import load_config, resolve_project_path
from selfheal.logging_setup import setup_logging
from selfheal.pipeline import DefendedPipeline, PipelineResult, short_id

ENV_CONFIG = "SELFHEAL_RUN_CONFIG"
ENV_TRACE = "SELFHEAL_TRACE_FILE"
ENV_LOG = "SELFHEAL_LOG_FILE"
ENV_RECORD = "SELFHEAL_RECORD_ONLY"

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_PIPELINE: DefendedPipeline | None = None
_TRACE_PATH: Path | None = None


def _initialise() -> DefendedPipeline:
    global _PIPELINE, _TRACE_PATH
    config_path = os.environ.get(ENV_CONFIG)
    if not config_path:
        raise RuntimeError(
            f"{ENV_CONFIG} is not set; run scans through selfheal.garak_runner, not garak directly."
        )
    cfg = load_config(config_path)
    setup_logging(
        level=cfg["logging"]["level"],
        log_dir=resolve_project_path(cfg["logging"]["dir"]),
        run_name="garak_target",
        log_file=os.environ.get(ENV_LOG),
        console=False,
    )
    logger.info("garak target initialising | config=%s | pid=%d", config_path, os.getpid())
    from selfheal.cache import DiskCache
    from selfheal.evaluator import CachingOllamaClient
    from selfheal.guards import classifiers

    cache_dir = resolve_project_path((cfg.get("evaluator") or {}).get("cache_dir", "results/cache"))
    client = CachingOllamaClient.from_target(
        cfg["target"], DiskCache(cache_dir / "responses.jsonl", "responses"))
    classifiers.set_score_cache(DiskCache(cache_dir / "classifier_scores.jsonl",
                                          "classifier_scores"))
    pipeline = DefendedPipeline(cfg, client=client)
    pipeline.client.check_ready()
    pipeline.client.warmup()
    trace = os.environ.get(ENV_TRACE)
    _TRACE_PATH = Path(trace) if trace else None
    _PIPELINE = pipeline
    logger.info("garak target uses the shared response cache in %s (paired answers)", cache_dir)
    return pipeline


def _get_pipeline() -> DefendedPipeline:
    if _PIPELINE is None:
        with _LOCK:
            if _PIPELINE is None:
                try:
                    _initialise()
                except Exception:
                    logger.exception(
                        "Step 'garak target initialisation' failed; this garak scan will abort"
                    )
                    raise
    assert _PIPELINE is not None
    return _PIPELINE


def _prompt_to_text(prompt: Any) -> str:
    """Accept a plain string (current function generator) or a garak Conversation."""
    if isinstance(prompt, str):
        return prompt
    last_message = getattr(prompt, "last_message", None)
    if callable(last_message):
        message = last_message()
        return getattr(message, "text", None) or ""
    text = getattr(prompt, "text", None)
    if isinstance(text, str):
        return text
    return "" if prompt is None else str(prompt)


def _write_trace(result: PipelineResult) -> None:
    if _TRACE_PATH is None:
        return
    line = json.dumps(result.to_trace(), ensure_ascii=False)
    with _LOCK:
        with _TRACE_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def generate(prompt: Any, **kwargs: Any) -> list[str | None]:
    if os.environ.get(ENV_RECORD) == "1":
        return [""]
    pipeline = _get_pipeline()
    text = _prompt_to_text(prompt)
    try:
        result = pipeline.respond(text)
    except Exception:
        logger.exception(
            "Step 'pipeline.respond' failed for prompt %s; returning None to garak",
            short_id(text),
        )
        return [None]
    try:
        _write_trace(result)
    except OSError:
        logger.exception("Could not write trace record for prompt %s", short_id(text))
    return [result.final_text]