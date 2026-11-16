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


def load_settings(path: str | Path | None = None) -> Settings:
    p = Path(path) if path else DEFAULT_SETTINGS
    with open(p, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return Settings(data, p)


@lru_cache(maxsize=1)
def settings() -> Settings:
    """Process-wide default settings (config/settings.yaml)."""
    return load_settings()
