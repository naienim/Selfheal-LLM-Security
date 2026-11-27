from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

from selfheal.benign_eval import is_refusal
from selfheal.config import ConfigError, load_config, resolve_project_path, validate_config
from selfheal.datasets import load_split
from selfheal.evaluator import Evaluator
from selfheal.logging_setup import get_logger, setup_logging

logger = get_logger("scripts.diagnose_prompt")


def _bare(cfg: dict, hardened: bool, rules: str) -> dict:
    state = copy.deepcopy(cfg)
    mitigations = state["mitigations"]
    mitigations["input_filter"]["enabled"] = False
    mitigations["output_filter"]["enabled"] = False
    mitigations["hardened_system_prompt"]["enabled"] = hardened
    mitigations["hardened_system_prompt"]["spotlight_user_input"] = False
    state["system_prompts"]["learned_rules"] = rules
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Model-level prompt diagnosis (guards off).")
    parser.add_argument("--config", default="configs/healing_search.yaml")
    parser.add_argument("--split", choices=["validation", "improvement"], default="validation")
    parser.add_argument("--learned-rules-file", type=Path)
    parser.add_argument("--show", type=int, default=10)
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        validate_config(cfg)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg["logging"]["level"], resolve_project_path(cfg["logging"]["dir"]),
                  "diagnose_prompt")
    rules = args.learned_rules_file.read_text(encoding="utf-8").strip() if args.learned_rules_file else ""
    evaluator = Evaluator(cfg)
    split = load_split(cfg, args.split)
    base = evaluator.evaluate(_bare(cfg, False, ""), split, "baseline prompt (no guards)")
    hard = evaluator.evaluate(_bare(cfg, True, rules), split, "hardened prompt (no guards)")

    attacks = {a.id: a for a in split.attacks}
    base_items = {i["id"]: i for i in base.items if i["kind"] == "attack"}
    worse, better, artefacts = [], 0, 0
    for item in hard.items:
        if item["kind"] != "attack" or item["hit"] == base_items[item["id"]]["hit"]:
            continue
        if not item["hit"]:
            better += 1
            continue
        attack = attacks[item["id"]]
        answer = item["raw_text"] or ""
        triggers = attack.notes.get("triggers") if isinstance(attack.notes, dict) else None
        flags = []
        if is_refusal(answer):
            flags.append("REFUSAL-BUT-FLAGGED")
        if isinstance(triggers, list) and any(str(t).lower() in answer.lower() for t in triggers):
            flags.append("QUOTES-TRIGGER")
        artefacts += bool(flags)
        worse.append((attack.probe, ",".join(attack.detectors), flags, answer))

    logger.info("Hardened vs baseline prompt at model level on %s: %d attacks newly blocked, "
                "%d attacks newly succeeding (%d of them look like detector artefacts)",
                args.split, better, len(worse), artefacts)
    for probe, detectors, flags, answer in worse[: args.show]:
        logger.info("NEWLY SUCCEEDING | %s | detectors=%s | flags=%s\n    answer: %s",
                    probe, detectors, ",".join(flags) or "-", " ".join(answer.split())[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())