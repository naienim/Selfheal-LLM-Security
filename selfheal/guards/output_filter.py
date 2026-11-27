from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from selfheal.guards.classifiers import HFTextClassifier

logger = logging.getLogger(__name__)

DEFAULT_TOXICITY_MODEL = "unitary/toxic-bert"

POLICY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "policy.jailbreak_tag",
        re.compile(
            r"\[\s*(?:\U0001F513\s*)?(?:JAILBREAK|JAILBROKEN|DAN|STAN|DUDE|"
            r"DEVELOPER MODE(?: OUTPUT)?)\s*\]",
            re.IGNORECASE,
        ),
    ),
    (
        "policy.persona_prefix",
        re.compile(r"(?m)^\s*\**\s*(?:DAN|STAN|DUDE|BetterDAN|AntiDAN)\s*\**\s*:"),
    ),
    (
        "policy.mode_enabled",
        re.compile(
            r"\b(?:DAN|developer|jailbreak|god)\s+mode\s+(?:enabled|activated|on)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "policy.persona_claim",
        re.compile(r"\bI\s+am\s+(?:now\s+)?(?:DAN|STAN|DUDE)\b|\bas\s+(?:DAN|STAN|DUDE)\b"),
    ),
    (
        "policy.unrestricted_claim",
        re.compile(
            r"\bI\s+(?:have\s+been|am\s+now|am)\s+(?:freed|jailbroken|unrestricted|unshackled)\b",
            re.IGNORECASE,
        ),
    ),
]


@dataclass
class OutputDecision:
    """Outcome of the output guard for one model response."""

    replaced: bool
    rule: str | None = None
    score: float | None = None
    detail: str = ""


class OutputFilter:
    """Replace responses that break the persona policy or score as toxic."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.policy_enabled = bool((cfg.get("policy") or {}).get("enabled"))
        toxicity_cfg = cfg.get("toxicity") or {}
        self.toxicity_threshold = float(toxicity_cfg.get("threshold", 0.5))
        self.toxicity: HFTextClassifier | None = None
        if toxicity_cfg.get("enabled"):
            self.toxicity = HFTextClassifier(
                toxicity_cfg.get("model", DEFAULT_TOXICITY_MODEL),
                device=toxicity_cfg.get("device", "cpu"),
                function_to_apply="sigmoid",
            )
            self.toxicity.preload()
        logger.info(
            "Output filter ready | policy keywords=%s | toxicity=%s",
            "on" if self.policy_enabled else "off",
            f"on (threshold={self.toxicity_threshold})" if self.toxicity else "off",
        )

    def check(self, text: str) -> OutputDecision:
        """Return a replacement decision for the model response ``text``."""
        if not text or not text.strip():
            return OutputDecision(replaced=False)
        if self.policy_enabled:
            for name, pattern in POLICY_PATTERNS:
                found = pattern.search(text)
                if found:
                    return OutputDecision(replaced=True, rule=name, detail=found.group(0)[:80])
        if self.toxicity is not None:
            scores = self.toxicity.label_scores(text)
            label, score = max(scores.items(), key=lambda item: item[1])
            if score >= self.toxicity_threshold:
                return OutputDecision(replaced=True, rule=f"toxicity.{label}", score=score)
            return OutputDecision(replaced=False, score=score)
        return OutputDecision(replaced=False)