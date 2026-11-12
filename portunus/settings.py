"""User settings with a fixed schema.

Only keys listed in SCHEMA are ever read or written, and every value is
type-checked and clamped. Nothing secret is stored here: no PIN, no
clipboard content, no typed text.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

from . import APP_NAME


def _choice(*allowed: str) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if value not in allowed:
            raise ValueError
        return value

    return check


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError
    return value


def _int_range(low: int, high: int) -> Callable[[Any], int]:
    def check(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError
        return max(low, min(high, int(value)))

    return check


# key -> (default, validator)
SCHEMA: dict[str, tuple[Any, Callable[[Any], Any]]] = {
    "theme": ("dark", _choice("system", "light", "dark")),
    "ctrlAsCmd": (True, _boolean),
    "scrollSpeed": (100, _int_range(40, 250)),
    "reflection": (True, _boolean),
    "autoUpdateCheck": (True, _boolean),
    "allowWifi": (False, _boolean),
    "railCompact": (False, _boolean),
}


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


class Settings:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (config_dir() / "settings.json")
        self._lock = threading.Lock()
        self._values = {key: default for key, (default, _) in SCHEMA.items()}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            self._merge(raw)

    def _merge(self, incoming: dict) -> None:
        for key, value in incoming.items():
            spec = SCHEMA.get(key)
            if spec is None:
                continue
            try:
                self._values[key] = spec[1](value)
            except ValueError:
                continue

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._values)

    def update(self, incoming: Any) -> dict[str, Any]:
        if not isinstance(incoming, dict):
            raise ValueError("settings must be an object")
        with self._lock:
            self._merge(incoming)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._values, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
            return dict(self._values)
