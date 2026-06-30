from __future__ import annotations
import yaml
from pathlib import Path
from typing import Any


class Config:
    """Loads config.yaml and provides attribute-style access with dot notation."""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            path = Path(__file__).parents[1] / "config" / "config.yaml"
        with open(path) as f:
            self._data = yaml.safe_load(f)

    def get(self, *keys: str, default: Any = None) -> Any:
        node = self._data
        for k in keys:
            if not isinstance(node, dict):
                return default
            node = node.get(k, default)
        return node

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        val = self._data.get(name)
        if isinstance(val, dict):
            return _DotDict(val)
        return val

    def raw(self) -> dict:
        return self._data


class _DotDict:
    def __init__(self, d: dict) -> None:
        object.__setattr__(self, "_d", d)

    def __getattr__(self, name: str) -> Any:
        val = self._d.get(name)
        if isinstance(val, dict):
            return _DotDict(val)
        return val

    def __getitem__(self, key: str) -> Any:
        return self._d[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)

    def __repr__(self) -> str:
        return repr(self._d)
