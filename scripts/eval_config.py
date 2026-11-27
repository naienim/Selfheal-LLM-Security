from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

import yaml

from selfheal.acceptance import decide, objective
from selfheal.config import ConfigError, load_config, resolve_project_path, set_path, validate_config
from selfheal.datasets import load_split
from selfheal.evaluator import Evaluator
from selfheal.garak_scoring import ScoringError
from selfheal.logging_setup import get_logger, setup_logging
from selfheal.moves import is_valid, normalize
from selfheal.ollama_client import OllamaError
from selfheal.pipeline import prompt_signature
from selfheal.search_healer import validate_search_cfg

logger = get_logger("scripts.eval_config")


def _apply_sets(state: dict, assignments: list[str]) -> None:
    for assignment in assignments:
        if "=" not in assignment:
            raise ConfigError(f"--set expects key=value, got {assignment!r}")
        key, raw = assignment.split("=", 1)
        value = yaml.safe_load(raw)
        key = key.strip()
        if key.startswith("system_prompts."):
            set_path(state, key, value)
        else:
            set_path(state["mitigations"], key.removeprefix("mitigations."), value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate a config / candidate on a split.")
    parser.add_argument("--config", default="configs/healing_search.yaml")
    parser.add_argument("--split", choices=["validation", "improvement"], default="validation")
    parser.add_argument("--start-config", help="take mitigations and learned rules from this YAML")
    parser.add_argument("--set", action="append", default=[], dest="sets",
                        help="candidate change, e.g. input_filter.classifier.threshold=0.8")
    parser.add_argument("--learned-rules-file", type=Path,
                        help="candidate learned rules (text file with '- ' bullets)")
    parser.add_argument("--log-level", help="override logging.level, e.g. DEBUG")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.log_level:
            cfg["logging"]["level"] = args.log_level.upper()
        validate_config(cfg)
        validate_search_cfg(cfg)
        current = copy.deepcopy(cfg)
        if args.start_config:
            start = load_config(args.start_config)
            current["mitigations"] = copy.deepcopy(start["mitigations"])
            current["system_prompts"]["learned_rules"] = str(
                (start.get("system_prompts") or {}).get("learned_rules") or "")
        normalize(current["mitigations"])
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg["logging"]["level"], resolve_project_path(cfg["logging"]["dir"]),
                  "eval_config")
    try:
        evaluator = Evaluator(cfg)
        split = load_split(cfg, args.split)
        result = evaluator.evaluate(current, split, "current")
        logger.info("Current objective: %.4f | %s", objective(result, cfg["search"]),
                    evaluator.cache_report())
        if not args.sets and not args.learned_rules_file:
            return 0
        candidate = copy.deepcopy(current)
        _apply_sets(candidate, args.sets)
        if args.learned_rules_file:
            candidate["system_prompts"]["learned_rules"] = args.learned_rules_file.read_text(
                encoding="utf-8").strip()
        normalize(candidate["mitigations"])
        if not is_valid(candidate["mitigations"]):
            logger.error("Candidate is invalid (spotlighting needs the hardened prompt)")
            return 2
        prompt_change = prompt_signature(candidate) != prompt_signature(current)
        logger.info("Candidate changes the system prompt: %s%s", prompt_change,
                    " (expect model calls the first time)" if prompt_change else
                    " (guard-only: should need 0 model calls)")
        cand_result = evaluator.evaluate(candidate, split, "candidate")
        decision = decide(result, cand_result, cfg["search"], prompt_change=prompt_change)
        logger.info("DECISION: %s | %s | ASR %s (%+d attacks), FPR %s (%+d) | objective %.4f -> "
                    "%.4f", "KEEP" if decision.accepted else "ROLL BACK", decision.reason,
                    decision.asr_change, decision.d_hits, decision.fpr_change, decision.d_fp,
                    decision.objective_current, decision.objective_candidate)
        logger.info("%s", evaluator.cache_report())
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    except (OllamaError, ScoringError):
        logger.exception("Step 'evaluation' failed")
        return 1
    except Exception:
        logger.exception("eval_config failed with an unexpected error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())