from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from selfheal.config import PROJECT_ROOT

logger = logging.getLogger(__name__)


class ScoringError(RuntimeError):
    """Raised when garak detectors cannot score an output."""


@dataclass
class ScoreRequest:
    """One output to score with one or more garak detectors (max over detectors)."""

    key: str
    probe: str
    prompt: str
    output: str
    detectors: list[str]
    notes: dict[str, Any] = field(default_factory=dict)


class _InProcessBackend:
    """Loads garak once and keeps detector instances alive."""

    def __init__(self) -> None:
        try:
            from garak import _config, _plugins
            from garak.attempt import Attempt, Message
        except ImportError as exc:
            raise ScoringError(f"garak is not importable: {exc}") from exc
        try:
            _config.load_base_config()
        except Exception as exc:
            raise ScoringError(f"garak base config could not be loaded: {exc}") from exc
        self._plugins = _plugins
        self._attempt_cls = Attempt
        self._message_cls = Message
        self._detectors: dict[str, Any] = {}

    def detector(self, name: str) -> Any:
        if name not in self._detectors:
            start = time.perf_counter()
            try:
                self._detectors[name] = self._plugins.load_plugin(f"detectors.{name}")
            except Exception as exc:
                raise ScoringError(f"could not load garak detector '{name}': {exc}") from exc
            logger.info("Loaded garak detector %s in %.1fs", name, time.perf_counter() - start)
        return self._detectors[name]

    def _attempt(self, request: ScoreRequest) -> Any:
        message = self._message_cls(request.prompt, lang="en")
        try:
            attempt = self._attempt_cls(
                prompt=message, probe_classname=request.probe, notes=dict(request.notes)
            )
        except TypeError:
            attempt = self._attempt_cls(prompt=message)
            attempt.notes = dict(request.notes)
        attempt.outputs = [request.output]
        return attempt

    def score(self, request: ScoreRequest) -> float:
        best = 0.0
        for name in request.detectors:
            detector = self.detector(name)
            try:
                values = detector.detect(self._attempt(request))
            except Exception as exc:
                raise ScoringError(
                    f"detector '{name}' failed on probe '{request.probe}': {exc}"
                ) from exc
            numbers = [float(v) for v in (values or []) if v is not None]
            if numbers:
                best = max(best, max(numbers))
        return best


class GarakScorer:
    """Score many outputs, using an optional persistent cache keyed by ``ScoreRequest.key``."""

    def __init__(self, mode: str = "inprocess", cache: Any = None, timeout_s: float = 1800) -> None:
        if mode not in ("inprocess", "subprocess"):
            raise ValueError(f"evaluator.scoring_mode must be inprocess or subprocess, not {mode}")
        self.mode = mode
        self.cache = cache
        self.timeout_s = timeout_s
        self._backend: _InProcessBackend | None = None

    def score(self, requests: list[ScoreRequest]) -> dict[str, float]:
        """Return ``{request.key: score}``; uncached requests are scored in one batch."""
        results: dict[str, float] = {}
        todo: list[ScoreRequest] = []
        for request in requests:
            cached = self.cache.get(request.key) if self.cache is not None else None
            if cached is not None:
                results[request.key] = float(cached)
            elif request.key not in {r.key for r in todo}:
                todo.append(request)
        if todo:
            start = time.perf_counter()
            fresh = (
                self._score_inprocess(todo)
                if self.mode == "inprocess"
                else self._score_subprocess(todo)
            )
            for key, value in fresh.items():
                results[key] = float(value)
                if self.cache is not None:
                    self.cache.put(key, float(value))
            logger.info(
                "Scored %d new outputs with garak detectors (%s) in %.1fs; %d from cache",
                len(todo),
                self.mode,
                time.perf_counter() - start,
                len(requests) - len(todo),
            )
        return results

    def _score_inprocess(self, requests: list[ScoreRequest]) -> dict[str, float]:
        if self._backend is None:
            self._backend = _InProcessBackend()
        return {request.key: self._backend.score(request) for request in requests}

    def _score_subprocess(self, requests: list[ScoreRequest]) -> dict[str, float]:
        with tempfile.TemporaryDirectory(prefix="selfheal_score_") as tmp:
            in_path = Path(tmp) / "requests.jsonl"
            out_path = Path(tmp) / "scores.json"
            with in_path.open("w", encoding="utf-8") as handle:
                for request in requests:
                    handle.write(json.dumps(asdict(request), ensure_ascii=False) + "\n")
            env = os.environ.copy()
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = str(PROJECT_ROOT) + (os.pathsep + existing if existing else "")
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            cmd = [sys.executable, "-m", "selfheal.garak_scoring", str(in_path), str(out_path)]
            try:
                proc = subprocess.run(cmd, capture_output=True, env=env, cwd=str(PROJECT_ROOT),
                                      timeout=self.timeout_s, check=False)
            except subprocess.TimeoutExpired as exc:
                raise ScoringError(f"scoring worker exceeded {self.timeout_s:.0f}s") from exc
            if proc.returncode != 0 or not out_path.exists():
                output = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
                tail = "\n".join(output.splitlines()[-30:])
                raise ScoringError(f"scoring worker failed (exit {proc.returncode}):\n{tail}")
            return json.loads(out_path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    """Worker entry point: score requests from a JSONL file into a JSON file."""
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 2:
        print("usage: python -m selfheal.garak_scoring <requests.jsonl> <scores.json>",
              file=sys.stderr)
        return 2
    in_path, out_path = Path(args[0]), Path(args[1])
    requests = [
        ScoreRequest(**json.loads(line))
        for line in in_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    backend = _InProcessBackend()
    scores = {request.key: backend.score(request) for request in requests}
    out_path.write_text(json.dumps(scores), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())