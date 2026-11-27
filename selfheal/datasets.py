from __future__ import annotations

import copy
import json
import logging
import random
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from selfheal.benign_eval import load_benign_items
from selfheal.cache import make_key
from selfheal.config import ConfigError, project_relative, resolve_project_path, save_yaml
from selfheal.experiment import read_json, write_json
from selfheal.garak_runner import run_category_scan
from selfheal.garak_target import ENV_RECORD
from selfheal.report_parser import parse_report

logger = logging.getLogger(__name__)

SPLITS = ("improvement", "validation", "test")
_WORD = re.compile(r"\w+")


@dataclass
class AttackItem:
    """One attack prompt with everything needed to score it with garak's detectors."""

    id: str
    category: str
    probe: str
    prompt: str
    detectors: list[str]
    notes: dict[str, Any] = field(default_factory=dict)
    group: str = ""


@dataclass
class BenignItem:
    """One benign prompt; tag is 'normal' or 'hard' (hard negative)."""

    id: str
    tag: str
    prompt: str


@dataclass
class Split:
    """The attack and benign items of one split."""

    name: str
    attacks: list[AttackItem]
    benign: list[BenignItem]

    def subset(self, attack_ids: set[str], benign_ids: set[str], name: str | None = None) -> "Split":
        return Split(
            name or f"{self.name}_subset",
            [a for a in self.attacks if a.id in attack_ids],
            [b for b in self.benign if b.id in benign_ids],
        )


def splits_dir(cfg: dict[str, Any]) -> Path:
    return resolve_project_path(cfg["splits"]["dir"])


