"""Configuration of jmap-mcp: config.yml + environment. No secrets anywhere.

Layering: environment > .env file > config.yml > built-in defaults. The .env
file is read with a tiny KEY=VALUE parser (no extra dependency) and only fills
variables that are not already present in the environment.

There is nothing secret in this module: the only credential of the server is
the bearer of the request being served, and it never reaches the configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = "config.yml"

#: Built-in defaults. Every one of them can be overridden by config.yml or env.
DEFAULTS: dict[str, Any] = {
    "stalwart": {
        "url": "http://127.0.0.1:8080",
        "session_path": "/jmap/session",
        "request_timeout_sec": 30,
        "max_attachment_mb": 25,
        "max_page_size": 50,
        "max_body_chars": 20000,
        "allow_roots": ["."],
        "download_dir": None,
        "session_cache_ttl_sec": 60,
    },
    "listen": "127.0.0.1",
    "port": 8000,
    "log_level": "info",
}

#: environment variable -> path inside the settings tree.
ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "STALWART_URL": ("stalwart", "url"),
    "STALWART_SESSION_PATH": ("stalwart", "session_path"),
    "REQUEST_TIMEOUT_SEC": ("stalwart", "request_timeout_sec"),
    "MAX_ATTACHMENT_MB": ("stalwart", "max_attachment_mb"),
    "MAX_PAGE_SIZE": ("stalwart", "max_page_size"),
    "MAX_BODY_CHARS": ("stalwart", "max_body_chars"),
    "ALLOW_ROOTS": ("stalwart", "allow_roots"),
    "DOWNLOAD_DIR": ("stalwart", "download_dir"),
    "SESSION_CACHE_TTL_SEC": ("stalwart", "session_cache_ttl_sec"),
    "HOST": ("listen",),
    # MCP_PORT is an alias of PORT; PORT wins when both are set.
    "MCP_PORT": ("port",),
    "PORT": ("port",),
    "LOG_LEVEL": ("log_level",),
}


@dataclass(frozen=True)
class StalwartSettings:
    """The single upstream of the server. Holds no credential."""

    url: str
    session_path: str
    request_timeout_sec: float
    max_attachment_mb: int
    max_page_size: int
    max_body_chars: int
    allow_roots: tuple[str, ...]
    download_dir: str | None
    session_cache_ttl_sec: int


@dataclass(frozen=True)
class Settings:
    """Everything the server needs to know at start-up."""

    stalwart: StalwartSettings
    listen: str
    port: int
    log_level: str


def read_dotenv(path: str = ".env") -> dict[str, str]:
    """Read a .env file into a dict. Comments and blank lines are skipped."""
    values: dict[str, str] = {}
    file = Path(path)
    if not file.is_file():
        return values
    for raw_line in file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_yaml_config(path: str | None = None) -> dict[str, Any]:
    """Read config.yml. A missing file is not an error - defaults take over."""
    config_path = path or os.environ.get("CONFIG_PATH") or DEFAULT_CONFIG_PATH
    file = Path(config_path)
    if not file.is_file():
        return {}
    with file.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{config_path}: the root of the config must be a mapping")
    return loaded


def _as_list(value: Any) -> list[str]:
    """A list from yaml, or from a comma separated env value."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(item) for item in value]


def _merged(raw: dict[str, Any]) -> dict[str, Any]:
    """Defaults <- config.yml. Only the keys present in the file are touched."""
    merged: dict[str, Any] = {
        "stalwart": dict(DEFAULTS["stalwart"]),
        "listen": DEFAULTS["listen"],
        "port": DEFAULTS["port"],
        "log_level": DEFAULTS["log_level"],
    }
    for key, value in raw.items():
        if key == "stalwart" and isinstance(value, dict):
            merged["stalwart"].update(value)
        elif key in merged:
            merged[key] = value
    return merged


def _apply_env(merged: dict[str, Any], dotenv: dict[str, str]) -> dict[str, Any]:
    """Environment wins over the yaml. An empty env value is treated as absent."""
    for env_key, path in ENV_OVERRIDES.items():
        raw = os.environ.get(env_key)
        if raw is None or raw.strip() == "":
            raw = dotenv.get(env_key)
        if raw is None or str(raw).strip() == "":
            continue
        node = merged
        for part in path[:-1]:
            node = node[part]
        node[path[-1]] = raw
    return merged


def load_settings(config_path: str | None = None) -> Settings:
    """Build the settings out of config.yml, .env and the environment."""
    merged = _merged(load_yaml_config(config_path))
    merged = _apply_env(merged, read_dotenv())

    stalwart_raw = merged["stalwart"]
    return Settings(
        stalwart=StalwartSettings(
            url=str(stalwart_raw["url"]).rstrip("/"),
            session_path=str(stalwart_raw["session_path"]),
            request_timeout_sec=float(stalwart_raw["request_timeout_sec"]),
            max_attachment_mb=int(stalwart_raw["max_attachment_mb"]),
            max_page_size=int(stalwart_raw["max_page_size"]),
            max_body_chars=int(stalwart_raw["max_body_chars"]),
            allow_roots=tuple(_as_list(stalwart_raw["allow_roots"])),
            download_dir=(str(stalwart_raw["download_dir"]) if stalwart_raw["download_dir"] else None),
            session_cache_ttl_sec=int(stalwart_raw["session_cache_ttl_sec"]),
        ),
        listen=str(merged["listen"]),
        port=int(merged["port"]),
        log_level=str(merged["log_level"]),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings of the running process."""
    return load_settings()


def reset_settings_cache() -> None:
    """Drop the cached settings (tests, reconfiguration)."""
    get_settings.cache_clear()
