from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)


REPEAT_LIMIT_MARKER = "token repeat limit"
REPEAT_RETRY_PENALTY = 1.3 
TRANSIENT_MARKERS = (
    "unexpected eof",
    "runner process has terminated",
    "runner has unexpectedly stopped",
    "connection reset",
    "forcibly closed",
)
TRANSIENT_RETRIES = 2
TRANSIENT_WAIT_S = 5.0


class OllamaError(RuntimeError):
    """Raised when the Ollama server is unreachable, misconfigured or returns an error."""


@dataclass
class ChatResult:
    """One model response."""

    text: str
    eval_tokens: int
    duration_s: float


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        temperature: float = 0.7,
        num_predict: int = 150,
        num_ctx: int = 4096,
        keep_alive: str = "30m",
        timeout_s: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.num_predict = num_predict
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        self.timeout_s = timeout_s
        self.session = requests.Session()

    @classmethod
    def from_config(cls, target: dict[str, Any]) -> "OllamaClient":
        """Build a client from the ``target`` section of a config."""
        return cls(
            base_url=str(target["base_url"]),
            model=str(target["model"]),
            temperature=float(target.get("temperature", 0.7)),
            num_predict=int(target.get("num_predict", 150)),
            num_ctx=int(target.get("num_ctx", 4096)),
            keep_alive=str(target.get("keep_alive", "30m")),
            timeout_s=float(target.get("request_timeout_s", 120)),
        )

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                response = self.session.post(url, json=payload, timeout=self.timeout_s)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
                logger.warning(
                    "Ollama request to %s failed (attempt %d/2): %s", endpoint, attempt, exc
                )
                time.sleep(2.0)
                continue
            if response.status_code != 200:
                raise OllamaError(
                    f"Ollama {endpoint} returned HTTP {response.status_code}: "
                    f"{response.text[:300]}"
                )
            return response.json()
        raise OllamaError(
            f"Cannot reach Ollama at {self.base_url} ({last_error}). "
            "Start the Ollama app or run 'ollama serve'."
        )

    def check_ready(self) -> None:
        """Verify that the server is up and the configured model has been pulled."""
        try:
            response = self.session.get(f"{self.base_url}/api/tags", timeout=10)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise OllamaError(
                f"Cannot reach Ollama at {self.base_url}: {exc}. "
                "Start the Ollama app or run 'ollama serve'."
            ) from exc
        names: set[str] = set()
        for model in response.json().get("models", []):
            for key in ("name", "model"):
                if model.get(key):
                    names.add(str(model[key]))
        if not names & {self.model, f"{self.model}:latest"}:
            raise OllamaError(
                f"Model '{self.model}' is not available. Run: ollama pull {self.model} "
                f"(installed: {sorted(names) or 'none'})"
            )
        logger.info("Ollama ready at %s with model %s", self.base_url, self.model)

    def warmup(self) -> float:
        """Load the model into memory (empty prompt) and keep it resident."""
        start = time.perf_counter()
        self._post(
            "/api/generate",
            {"model": self.model, "prompt": "", "keep_alive": self.keep_alive, "stream": False},
        )
        duration = time.perf_counter() - start
        logger.info(
            "Model %s loaded and kept warm in %.1fs (keep_alive=%s)",
            self.model,
            duration,
            self.keep_alive,
        )
        return duration

    def _payload(self, messages: list[dict[str, str]], options: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": options,
        }

    @staticmethod
    def _preview(messages: list[dict[str, str]]) -> str:
        user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        return " ".join(user.split())[:80]

    def _post_chat(self, messages: list[dict[str, str]], options: dict[str, Any]) -> dict[str, Any]:
        """POST /api/chat, retrying when the model runner crashed (transient)."""
        for attempt in range(TRANSIENT_RETRIES + 1):
            try:
                return self._post("/api/chat", self._payload(messages, options))
            except OllamaError as exc:
                text = str(exc).lower()
                if attempt >= TRANSIENT_RETRIES or not any(m in text for m in TRANSIENT_MARKERS):
                    raise
                wait = TRANSIENT_WAIT_S * (attempt + 1)
                logger.warning(
                    "Ollama model runner failed for %r (%s); waiting %.0fs and retrying (%d/%d). "
                    "If this repeats, check free GPU memory with nvidia-smi.",
                    self._preview(messages), str(exc)[:120], wait, attempt + 1, TRANSIENT_RETRIES,
                )
                time.sleep(wait)
        raise AssertionError("unreachable")

    def chat(self, messages: list[dict[str, str]], seed: int | None = None) -> ChatResult:
        options: dict[str, Any] = {
            "temperature": self.temperature,
            "num_predict": self.num_predict,
            "num_ctx": self.num_ctx,
        }
        if seed is not None:
            options["seed"] = int(seed)
        start = time.perf_counter()
        try:
            data = self._post_chat(messages, options)
        except OllamaError as exc:
            if REPEAT_LIMIT_MARKER not in str(exc):
                raise
            logger.warning(
                "Ollama aborted a repeating (degenerate) answer for user message %r; "
                "retrying once with repeat_penalty=%.1f",
                self._preview(messages), REPEAT_RETRY_PENALTY,
            )
            retry_options = dict(options, repeat_penalty=REPEAT_RETRY_PENALTY)
            try:
                data = self._post_chat(messages, retry_options)
            except OllamaError as retry_exc:
                if REPEAT_LIMIT_MARKER not in str(retry_exc):
                    raise
                logger.warning(
                    "Retry also hit Ollama's token repeat limit for %r; recording an EMPTY "
                    "answer for this prompt", self._preview(messages),
                )
                data = {"message": {"content": ""}, "eval_count": 0}
        text = (data.get("message") or {}).get("content") or ""
        return ChatResult(
            text=text,
            eval_tokens=int(data.get("eval_count") or 0),
            duration_s=time.perf_counter() - start,
        )