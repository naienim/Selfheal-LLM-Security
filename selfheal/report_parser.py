from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _strip_prefix(name: str, prefixes: tuple[str, ...]) -> str:
    for prefix in prefixes:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def text_of(obj: Any) -> str | None:
    """Extract plain text from the prompt/output shapes used by different garak versions."""
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        turns = obj.get("turns")
        if isinstance(turns, list) and turns:
            for turn in reversed(turns):
                if isinstance(turn, dict) and turn.get("role", "user") == "user":
                    return text_of(turn.get("content"))
            last = turns[-1]
            return text_of(last.get("content") if isinstance(last, dict) else last)
        if "text" in obj:
            value = obj.get("text")
            return value if isinstance(value, str) or value is None else str(value)
        if "content" in obj:
            return text_of(obj["content"])
    if isinstance(obj, list) and obj:
        return text_of(obj[-1])
    return str(obj)


@dataclass
class ProbeResult:
    """Aggregated eval result for one probe (worst detector)."""

    probe: str
    detector: str
    passed: int
    total: int

    @property
    def fails(self) -> int:
        return max(self.total - self.passed, 0)

    @property
    def asr(self) -> float:
        return self.fails / self.total if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "detector": self.detector,
            "passed": self.passed,
            "fails": self.fails,
            "total": self.total,
            "asr": self.asr,
        }


@dataclass
class AttemptRecord:
    """One evaluated garak attempt: prompt, outputs, per-output max score and probe notes."""

    probe: str
    prompt: str
    outputs: list[str | None]
    scores: list[float]
    notes: dict[str, Any] = field(default_factory=dict)

    def hit(self, threshold: float) -> bool:
        return any(score >= threshold for score in self.scores)

    def first_hit_output(self, threshold: float) -> str | None:
        for output, score in zip(self.outputs, self.scores):
            if score >= threshold:
                return output
        return None


@dataclass
class ParsedReport:
    """Contents of one report.jsonl."""

    path: Path
    probe_results: dict[str, ProbeResult] = field(default_factory=dict)
    attempts: list[AttemptRecord] = field(default_factory=list)
    garak_version: str | None = None
    probe_detectors: dict[str, list[str]] = field(default_factory=dict)


def _eval_names(entry: dict[str, Any]) -> tuple[str, str]:
    probe = _strip_prefix(str(entry.get("probe", "")), ("probes.",))
    detector = _strip_prefix(str(entry.get("detector", "")), ("detectors.", "detector."))
    return probe, detector


def _parse_eval(entry: dict[str, Any]) -> ProbeResult | None:
    probe, detector = _eval_names(entry)
    total = entry.get("total_evaluated", entry.get("total"))
    if not probe or total is None:
        return None
    total = int(total)
    if total <= 0:
        return None
    return ProbeResult(probe, detector, int(entry.get("passed", 0)), total)


def _parse_attempt(entry: dict[str, Any]) -> AttemptRecord | None:
    probe = _strip_prefix(str(entry.get("probe_classname", "")), ("probes.",))
    prompt = text_of(entry.get("prompt")) or ""
    outputs = [text_of(o) for o in (entry.get("outputs") or [])]
    detector_results = entry.get("detector_results") or {}
    count = max([len(outputs)] + [len(v or []) for v in detector_results.values()])
    scores = [0.0] * count
    for values in detector_results.values():
        for index, value in enumerate(values or []):
            if value is not None and index < count:
                scores[index] = max(scores[index], float(value))
    outputs += [None] * (count - len(outputs))
    notes = entry.get("notes")
    if not probe:
        return None
    return AttemptRecord(
        probe=probe,
        prompt=prompt,
        outputs=outputs,
        scores=scores,
        notes=notes if isinstance(notes, dict) else {},
    )


def _results_from_attempts(
    attempts: list[AttemptRecord], threshold: float
) -> dict[str, ProbeResult]:
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for attempt in attempts:
        for score in attempt.scores:
            counts[attempt.probe][1] += 1
            if score < threshold:
                counts[attempt.probe][0] += 1
    return {
        probe: ProbeResult(probe, "max_over_detectors", passed, total)
        for probe, (passed, total) in counts.items()
    }


def parse_report(path: Path, eval_threshold: float = 0.5) -> ParsedReport:
    """Parse a garak report; uses eval entries, falling back to attempts if none exist."""
    evals: dict[str, list[ProbeResult]] = defaultdict(list)
    probe_detectors: dict[str, list[str]] = defaultdict(list)
    attempts: list[AttemptRecord] = []
    version: str | None = None
    bad_lines = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            entry_type = entry.get("entry_type")
            if entry_type in ("start_run setup", "init"):
                version = version or entry.get("garak_version") or entry.get("_config.version")
            elif entry_type == "eval":
                probe, detector = _eval_names(entry)
                if probe and detector and detector not in probe_detectors[probe]:
                    probe_detectors[probe].append(detector)
                result = _parse_eval(entry)
                if result:
                    evals[result.probe].append(result)
            elif entry_type == "attempt" and entry.get("status") == 2:
                record = _parse_attempt(entry)
                if record:
                    attempts.append(record)
    if bad_lines:
        logger.warning("Skipped %d malformed lines in %s", bad_lines, path)
    probe_results = {probe: max(items, key=lambda r: r.asr) for probe, items in evals.items()}
    if not probe_results and attempts:
        logger.warning("No eval entries in %s (partial run?); computing from attempts", path)
        probe_results = _results_from_attempts(attempts, eval_threshold)
    return ParsedReport(
        path,
        probe_results,
        attempts,
        str(version) if version else None,
        dict(probe_detectors),
    )


def summarize_category(parsed: ParsedReport) -> dict[str, Any]:
    """ASR for a category = failed outputs / evaluated outputs over its probes."""
    fails = sum(r.fails for r in parsed.probe_results.values())
    total = sum(r.total for r in parsed.probe_results.values())
    return {
        "asr": fails / total if total else 0.0,
        "fails": fails,
        "total": total,
        "attempts": len(parsed.attempts),
        "garak_version": parsed.garak_version,
        "probes": {p: r.to_dict() for p, r in sorted(parsed.probe_results.items())},
    }


def load_trace(path: Path) -> dict[str, dict[str, Any]]:
    """Map prompt text -> first pipeline trace record (from trace.jsonl)."""
    records: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            records.setdefault(record.get("prompt", ""), record)
    return records