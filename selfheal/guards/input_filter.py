from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from selfheal.guards.classifiers import HFTextClassifier

logger = logging.getLogger(__name__)

DEFAULT_PI_MODEL = "protectai/deberta-v3-base-prompt-injection-v2"

Matcher = Callable[[str], Optional[str]]


@dataclass
class InputDecision:
    """Outcome of the input guard for one prompt."""

    blocked: bool
    rule: str | None = None
    score: float | None = None
    detail: str = ""


def _regex_rule(name: str, pattern: str, flags: int = re.IGNORECASE) -> tuple[str, Matcher]:
    compiled = re.compile(pattern, flags)

    def match(text: str) -> str | None:
        found = compiled.search(text)
        return found.group(0) if found else None

    return name, match


_BASE64_BLOB = re.compile(r"(?:[A-Za-z0-9+/]{4}){6,}(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")
_HEX_BLOB = re.compile(r"(?:[0-9A-Fa-f]{2}[\s:]?){16,}")
_DECODE_HINT = re.compile(
    r"\b(?:decode[ds]?|decoding|decipher|decrypt|encoded|base64|base-64|hexadecimal|rot13)\b",
    re.IGNORECASE,
)


def _encoded_payload(text: str) -> str | None:
    """Flag text that combines a long encoded blob with an instruction to decode it."""
    blob = _BASE64_BLOB.search(text) or _HEX_BLOB.search(text)
    if blob and _DECODE_HINT.search(text):
        return blob.group(0)[:60]
    return None


RULE_SETS: dict[str, list[tuple[str, Matcher]]] = {
    "injection": [
        _regex_rule(
            "injection.ignore_previous",
            r"\b(?:ignore|disregard|forget|override|bypass|skip)\b[^.\n]{0,40}?"
            r"\b(?:previous|prior|above|earlier|preceding|following|all|any|your|the|these|those)"
            r"\b[^.\n]{0,25}?\b(?:instructions?|prompts?|rules|directions|directives|"
            r"guidelines|commands?)\b",
        ),
        _regex_rule("injection.stop_everything", r"\b(?:stop|halt)\s+everything\b"),
        _regex_rule("injection.new_instructions", r"\bnew\s+(?:instructions?|task|directives?)\s*:"),
        _regex_rule(
            "injection.prompt_leak",
            r"\b(?:reveal|print|show|repeat|output|leak|tell\s+me)\b[^.\n]{0,30}?"
            r"\b(?:system\s+prompt|initial\s+instructions|hidden\s+instructions|"
            r"your\s+instructions)\b",
        ),
    ],
    "jailbreak": [
        _regex_rule(
            "jailbreak.persona_name",
            r"\b(?:DAN|STAN|DUDE|UCAR|BetterDAN|AntiDAN)\b",
            flags=0,
        ),
        _regex_rule("jailbreak.do_anything_now", r"\bdo\s+anything\s+now\b"),
        _regex_rule(
            "jailbreak.special_mode",
            r"\b(?:developer|dev|god|jailbreak|jailbroken|unrestricted|unfiltered|unlocked|evil)"
            r"\s+mode\b",
        ),
        _regex_rule(
            "jailbreak.no_restrictions",
            r"\b(?:no|without(?:\s+any)?|free\s+(?:from|of)|not\s+bound\s+by|break\s+free\s+of|"
            r"ignores?\s+all)\b[^.\n]{0,25}?\b(?:restrictions?|filters?|filtering|censorship|"
            r"guidelines|limitations|limits|ethics|ethical|morals?|content\s+polic(?:y|ies))\b",
        ),
        _regex_rule("jailbreak.stay_in_character", r"\b(?:stay|remain|keep)\s+in\s+character\b"),
        _regex_rule(
            "jailbreak.policy_evasion",
            r"\b(?:openai|anthropic|content)(?:'s)?\s+(?:content\s+)?polic(?:y|ies)\b",
        ),
    ],
    "encoding": [("encoding.decode_payload", _encoded_payload)],
}


class InputFilter:
    """Block prompts that match enabled rule sets or score high on the PI classifier."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        regex_cfg = cfg.get("regex") or {}
        self.rules: list[tuple[str, Matcher]] = []
        self.active_rule_sets: list[str] = []
        for set_name, rules in RULE_SETS.items():
            if regex_cfg.get(set_name):
                self.rules.extend(rules)
                self.active_rule_sets.append(set_name)

        classifier_cfg = cfg.get("classifier") or {}
        self.threshold = float(classifier_cfg.get("threshold", 0.9))
        self.min_words = int(round(float(classifier_cfg.get("min_words", 0) or 0)))
        self.classifier: HFTextClassifier | None = None
        if classifier_cfg.get("enabled"):
            self.classifier = HFTextClassifier(
                classifier_cfg.get("model", DEFAULT_PI_MODEL),
                device=classifier_cfg.get("device", "cpu"),
                function_to_apply="softmax",
            )
            self.classifier.preload()
        logger.info(
            "Input filter ready | rule sets=%s (%d rules) | classifier=%s",
            self.active_rule_sets or "none",
            len(self.rules),
            f"on (threshold={self.threshold}, min_words={self.min_words})"
            if self.classifier else "off",
        )

    def check(self, text: str) -> InputDecision:
        """Return a blocking decision for ``text`` (regex first, then the gated classifier)."""
        for name, matcher in self.rules:
            snippet = matcher(text)
            if snippet:
                return InputDecision(blocked=True, rule=name, detail=snippet[:80])
        if self.classifier is not None and text.strip():
            if self.min_words and len(text.split()) < self.min_words:
                return InputDecision(blocked=False, detail="classifier skipped (short prompt)")
            score = self.classifier.label_scores(text).get("injection", 0.0)
            if score >= self.threshold:
                return InputDecision(blocked=True, rule="classifier.prompt_injection", score=score)
            return InputDecision(blocked=False, score=score)
        return InputDecision(blocked=False)