def _check_split_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    split_cfg = cfg.get("splits")
    if not isinstance(split_cfg, dict):
        raise ConfigError("Missing 'splits' section (see configs/base.yaml)")
    for key in ("attack_fractions", "benign_fractions"):
        fractions = split_cfg.get(key) or {}
        if set(fractions) != set(SPLITS) or abs(sum(fractions.values()) - 1.0) > 1e-6:
            raise ConfigError(f"splits.{key} must define {SPLITS} and sum to 1.0")
    if not split_cfg.get("pool"):
        raise ConfigError("splits.pool must list probes per category")
    return split_cfg


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def record_attack_pool(cfg: dict[str, Any], run_dir: Path) -> list[AttackItem]:
    """Run garak once in record-only mode and collect unique prompts with notes/detectors."""
    split_cfg = _check_split_cfg(cfg)
    record_cfg = copy.deepcopy(cfg)
    record_cfg["garak"]["prompt_cap"] = int(split_cfg.get("prompt_cap", 30))
    record_cfg["garak"]["categories"] = {
        category: {"generations": 1, "probes": list(probes)}
        for category, probes in split_cfg["pool"].items()
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    resolved = save_yaml(record_cfg, run_dir / "resolved_config.yaml")
    log_dir = resolve_project_path(cfg["logging"]["dir"])
    threshold = float(cfg["garak"].get("eval_threshold", 0.5))
    items: list[AttackItem] = []
    for category in record_cfg["garak"]["categories"]:
        scan = run_category_scan(record_cfg, category, resolved, run_dir, log_dir, "record",
                                 extra_env={ENV_RECORD: "1"})
        parsed = parse_report(scan.report_path, threshold)
        seen: set[str] = set()
        missing_detectors: set[str] = set()
        for attempt in parsed.attempts:
            text = attempt.prompt
            if not text.strip() or text in seen:
                continue
            seen.add(text)
            detectors = parsed.probe_detectors.get(attempt.probe, [])
            if not detectors:
                missing_detectors.add(attempt.probe)
                continue
            items.append(
                AttackItem(
                    id=f"{category}:{make_key(category, attempt.probe, text)[:12]}",
                    category=category,
                    probe=attempt.probe,
                    prompt=text,
                    detectors=list(detectors),
                    notes=dict(attempt.notes),
                )
            )
        for probe in sorted(missing_detectors):
            logger.warning("No detector recorded for probe %s; its prompts were skipped", probe)
        logger.info("Recorded %d unique prompts for category %s in %.1fs",
                    len(seen), category, scan.duration_s)
    return items


def _subsample(items: list[AttackItem], max_per_probe: int, rng: random.Random) -> list[AttackItem]:
    by_probe: dict[str, list[AttackItem]] = defaultdict(list)
    for item in sorted(items, key=lambda i: i.id):
        by_probe[item.probe].append(item)
    kept: list[AttackItem] = []
    for probe in sorted(by_probe):
        group = by_probe[probe]
        if len(group) > max_per_probe:
            logger.info("Subsampling %s: %d -> %d prompts", probe, len(group), max_per_probe)
            group = sorted(rng.sample(group, max_per_probe), key=lambda i: i.id)
        kept.extend(group)
    return kept


def _shingles(text: str, size: int = 5) -> set[str]:
    words = _WORD.findall(text.lower())
    if len(words) <= size:
        return {" ".join(words)}
    return {" ".join(words[i:i + size]) for i in range(len(words) - size + 1)}


def assign_groups(items: list[AttackItem], threshold: float) -> dict[str, dict[str, int]]:
    """Union near-duplicate prompts (same probe, shingle Jaccard >= threshold) into groups."""
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    shingles = [_shingles(item.prompt) for item in items]
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items[i].probe != items[j].probe:
                continue
            inter = len(shingles[i] & shingles[j])
            if inter and inter / len(shingles[i] | shingles[j]) >= threshold:
                parent[find(i)] = find(j)
    roots: dict[int, str] = {}
    sizes: dict[str, int] = defaultdict(int)
    for index, item in enumerate(items):
        root = find(index)
        if root not in roots:
            roots[root] = f"{item.probe}#{len(roots)}"
        item.group = roots[root]
        sizes[item.group] += 1
    stats: dict[str, dict[str, int]] = {}
    for item in items:
        entry = stats.setdefault(item.probe, {"prompts": 0, "groups": 0, "largest_group": 0})
        entry["prompts"] += 1
    for group, size in sizes.items():
        probe = group.rsplit("#", 1)[0]
        stats[probe]["groups"] += 1
        stats[probe]["largest_group"] = max(stats[probe]["largest_group"], size)
    for probe, entry in stats.items():
        if entry["largest_group"] > 0.4 * entry["prompts"] and entry["prompts"] >= 5:
            logger.warning(
                "Probe %s: one near-duplicate group holds %d of %d prompts; it will sit in a "
                "single split (lower splits.near_duplicate_jaccard to split it)",
                probe, entry["largest_group"], entry["prompts"],
            )
    return stats


def _allocate(groups: list[list[Any]], fractions: dict[str, float],
              rng: random.Random) -> dict[str, list[Any]]:
    """Assign whole groups to splits, always filling the split furthest below its target."""
    total = sum(len(g) for g in groups)
    targets = {s: fractions[s] * total for s in SPLITS}
    current = {s: 0 for s in SPLITS}
    out: dict[str, list[Any]] = {s: [] for s in SPLITS}
    order = list(groups)
    rng.shuffle(order)
    order.sort(key=len, reverse=True)
    for group in order:
        best = max(SPLITS, key=lambda s: (targets[s] - current[s], fractions[s]))
        out[best].extend(group)
        current[best] += len(group)
    return out


def _stratified_counts(n: int, fractions: dict[str, float]) -> dict[str, int]:
    raw = {s: n * fractions[s] for s in SPLITS}
    counts = {s: int(raw[s]) for s in SPLITS}
    remainder = n - sum(counts.values())
    order = sorted(SPLITS, key=lambda s: (raw[s] - counts[s], fractions[s]), reverse=True)
    for s in order[:remainder]:
        counts[s] += 1
    return counts


def build_splits(cfg: dict[str, Any], results_root: Path, force: bool = False) -> dict[str, Any]:
    """Record, group and split the data once; returns the manifest."""
    split_cfg = _check_split_cfg(cfg)
    out_dir = splits_dir(cfg)
    manifest_path = out_dir / "manifest.json"
    if manifest_path.exists() and not force:
        raise FileExistsError(
            f"Splits already exist in {out_dir}. They are fixed on purpose; "
            "use --force only if you really want new data (old results become incomparable)."
        )
    seed = int(split_cfg.get("seed", 0))
    rng = random.Random(seed)
    run_dir = results_root / "splits" / f"record_{datetime.now():%Y%m%d_%H%M%S}"
    logger.info("Building splits | seed=%d | recorder run dir=%s", seed, project_relative(run_dir))

    items = record_attack_pool(cfg, run_dir)
    items = _subsample(items, int(split_cfg.get("max_prompts_per_probe", 30)), rng)
    group_stats = assign_groups(items, float(split_cfg.get("near_duplicate_jaccard", 0.6)))

    attack_splits: dict[str, list[AttackItem]] = {s: [] for s in SPLITS}
    for category in sorted({item.category for item in items}):
        groups: dict[str, list[AttackItem]] = defaultdict(list)
        for item in items:
            if item.category == category:
                groups[item.group].append(item)
        allocation = _allocate([groups[g] for g in sorted(groups)],
                               split_cfg["attack_fractions"], rng)
        for name in SPLITS:
            attack_splits[name].extend(allocation[name])

    benign_items = [
        BenignItem(id=f"benign:{make_key(tag, prompt)[:12]}", tag=tag, prompt=prompt)
        for tag, prompt in load_benign_items(resolve_project_path(cfg["benign"]["prompts_file"]))
    ]
    benign_splits: dict[str, list[BenignItem]] = {s: [] for s in SPLITS}
    for tag in sorted({b.tag for b in benign_items}):
        tagged = sorted([b for b in benign_items if b.tag == tag], key=lambda b: b.id)
        rng.shuffle(tagged)
        counts = _stratified_counts(len(tagged), split_cfg["benign_fractions"])
        start = 0
        for name in SPLITS:
            benign_splits[name].extend(tagged[start:start + counts[name]])
            start += counts[name]

    for name in SPLITS:
        _write_jsonl(out_dir / f"{name}_attacks.jsonl", [asdict(a) for a in attack_splits[name]])
        _write_jsonl(out_dir / f"{name}_benign.jsonl", [asdict(b) for b in benign_splits[name]])

    manifest = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "seed": seed,
        "recorder_run": project_relative(run_dir),
        "pool": split_cfg["pool"],
        "attack_fractions": split_cfg["attack_fractions"],
        "benign_fractions": split_cfg["benign_fractions"],
        "group_stats": group_stats,
        "attacks": {
            name: {
                "total": len(attack_splits[name]),
                "by_category": {
                    c: sum(1 for a in attack_splits[name] if a.category == c)
                    for c in sorted({a.category for a in items})
                },
                "asr_step_pp": round(100 / max(len(attack_splits[name]), 1), 2),
            }
            for name in SPLITS
        },
        "benign": {
            name: {
                "total": len(benign_splits[name]),
                "by_tag": {t: sum(1 for b in benign_splits[name] if b.tag == t)
                           for t in sorted({b.tag for b in benign_items})},
                "fpr_step_pp": round(100 / max(len(benign_splits[name]), 1), 2),
            }
            for name in SPLITS
        },
    }
    write_json(manifest, manifest_path)
    logger.info("Splits written to %s\n%s", project_relative(out_dir), format_manifest(manifest))
    return manifest


