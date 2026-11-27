from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass
from typing import Any

from selfheal.config import get_path, set_path


@dataclass(frozen=True)
class Move:
    """One change to the mitigations."""

    key: str
    value: Any
    kind: str
    label: str


def short_name(key: str) -> str:
    """'input_filter.classifier.min_words' -> 'classifier.min_words'."""
    parts = key.split(".")
    return ".".join(parts[-2:])


def normalize(mitigations: dict[str, Any]) -> None:
    """Derive the two layer switches from their parts (in place)."""
    input_filter = mitigations["input_filter"]
    regex = input_filter.get("regex") or {}
    input_filter["enabled"] = bool(any(regex.values())
                                   or (input_filter.get("classifier") or {}).get("enabled"))
    output_filter = mitigations["output_filter"]
    output_filter["enabled"] = bool((output_filter.get("policy") or {}).get("enabled")
                                    or (output_filter.get("toxicity") or {}).get("enabled"))


def is_valid(mitigations: dict[str, Any]) -> bool:
    """Spotlighting is a sub-option of the hardened prompt."""
    hardened = mitigations["hardened_system_prompt"]
    return not (hardened.get("spotlight_user_input") and not hardened.get("enabled"))


def single_moves(mitigations: dict[str, Any], search_cfg: dict[str, Any],
                 steps: dict[str, float], thresholds_only: bool = False) -> list[Move]:
    """All single moves from the current mitigations: toggles first, then numeric steps."""
    moves: list[Move] = []
    if not thresholds_only:
        for key in search_cfg["toggles"]:
            current = bool(get_path(mitigations, key))
            moves.append(Move(key, not current, "toggle", f"{key}={'off' if current else 'on'}"))
    for key, spec in search_cfg["thresholds"].items():
        required = spec.get("requires")
        if required and not get_path(mitigations, required):
            continue
        step = float(steps[key])
        if step < float(spec["min_step"]) - 1e-12:
            continue
        current = float(get_path(mitigations, key) or 0)
        for sign in (1, -1):
            new = round(min(float(spec["max"]), max(float(spec["min"]), current + sign * step)), 4)
            if abs(new - current) > 1e-9:
                moves.append(Move(key, new, "threshold",
                                  f"{short_name(key)} {current:g}->{new:g}"))
    return moves


def apply_moves(state: dict[str, Any], moves: tuple[Move, ...]) -> dict[str, Any] | None:
    """New config with the moves applied, or None if the result is invalid."""
    keys = [m.key for m in moves]
    if len(set(keys)) != len(keys):
        return None
    candidate = copy.deepcopy(state)
    for move in moves:
        set_path(candidate["mitigations"], move.key, move.value)
    normalize(candidate["mitigations"])
    return candidate if is_valid(candidate["mitigations"]) else None


def combinations(singles: list[Move], max_size: int) -> list[tuple[Move, ...]]:
    """Combinations of 2..max_size moves that touch different settings."""
    combos: list[tuple[Move, ...]] = []
    for size in range(2, max_size + 1):
        for combo in itertools.combinations(singles, size):
            if len({m.key for m in combo}) == size:
                combos.append(combo)
    return combos


def describe(moves: tuple[Move, ...]) -> str:
    return " + ".join(m.label for m in moves)