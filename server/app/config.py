from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT_DIR / "config.yaml"


def _coerce_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    return str(val).strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class TelegramConfig:
    api_id: int
    api_hash: str
    phone: str
    source_channels: list[str]


@dataclass
class CloudflareConfig:
    api_token: str
    zone_id: str
    record_name: str
    record_id: str | None
    ttl: int
    proxied: bool
    extra_records: int


@dataclass
class AuthoritativeDnsConfig:
    domain: str
    host: str
    port: int
    ttl: int
    ns_host: str
    soa_email: str
    tcp: bool


@dataclass
class ServerConfig:
    mode: str
    update_interval_sec: int
    max_messages: int
    max_txt_len: int
    send_media: bool
    session_name: str
    telegram: TelegramConfig
    cloudflare: CloudflareConfig
    authoritative_dns: AuthoritativeDnsConfig
    log_level: str


@dataclass
class AppConfig:
    server: ServerConfig


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    server_raw = raw.get("server", {})
    telegram_raw = server_raw.get("telegram", {})
    cloudflare_raw = server_raw.get("cloudflare", {})
    authoritative_raw = server_raw.get("authoritative_dns", {})

    telegram = TelegramConfig(
        api_id=int(telegram_raw.get("api_id", 0)),
        api_hash=str(telegram_raw.get("api_hash", "")),
        phone=str(telegram_raw.get("phone", "")),
        source_channels=list(telegram_raw.get("source_channels", [])),
    )

    cloudflare = CloudflareConfig(
        api_token=str(cloudflare_raw.get("api_token", "")),
        zone_id=str(cloudflare_raw.get("zone_id", "")),
        record_name=str(cloudflare_raw.get("record_name", "")),
        record_id=cloudflare_raw.get("record_id"),
        ttl=int(cloudflare_raw.get("ttl", 120)),
        proxied=_coerce_bool(cloudflare_raw.get("proxied", False)),
        extra_records=int(cloudflare_raw.get("extra_records", 0)),
    )

    authoritative_dns = AuthoritativeDnsConfig(
        domain=str(authoritative_raw.get("domain", "news.example.com")),
        host=str(authoritative_raw.get("host", "0.0.0.0")),
        port=int(authoritative_raw.get("port", 5353)),
        ttl=int(authoritative_raw.get("ttl", 30)),
        ns_host=str(authoritative_raw.get("ns_host", "ns1.news.example.com")),
        soa_email=str(authoritative_raw.get("soa_email", "hostmaster.news.example.com")),
        tcp=_coerce_bool(authoritative_raw.get("tcp", True)),
    )

    server = ServerConfig(
        mode=str(server_raw.get("mode", "cloudflare")).strip().lower(),
        update_interval_sec=int(server_raw.get("update_interval_sec", 60)),
        max_messages=int(server_raw.get("max_messages", 30)),
        max_txt_len=int(server_raw.get("max_txt_len", 1800)),
        send_media=_coerce_bool(server_raw.get("send_media", False)),
        session_name=str(server_raw.get("session_name", "newsgate")),
        telegram=telegram,
        cloudflare=cloudflare,
        authoritative_dns=authoritative_dns,
        log_level=str(server_raw.get("log_level", "INFO")),
    )

    return AppConfig(server=server)
