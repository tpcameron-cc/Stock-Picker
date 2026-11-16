"""Load config/settings.yaml and resolve env-var-backed secrets."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS = PROJECT_ROOT / "config" / "settings.yaml"

_MISSING = object()


class Settings:
    """Thin read-only wrapper around the settings dict with dotted-path access."""

    def __init__(self, data: dict[str, Any], path: Path | None = None):
        self._data = data
        self.path = path

    def get(self, dotted: str, default: Any = _MISSING) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif default is not _MISSING:
                return default
            else:
                raise KeyError(f"setting '{dotted}' not found in {self.path or 'settings'}")
        return node

    def section(self, dotted: str) -> dict[str, Any]:
        value = self.get(dotted)
        if not isinstance(value, dict):
            raise TypeError(f"setting '{dotted}' is not a section")
        return value

    def env(self, dotted_env_field: str) -> str | None:
        """Resolve a `*_env` field to the env var's value (None if unset)."""
        var_name = self.get(dotted_env_field, None)
        return os.environ.get(var_name) if var_name else None

    def path_for(self, dotted: str) -> Path:
        p = Path(self.get(dotted))
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def raw(self) -> dict[str, Any]:
        return self._data


LOCAL_SETTINGS = PROJECT_ROOT / "config" / "local.yaml"   # gitignored personal overrides


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_settings(path: str | Path | None = None, local_path: str | Path | None = None) -> Settings:
    """Load settings.yaml, then overlay config/local.yaml if it exists (only for the default settings file)."""
    p = Path(path) if path else DEFAULT_SETTINGS
    with open(p, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    lp = Path(local_path) if local_path else (LOCAL_SETTINGS if path is None else None)
    if lp is not None and lp.exists():
        with open(lp, encoding="utf-8") as fh:
            data = _deep_merge(data, yaml.safe_load(fh) or {})
    return Settings(data, p)


def set_local(dotted: str, value: Any, local_path: str | Path | None = None) -> Path:
    """Persist one value into config/local.yaml (created if missing). Used for personal settings."""
    lp = Path(local_path) if local_path else LOCAL_SETTINGS
    data: dict = {}
    if lp.exists():
        data = yaml.safe_load(lp.read_text(encoding="utf-8")) or {}
    node = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    lp.write_text("# Personal overrides of settings.yaml. Gitignored: never commit this file.\n"
                  + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    settings.cache_clear()
    return lp


@lru_cache(maxsize=1)
def settings() -> Settings:
    """Process-wide default settings (config/settings.yaml)."""
    return load_settings()
