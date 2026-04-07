from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT_DIR / "config.yaml"


def _coerce_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    return str(val).strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class ApiConfig:
    enabled: bool
    host: str
    port: int
    path: str
    allow_cors: bool


@dataclass
class ClientConfig:
    update_interval_sec: int
    dns_name: str
    extra_records: int
    resolvers_file: str
    ok_resolvers_file: str
    data_file: str
    timeout_sec: float
    max_parallel: int
    api: ApiConfig
    log_level: str


@dataclass
class AppConfig:
    client: ClientConfig


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    client_raw = raw.get("client", {})
    api_raw = client_raw.get("api", {})

    api = ApiConfig(
        enabled=_coerce_bool(api_raw.get("enabled", True)),
        host=str(api_raw.get("host", "0.0.0.0")),
        port=int(api_raw.get("port", 8080)),
        path=str(api_raw.get("path", "/news")),
        allow_cors=_coerce_bool(api_raw.get("allow_cors", False)),
    )

    client = ClientConfig(
        update_interval_sec=int(client_raw.get("update_interval_sec", 30)),
        dns_name=str(client_raw.get("dns_name", "news.example.com")),
        extra_records=int(client_raw.get("extra_records", 0)),
        resolvers_file=str(client_raw.get("resolvers_file", "resolvers.txt")),
        ok_resolvers_file=str(client_raw.get("ok_resolvers_file", "data/ok_resolvers.txt")),
        data_file=str(client_raw.get("data_file", "data/messages.sqlite3")),
        timeout_sec=float(client_raw.get("timeout_sec", 2.0)),
        max_parallel=int(client_raw.get("max_parallel", 25)),
        api=api,
        log_level=str(client_raw.get("log_level", "INFO")),
    )

    return AppConfig(client=client)
