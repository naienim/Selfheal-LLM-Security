from __future__ import annotations

import contextlib
import copy
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterator

from selfheal.benign_eval import is_refusal
from selfheal.cache import DiskCache, make_key
from selfheal.config import resolve_project_path
from selfheal.datasets import Split
from selfheal.garak_scoring import GarakScorer, ScoreRequest
from selfheal.guards import classifiers
from selfheal.ollama_client import ChatResult, OllamaClient
from selfheal.pipeline import DefendedPipeline, prompt_signature

logger = logging.getLogger(__name__)

_NOISY_LOGGERS = ("selfheal.pipeline", "selfheal.guards")
_CLIP = 400


@contextlib.contextmanager
def quiet_pipeline_logs() -> Iterator[None]:
    if logging.getLogger("selfheal").getEffectiveLevel() <= logging.DEBUG:
        yield
        return
    loggers = [logging.getLogger(name) for name in _NOISY_LOGGERS]
    previous = [lg.level for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for lg, level in zip(loggers, previous):
            lg.setLevel(level)


class CachingOllamaClient(OllamaClient):
    """OllamaClient that answers identical requests from the persistent response cache."""

    def __init__(self, cache: DiskCache, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cache = cache
        self.calls = 0

    @classmethod
    def from_target(cls, target: dict[str, Any], cache: DiskCache) -> "CachingOllamaClient":
        return cls(
            cache=cache,
            base_url=str(target["base_url"]),
            model=str(target["model"]),
            temperature=float(target.get("temperature", 0.7)),
            num_predict=int(target.get("num_predict", 150)),
            num_ctx=int(target.get("num_ctx", 4096)),
            keep_alive=str(target.get("keep_alive", "30m")),
            timeout_s=float(target.get("request_timeout_s", 120)),
        )

    def key_for(self, messages: list[dict[str, str]], seed: int | None) -> str:
        return make_key("chat", self.model, messages, seed, self.temperature, self.num_predict,
                        self.num_ctx)

    def chat(self, messages: list[dict[str, str]], seed: int | None = None) -> ChatResult:
        key = self.key_for(messages, seed)
        cached = self.cache.get(key)
        if cached is not None:
            return ChatResult(text=str(cached.get("text", "")),
                              eval_tokens=int(cached.get("tokens", 0)), duration_s=0.0)
        result = super().chat(messages, seed=seed)
        self.calls += 1
        self.cache.put(key, {"text": result.text, "tokens": result.eval_tokens,
                             "s": round(result.duration_s, 3)})
        return result


@dataclass
class EvalResult:
    """Outcome of one config on one split."""

    label: str
    split: str
    signature: str
    categories: dict[str, dict[str, Any]]
    attack_hits: int
    attack_total: int
    asr_macro: float
    benign_fp: int
    benign_n: int
    fpr: float
    guard_fp: int
    model_refusals: int
    blocked_by_rule: dict[str, int]
    fp_by_reason: dict[str, int]
    model_calls: int
    cache_hits: int
    duration_s: float
    items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def asr_micro(self) -> float:
        return self.attack_hits / self.attack_total if self.attack_total else 0.0

    def brief(self) -> str:
        cats = ", ".join(f"{c[:3]} {v['asr'] * 100:.1f}%" for c, v in sorted(self.categories.items()))
        return (
            f"ASR {self.asr_macro * 100:5.1f}% ({self.attack_hits}/{self.attack_total}) [{cats}] | "
            f"FPR {self.fpr * 100:5.1f}% ({self.benign_fp}/{self.benign_n}) | "
            f"model calls {self.model_calls}, cache hits {self.cache_hits} | {self.duration_s:.1f}s"
        )

    def to_dict(self, with_items: bool = False) -> dict[str, Any]:
        data = {
            "label": self.label,
            "split": self.split,
            "signature": self.signature,
            "categories": self.categories,
            "attack_hits": self.attack_hits,
            "attack_total": self.attack_total,
            "asr_macro": self.asr_macro,
            "asr_micro": self.asr_micro,
            "benign_fp": self.benign_fp,
            "benign_n": self.benign_n,
            "fpr": self.fpr,
            "guard_fp": self.guard_fp,
            "model_refusals": self.model_refusals,
            "blocked_by_rule": self.blocked_by_rule,
            "fp_by_reason": self.fp_by_reason,
            "model_calls": self.model_calls,
            "cache_hits": self.cache_hits,
            "duration_s": self.duration_s,
        }
        if with_items:
            data["items"] = self.items
        return data


def _decision_of(result: Any) -> Any:
    if result.blocked_stage == "input":
        return result.input_decision
    if result.blocked_stage == "output":
        return result.output_decision
    return None


class Evaluator:
    """Evaluates configs on fixed splits with garak's detectors and persistent caches."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        settings = cfg.get("evaluator") or {}
        cache_dir = resolve_project_path(settings.get("cache_dir", "results/cache"))
        self.responses = DiskCache(cache_dir / "responses.jsonl", "responses")
        self.classifier_scores = DiskCache(cache_dir / "classifier_scores.jsonl",
                                           "classifier_scores")
        self.detector_scores = DiskCache(cache_dir / "detector_scores.jsonl", "detector_scores")
        classifiers.set_score_cache(self.classifier_scores)
        self.scorer = GarakScorer(mode=str(settings.get("scoring_mode", "inprocess")),
                                  cache=self.detector_scores)
        self.threshold = float(settings.get("eval_threshold", 0.5))
        self.client = CachingOllamaClient.from_target(cfg["target"], self.responses)
        self.client.check_ready()
        self._warmed: set[tuple[str, str]] = set()
        logger.info("Evaluator ready | scoring=%s | eval_threshold=%.2f | cache dir=%s",
                    self.scorer.mode, self.threshold, cache_dir)

    @staticmethod
    def _bare(cfg: dict[str, Any]) -> dict[str, Any]:
        bare = copy.deepcopy(cfg)
        bare["mitigations"]["input_filter"]["enabled"] = False
        bare["mitigations"]["output_filter"]["enabled"] = False
        return bare

    def _prompts(self, split: Split) -> list[str]:
        return [a.prompt for a in split.attacks] + [b.prompt for b in split.benign]

    def is_warm(self, cfg: dict[str, Any], split: Split) -> bool:
        """True if every answer this system prompt needs on ``split`` is already cached."""
        key = (prompt_signature(cfg), split.name)
        if key in self._warmed:
            return True
        with quiet_pipeline_logs():
            pipeline = DefendedPipeline(self._bare(cfg), client=self.client)
        for prompt in self._prompts(split):
            messages = pipeline.build_messages(prompt)
            if self.responses.get(self.client.key_for(messages, pipeline._next_seed(prompt))) is None:
                return False
        self._warmed.add(key)
        return True

    def warm(self, cfg: dict[str, Any], split: Split) -> int:
        """Make sure every prompt of ``split`` has an answer under this system prompt."""
        key = (prompt_signature(cfg), split.name)
        if key in self._warmed:
            return 0
        before = self.client.calls
        start = time.perf_counter()
        prompts = self._prompts(split)
        with quiet_pipeline_logs():
            pipeline = DefendedPipeline(self._bare(cfg), client=self.client)
            for prompt in prompts:
                pipeline.respond(prompt)
        calls = self.client.calls - before
        self._warmed.add(key)
        logger.log(
            logging.INFO if calls else logging.DEBUG,
            "Prompt signature %s warmed on split '%s': %d prompts, %d new model calls, "
            "%d cached (%.1fs)",
            key[0], split.name, len(prompts), calls, len(prompts) - calls,
            time.perf_counter() - start,
        )
        return calls

    def evaluate(self, cfg: dict[str, Any], split: Split, label: str = "",
                 log_summary: bool = True) -> EvalResult:
        """Run the defended pipeline on every item of ``split`` and score it."""
        start = time.perf_counter()
        calls_before = self.client.calls
        self.warm(cfg, split)
        self.responses.reset_stats()
        with quiet_pipeline_logs():
            pipeline = DefendedPipeline(cfg, client=self.client)
            attack_runs = [(item, pipeline.respond(item.prompt)) for item in split.attacks]
            benign_runs = [(item, pipeline.respond(item.prompt)) for item in split.benign]

        requests = [
            ScoreRequest(key=make_key("det", item.id, item.detectors, run.final_text),
                         probe=item.probe, prompt=item.prompt, output=run.final_text,
                         detectors=list(item.detectors), notes=dict(item.notes))
            for item, run in attack_runs
        ]
        scores = self.scorer.score(requests) if requests else {}

        categories: dict[str, dict[str, Any]] = {}
        blocked: Counter[str] = Counter()
        items: list[dict[str, Any]] = []
        hits = 0
        for (item, run), request in zip(attack_runs, requests):
            score = scores[request.key]
            hit = score >= self.threshold
            decision = _decision_of(run)
            rule = decision.rule if decision is not None else None
            if rule:
                blocked[rule] += 1
            bucket = categories.setdefault(item.category, {"hits": 0, "total": 0})
            bucket["total"] += 1
            bucket["hits"] += int(hit)
            hits += int(hit)
            items.append({
                "id": item.id, "kind": "attack", "category": item.category, "probe": item.probe,
                "blocked_stage": run.blocked_stage, "rule": rule,
                "rule_score": decision.score if decision is not None else None,
                "score": score, "hit": hit,
                "raw_text": (run.raw_model_text or "")[:_CLIP], "final_text": run.final_text[:_CLIP],
            })
            logger.debug("[%s] %s | stage=%s rule=%s | detector score=%.2f hit=%s",
                         label, item.id, run.blocked_stage, rule, score, hit)
        for bucket in categories.values():
            bucket["asr"] = bucket["hits"] / bucket["total"] if bucket["total"] else 0.0
        rates = [b["asr"] for b in categories.values() if b["total"]]

        fp_reasons: Counter[str] = Counter()
        benign_fp = guard_fp = refusals = 0
        for item, run in benign_runs:
            decision = _decision_of(run)
            refusal = run.blocked_stage is None and is_refusal(run.final_text)
            false_positive = run.blocked_stage is not None or refusal
            reason = decision.rule if decision is not None else ("model_refusal" if refusal else None)
            if false_positive:
                fp_reasons[str(reason)] += 1
                benign_fp += 1
            guard_fp += int(run.blocked_stage is not None)
            refusals += int(refusal)
            items.append({
                "id": item.id, "kind": "benign", "tag": item.tag,
                "blocked_stage": run.blocked_stage, "rule": decision.rule if decision else None,
                "refusal": refusal, "fp": false_positive, "final_text": run.final_text[:_CLIP],
            })
            logger.debug("[%s] %s | stage=%s | fp=%s reason=%s", label, item.id,
                         run.blocked_stage, false_positive, reason)

        result = EvalResult(
            label=label,
            split=split.name,
            signature=prompt_signature(cfg),
            categories=categories,
            attack_hits=hits,
            attack_total=len(attack_runs),
            asr_macro=sum(rates) / len(rates) if rates else 0.0,
            benign_fp=benign_fp,
            benign_n=len(benign_runs),
            fpr=benign_fp / len(benign_runs) if benign_runs else 0.0,
            guard_fp=guard_fp,
            model_refusals=refusals,
            blocked_by_rule=dict(blocked),
            fp_by_reason=dict(fp_reasons),
            model_calls=self.client.calls - calls_before,
            cache_hits=self.responses.hits,
            duration_s=time.perf_counter() - start,
            items=items,
        )
        if log_summary:
            logger.info("Eval %-24s on %-11s %s", label, split.name, result.brief())
            if blocked:
                logger.info("Eval %-24s attacks blocked by rule: %s", label, dict(blocked))
            if fp_reasons:
                logger.info("Eval %-24s benign false positives by reason: %s", label,
                            dict(fp_reasons))
        return result

    def cache_report(self) -> str:
        parts = []
        for cache in (self.responses, self.classifier_scores, self.detector_scores):
            stats = cache.stats()
            parts.append(f"{cache.name}: {stats['entries']} entries")
        return f"{'; '.join(parts)} | model calls this process: {self.client.calls}"