def load_split(cfg: dict[str, Any], name: str) -> Split:
    """Load one fixed split from disk."""
    if name not in SPLITS:
        raise ValueError(f"Unknown split '{name}'; expected one of {SPLITS}")
    directory = splits_dir(cfg)
    attack_path = directory / f"{name}_attacks.jsonl"
    benign_path = directory / f"{name}_benign.jsonl"
    if not attack_path.exists() or not benign_path.exists():
        raise FileNotFoundError(
            f"Split '{name}' not found in {directory}. Run: python -m scripts.build_splits"
        )
    attacks = [AttackItem(**row) for row in _read_jsonl(attack_path)]
    benign = [BenignItem(**row) for row in _read_jsonl(benign_path)]
    logger.debug("Loaded split %s: %d attacks, %d benign", name, len(attacks), len(benign))
    return Split(name, attacks, benign)


def load_manifest(cfg: dict[str, Any]) -> dict[str, Any]:
    path = splits_dir(cfg) / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"No splits manifest at {path}. Run: python -m scripts.build_splits")
    return read_json(path)


def format_manifest(manifest: dict[str, Any]) -> str:
    """Readable summary of the split sizes and measurement resolution."""
    lines = [f"Splits (seed {manifest['seed']}, created {manifest['created']})"]
    for name in SPLITS:
        attacks = manifest["attacks"][name]
        benign = manifest["benign"][name]
        cats = ", ".join(f"{c}={n}" for c, n in attacks["by_category"].items())
        tags = ", ".join(f"{t}={n}" for t, n in benign["by_tag"].items())
        lines.append(
            f"  {name:<12} attacks={attacks['total']:<4} ({cats}) | "
            f"benign={benign['total']:<3} ({tags}) | "
            f"1 attack = {attacks['asr_step_pp']} pp ASR, 1 benign = {benign['fpr_step_pp']} pp FPR"
        )
    groups = manifest.get("group_stats", {})
    if groups:
        lines.append("  near-duplicate groups per probe (prompts / groups / largest):")
        for probe, stats in sorted(groups.items()):
            lines.append(
                f"    {probe:<42} {stats['prompts']:>3} / {stats['groups']:>3} / "
                f"{stats['largest_group']:>3}"
            )
    return "\n".join(lines)