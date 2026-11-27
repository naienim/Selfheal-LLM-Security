from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

_PIPES: dict[tuple[str, int], Any] = {}
_MEMORY: "OrderedDict[str, dict[str, float]]" = OrderedDict()
_MEMORY_LIMIT = 50000
_SCORE_CACHE: Any = None


def set_score_cache(cache: Any) -> None:
    """Use a persistent cache (object with ``get(key)`` and ``put(key, value)``) for scores."""
    global _SCORE_CACHE
    _SCORE_CACHE = cache


def _remember(key: str, scores: dict[str, float]) -> None:
    _MEMORY[key] = scores
    _MEMORY.move_to_end(key)
    if len(_MEMORY) > _MEMORY_LIMIT:
        _MEMORY.popitem(last=False)


class HFTextClassifier:
    """Wrap a Hugging Face ``text-classification`` pipeline (shared per model and device)."""

    def __init__(
        self,
        model_name: str,
        device: str = "cpu",
        function_to_apply: str = "softmax",
    ) -> None:
        self.model_name = model_name
        self.device = str(device)
        self.function_to_apply = function_to_apply
        self._pipe: Any = None

    def _resolve_device(self) -> int:
        if not self.device.lower().startswith("cuda"):
            return -1
        try:
            import torch
        except ImportError:
            logger.warning("torch is not importable; running %s on CPU", self.model_name)
            return -1
        if torch.cuda.is_available():
            return 0
        logger.warning("CUDA requested for %s but not available; using CPU", self.model_name)
        return -1

    def preload(self) -> None:
        """Load the model now (downloads it on first use; reused across instances)."""
        if self._pipe is not None:
            return
        device = self._resolve_device()
        key = (self.model_name, device)
        pipe = _PIPES.get(key)
        if pipe is None:
            from transformers import pipeline

            start = time.perf_counter()
            pipe = pipeline("text-classification", model=self.model_name, device=device)
            _PIPES[key] = pipe
            logger.info(
                "Loaded classifier %s on %s in %.1fs",
                self.model_name,
                "cuda:0" if device == 0 else "cpu",
                time.perf_counter() - start,
            )
        self._pipe = pipe

    def label_scores(self, text: str) -> dict[str, float]:
        """Return ``{label_lowercase: score}`` for every label of the model."""
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        key = f"{self.model_name}|{self.function_to_apply}|{digest}"
        cached = _MEMORY.get(key)
        if cached is not None:
            _MEMORY.move_to_end(key)
            return cached
        if _SCORE_CACHE is not None:
            stored = _SCORE_CACHE.get(key)
            if stored is not None:
                _remember(key, stored)
                return stored
        self.preload()
        raw = self._pipe(
            text,
            truncation=True,
            max_length=512,
            top_k=None,
            function_to_apply=self.function_to_apply,
        )
        if raw and isinstance(raw[0], list):
            raw = raw[0]
        scores = {str(item["label"]).lower(): float(item["score"]) for item in raw}
        _remember(key, scores)
        if _SCORE_CACHE is not None:
            _SCORE_CACHE.put(key, scores)
        return scores