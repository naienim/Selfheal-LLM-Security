from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]

logger = logging.getLogger(__name__)

_REQUIRED_KEYS = (
    "target.base_url",
    "target.model",
    "system_prompts.baseline",
    "system_prompts.hardened",
    "system_prompts.spotlight_note",
    "mitigations.hardened_system_prompt",
    "mitigations.input_filter",
    "mitigations.output_filter",
    "mitigations.fallback_messages.input_blocked",
    "mitigations.fallback_messages.output_replaced",
    "garak.categories",
    "benign.prompts_file",
    "logging.dir",
    "paths.results_dir",
)


class ConfigError(Exception):
    """Raised when a configuration file is missing or invalid."""


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"The top level of {path} must be a mapping.")
    return data


def load_config(path: str | Path) -> dict[str, Any]:
    """Load a YAML config, resolving ``extends`` (relative to the file) recursively."""
    path = Path(path)
    if not path.is_absolute():
        candidate = Path.cwd() / path
        path = candidate if candidate.exists() else PROJECT_ROOT / path
    path = path.resolve()
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    data = _read_yaml(path)
    own_name = data.get("name")
    parent = data.pop("extends", None)
    if parent:
        data = deep_merge(load_config((path.parent / parent).resolve()), data)
    data["name"] = own_name or path.stem
    data["_source"] = str(path)
    return data


def get_path(cfg: dict[str, Any], dotted: str, default: Any = None) -> Any:
    """Read a nested value with a dotted key such as ``input_filter.classifier.threshold``."""
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_path(cfg: dict[str, Any], dotted: str, value: Any) -> None:
    """Set a nested value with a dotted key, creating intermediate dicts."""
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts into ``{"a.b.c": value}``."""
    flat: dict[str, Any] = {}
    for key, value in data.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            flat.update(flatten(value, dotted))
        else:
            flat[dotted] = value
    return flat


def validate_config(cfg: dict[str, Any]) -> None:
    """Fail early with a clear message if required settings are missing or malformed."""
    missing = [key for key in _REQUIRED_KEYS if get_path(cfg, key) is None]
    if missing:
        raise ConfigError(f"Config '{cfg.get('name')}' is missing keys: {', '.join(missing)}")
    categories = cfg["garak"]["categories"]
    if not isinstance(categories, dict) or not categories:
        raise ConfigError("garak.categories must be a non-empty mapping")
    for name, category in categories.items():
        probes = category.get("probes") if isinstance(category, dict) else None
        if not isinstance(probes, list) or not probes:
            raise ConfigError(f"garak.categories.{name}.probes must be a non-empty list")
        generations = category.get("generations", 1)
        if not isinstance(generations, int) or generations < 1:
            raise ConfigError(f"garak.categories.{name}.generations must be an integer >= 1")
    healing = cfg.get("healing")
    if healing is not None:
        target = healing.get("target_asr")
        if not isinstance(target, (int, float)) or not 0 <= target <= 1:
            raise ConfigError("healing.target_asr must be a number between 0 and 1")
        if not isinstance(healing.get("max_iterations"), int) or healing["max_iterations"] < 1:
            raise ConfigError("healing.max_iterations must be an integer >= 1")
        unknown = set(healing.get("playbook", {})) - set(categories)
        if unknown:
            raise ConfigError(f"healing.playbook has unknown categories: {sorted(unknown)}")


def describe_mitigations(mitigations: dict[str, Any]) -> list[str]:
    """Human-readable list of the mitigations that are switched on."""
    enabled: list[str] = []
    hardened = mitigations.get("hardened_system_prompt") or {}
    if hardened.get("enabled"):
        suffix = "+spotlight" if hardened.get("spotlight_user_input") else ""
        enabled.append(f"hardened_system_prompt{suffix}")
    input_filter = mitigations.get("input_filter") or {}
    if input_filter.get("enabled"):
        rules = [name for name, on in (input_filter.get("regex") or {}).items() if on]
        if rules:
            enabled.append(f"input_regex[{','.join(rules)}]")
        classifier = input_filter.get("classifier") or {}
        if classifier.get("enabled"):
            enabled.append(f"input_classifier(thr={classifier.get('threshold')})")
    output_filter = mitigations.get("output_filter") or {}
    if output_filter.get("enabled"):
        if (output_filter.get("policy") or {}).get("enabled"):
            enabled.append("output_policy_keywords")
        toxicity = output_filter.get("toxicity") or {}
        if toxicity.get("enabled"):
            enabled.append(f"output_toxicity(thr={toxicity.get('threshold')})")
    return enabled or ["none"]


def resolve_project_path(path: str | Path) -> Path:
    """Resolve a path relative to the project root (absolute paths are kept)."""
    path = Path(path)
    return path if path.is_absolute() else PROJECT_ROOT / path


def project_relative(path: str | Path) -> str:
    """Return a POSIX-style path relative to the project root when possible."""
    try:
        return Path(path).resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return Path(path).as_posix()


def save_yaml(data: dict[str, Any], path: Path, header: str | None = None) -> Path:
    """Write ``data`` as UTF-8 YAML (safe_dump quotes Windows paths correctly)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        if header:
            handle.write(header)
        yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)
    return path