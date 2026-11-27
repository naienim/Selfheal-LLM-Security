from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

from selfheal.guards.input_filter import InputDecision, InputFilter
from selfheal.guards.output_filter import OutputDecision, OutputFilter
from selfheal.ollama_client import OllamaClient

logger = logging.getLogger(__name__)

SPOTLIGHT_OPEN = "<user_input>"
SPOTLIGHT_CLOSE = "</user_input>"
LEARNED_RULES_HEADER = (
    "Additional rules learned from past mistakes (they never override the security rules above):"
)


def short_id(text: str) -> str:
    """Stable short id used to trace one prompt through every log line."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:10]


def build_system_prompt(cfg: dict[str, Any]) -> str:
    """Baseline prompt, or hardened core rules + learned rules (+ spotlighting note)."""
    hardened = cfg["mitigations"]["hardened_system_prompt"]
    prompts = cfg["system_prompts"]
    if not hardened.get("enabled"):
        return str(prompts["baseline"]).strip()
    text = str(prompts["hardened"]).strip()
    learned = str(prompts.get("learned_rules") or "").strip()
    if learned:
        text += "\n\n" + LEARNED_RULES_HEADER + "\n" + learned
    if hardened.get("spotlight_user_input"):
        text += "\n\n" + str(prompts["spotlight_note"]).strip()
    return text


def prompt_signature(cfg: dict[str, Any]) -> str:
    """Short hash of everything that changes what the model sees besides the user prompt."""
    hardened = cfg["mitigations"]["hardened_system_prompt"]
    spotlight = bool(hardened.get("enabled") and hardened.get("spotlight_user_input"))
    raw = f"{build_system_prompt(cfg)}\n<spotlight={spotlight}>"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class PipelineResult:
    """Everything that happened to one prompt."""

    prompt: str
    final_text: str
    raw_model_text: str | None
    blocked_stage: str | None
    input_decision: InputDecision | None = None
    output_decision: OutputDecision | None = None
    latency_s: float = 0.0
    model_latency_s: float = 0.0

    def to_trace(self) -> dict[str, Any]:
        """Serialisable record for trace.jsonl."""
        return {
            "prompt_id": short_id(self.prompt),
            "prompt": self.prompt,
            "blocked_stage": self.blocked_stage,
            "input_decision": asdict(self.input_decision) if self.input_decision else None,
            "raw_model_text": self.raw_model_text,
            "output_decision": asdict(self.output_decision) if self.output_decision else None,
            "final_text": self.final_text,
            "latency_s": round(self.latency_s, 3),
            "model_latency_s": round(self.model_latency_s, 3),
        }


def _fmt_score(score: float | None) -> str:
    return "n/a" if score is None else f"{score:.3f}"


class DefendedPipeline:
    """Layered defence around one Ollama model, configured by the ``mitigations`` block."""

    def __init__(self, cfg: dict[str, Any], client: OllamaClient | None = None) -> None:
        mitigations = cfg["mitigations"]
        self.client = client or OllamaClient.from_config(cfg["target"])
        input_cfg = mitigations["input_filter"]
        output_cfg = mitigations["output_filter"]
        self.input_filter = InputFilter(input_cfg) if input_cfg.get("enabled") else None
        self.output_filter = OutputFilter(output_cfg) if output_cfg.get("enabled") else None
        hardened = mitigations["hardened_system_prompt"]
        self.spotlight = bool(hardened.get("enabled") and hardened.get("spotlight_user_input"))
        self.system_prompt = build_system_prompt(cfg)
        fallback = mitigations["fallback_messages"]
        self.fallback_input = str(fallback["input_blocked"])
        self.fallback_output = str(fallback["output_replaced"])
        self.base_seed = int(cfg.get("seed", 42))
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()
        logger.info(
            "Pipeline ready | model=%s | system_prompt=%s | spotlight=%s | input_filter=%s"
            " | output_filter=%s",
            self.client.model,
            "hardened" if hardened.get("enabled") else "baseline",
            self.spotlight,
            "on" if self.input_filter else "off",
            "on" if self.output_filter else "off",
        )

    def _next_seed(self, prompt: str) -> int:
        """Deterministic per-call seed: prompt hash + generation index (reproducible runs)."""
        key = short_id(prompt)
        with self._lock:
            index = self._counts.get(key, 0)
            self._counts[key] = index + 1
        return (self.base_seed + int(key, 16) + index) % (2**31 - 1)

    def build_messages(self, prompt: str) -> list[dict[str, str]]:
        """Chat messages sent to the model (spotlighting wraps the user text)."""
        user_text = prompt
        if self.spotlight:
            safe = prompt.replace(SPOTLIGHT_CLOSE, "</ user_input>")
            user_text = f"{SPOTLIGHT_OPEN}\n{safe}\n{SPOTLIGHT_CLOSE}"
        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_text},
        ]

    def respond(self, prompt: str) -> PipelineResult:
        """Run one prompt through every enabled stage and return the full record."""
        start = time.perf_counter()
        pid = short_id(prompt)
        logger.debug("[%s] stage=input prompt=%r", pid, prompt)

        input_decision: InputDecision | None = None
        if self.input_filter is not None:
            input_decision = self.input_filter.check(prompt)
            logger.debug("[%s] stage=input_filter decision=%s", pid, input_decision)
            if input_decision.blocked:
                logger.info(
                    "[%s] INPUT BLOCKED | rule=%s | score=%s | match=%r",
                    pid,
                    input_decision.rule,
                    _fmt_score(input_decision.score),
                    input_decision.detail,
                )
                logger.debug("[%s] stage=final text=%r", pid, self.fallback_input)
                return PipelineResult(
                    prompt=prompt,
                    final_text=self.fallback_input,
                    raw_model_text=None,
                    blocked_stage="input",
                    input_decision=input_decision,
                    latency_s=time.perf_counter() - start,
                )

        messages = self.build_messages(prompt)
        logger.debug("[%s] stage=model_request messages=%r", pid, messages)
        chat = self.client.chat(messages, seed=self._next_seed(prompt))
        logger.debug(
            "[%s] stage=model_response (%.2fs, %d tokens) text=%r",
            pid,
            chat.duration_s,
            chat.eval_tokens,
            chat.text,
        )

        final_text = chat.text
        blocked_stage: str | None = None
        output_decision: OutputDecision | None = None
        if self.output_filter is not None:
            output_decision = self.output_filter.check(chat.text)
            logger.debug("[%s] stage=output_filter decision=%s", pid, output_decision)
            if output_decision.replaced:
                logger.info(
                    "[%s] OUTPUT REPLACED | rule=%s | score=%s | match=%r",
                    pid,
                    output_decision.rule,
                    _fmt_score(output_decision.score),
                    output_decision.detail,
                )
                final_text = self.fallback_output
                blocked_stage = "output"

        logger.debug("[%s] stage=final text=%r", pid, final_text)
        return PipelineResult(
            prompt=prompt,
            final_text=final_text,
            raw_model_text=chat.text,
            blocked_stage=blocked_stage,
            input_decision=input_decision,
            output_decision=output_decision,
            latency_s=time.perf_counter() - start,
            model_latency_s=chat.duration_s,
        )