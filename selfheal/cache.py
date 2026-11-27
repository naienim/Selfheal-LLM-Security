from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def make_key(*parts: Any) -> str:
    """Stable SHA-256 key for any JSON-serialisable parts."""
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DiskCache:
    def __init__(self, path: Path, name: str) -> None:
        self.path = Path(path)
        self.name = name
        self.hits = 0
        self.misses = 0
        self._data: dict[str, Any] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        bad = 0
        if self.path.exists():
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                        self._data[record["k"]] = record["v"]
                    except (json.JSONDecodeError, KeyError, TypeError):
                        bad += 1
        logger.info(
            "Cache '%s' loaded: %d entries from %s%s",
            name,
            len(self._data),
            self.path,
            f" ({bad} unreadable lines skipped)" if bad else "",
        )

    def get(self, key: str) -> Any:
        """Return the cached value or None (counts a hit or a miss)."""
        value = self._data.get(key)
        if value is None:
            self.misses += 1
        else:
            self.hits += 1
        return value

    def put(self, key: str, value: Any) -> None:
        """Store a value in memory and append it to the file."""
        self._data[key] = value
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"k": key, "v": value}, ensure_ascii=False) + "\n")

    def stats(self) -> dict[str, int]:
        return {"entries": len(self._data), "hits": self.hits, "misses": self.misses}

    def reset_stats(self) -> None:
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._data)