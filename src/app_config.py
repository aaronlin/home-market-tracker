from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml


APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_ROOT


class SetupError(RuntimeError):
    """Raised when local config or credentials are missing."""


def load_dotenv() -> None:
    path = APP_ROOT / ".env.local"
    if path.exists():
        _load_env_file(path)


def _load_env_file(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = _clean_env_value(value.strip())
        if key and key not in os.environ:
            os.environ[key] = value


def _clean_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def resolve_repo_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return REPO_ROOT / path


def load_yaml(path: str | Path) -> Dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected mapping in {path}")
    return loaded


def load_settings() -> Dict[str, Any]:
    configured = os.environ.get("HOME_MARKET_SETTINGS_PATH", "").strip()
    if configured:
        path = Path(configured)
        if not path.exists():
            raise SetupError(f"Configured settings file does not exist: {path}")
        return load_yaml(path)
    candidates = [
        APP_ROOT / "config" / "settings.yaml",
    ]
    for path in candidates:
        if path.exists():
            return load_yaml(path)
    raise SetupError("Missing settings file. Copy config/settings.example.yaml to config/settings.yaml.")


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SetupError(f"Missing env var: {name}")
    return value
