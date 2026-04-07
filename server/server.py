from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import zlib
from datetime import timezone

import requests
from telethon import TelegramClient
from telethon.tl.types import Message

from app.authoritative_dns import AuthoritativeDnsSettings, SnapshotStore, start_authoritative_dns
from app.config import AppConfig, load_config
from app.utils import setup_logging

logger = logging.getLogger("newsgate.server")

CF_TXT_SAFE_BYTES = 4000
PAYLOAD_VERSION = 2
EMPTY_RECORD = {"v": PAYLOAD_VERSION, "k": "empty"}


def _json_dumps(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _payload_budget(max_len: int) -> int:
    return min(max_len, CF_TXT_SAFE_BYTES)


def _derive_record_names(base_name: str, extra_records: int) -> list[str]:
    if extra_records <= 0:
        return [base_name]
    if "." not in base_name:
        return [base_name] + [f"{base_name}-{idx}" for idx in range(1, extra_records + 1)]

    head, tail = base_name.split(".", 1)
    return [base_name] + [f"{head}-{idx}.{tail}" for idx in range(1, extra_records + 1)]


async def _extract_media(client: TelegramClient, msg: Message, send_media: bool) -> tuple[str | None, str | None, str]:
    if not send_media:
        return None, None, ""
    if getattr(msg, "grouped_id", None):
        return None, None, ""
    if not getattr(msg, "photo", None):
        return None, None, ""

    data = None
    try:
        data = await client.download_media(msg, file=bytes, thumb=-1)
    except Exception:
        data = None
    if data is None:
        try:
            data = await client.download_media(msg, file=bytes)
        except Exception:
            data = None

    if not data:
        return None, None, ""
    mime = getattr(getattr(msg, "file", None), "mime_type", None) or "image/jpeg"
    media_b64 = base64.b64encode(data).decode("ascii")
    media_hash = hashlib.sha1(data).hexdigest()[:12]
    return media_b64, mime, media_hash


async def _message_to_item(
    client: TelegramClient,
    msg: Message,
    channel_name: str,
    channel_id: int | None,
    send_media: bool,
) -> dict[str, str] | None:
    text = msg.raw_text or ""
    media_b64, media_mime, media_hash = await _extract_media(client, msg, send_media)
    if not text.strip() and not media_b64:
        return None
    sent_at = msg.date.astimezone(timezone.utc).isoformat() if msg.date else ""
    revision = hashlib.sha1(f"{msg.id}|{sent_at}|{text}|{media_hash}".encode("utf-8")).hexdigest()[:12]

    item = {
        "id": f"{channel_id or 0}:{msg.id}",
        "c": channel_name,
        "t": text,
        "r": revision,
    }
    if sent_at:
        item["d"] = sent_at
    if media_b64:
        item["mb"] = media_b64
        item["mm"] = media_mime
    return item


def _dedupe_items(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for item in items:
        item_id = item["id"]
        if item_id in seen:
            continue
        seen.add(item_id)
        deduped.append(item)
    return deduped


def _batch_id(items: list[dict[str, str]]) -> str:
    raw = "|".join(f"{item['id']}:{item.get('r', '')}" for item in items)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _envelope(batch_id: str, part_index: int, part_count: int, items: list[dict[str, str]]) -> str:
    return _json_dumps({"v": PAYLOAD_VERSION, "b": batch_id, "i": part_index, "n": part_count, "m": items})


def _encode_payload_for_dns(payload: str) -> str:
    return payload.replace("\\", "\\\\")


def _payload_fits(payload: str, budget: int) -> bool:
    return _utf8_len(payload) <= budget and len(payload) <= budget


def _wrap_payload(payload: str) -> str:
    compressed = zlib.compress(payload.encode("utf-8"))
    b64 = base64.b64encode(compressed).decode("ascii")
    return _json_dumps({"v": PAYLOAD_VERSION, "z": 1, "p": b64})


def _can_fit(item: dict[str, str], batch_id: str, part_index: int, part_count: int, budget: int) -> bool:
    payload = _encode_payload_for_dns(_wrap_payload(_envelope(batch_id, part_index, part_count, [item])))
    return _payload_fits(payload, budget)


def _partition_items(
    items: list[dict[str, str]],
    max_len: int,
    max_records: int,
) -> tuple[list[str], int]:
    budget = _payload_budget(max_len)
    if budget <= 0:
        raise RuntimeError("max_txt_len must be greater than zero.")

    if not items:
        return [], 0

    kept = [item for item in items if _can_fit(item, "0" * 16, 0, max_records or 1, budget)]
    dropped = len(items) - len(kept)
    if not kept:
        return [], dropped

    batch_id = _batch_id(kept)
    parts: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []

    for item in kept:
        probe = current + [item]
        payload = _encode_payload_for_dns(_wrap_payload(_envelope(batch_id, len(parts), max_records or 1, probe)))
        if _payload_fits(payload, budget):
            current = probe
            continue

        if current:
            parts.append(current)
            if len(parts) >= max_records:
                current = []
                break
            current = [item]
            continue

        dropped += 1

    if current and len(parts) < max_records:
        parts.append(current)

    part_count = len(parts)
    payloads = [_envelope(batch_id, idx, part_count, part_items) for idx, part_items in enumerate(parts)]
    for payload in payloads:
        if not _payload_fits(_encode_payload_for_dns(_wrap_payload(payload)), budget):
            raise RuntimeError("Payload exceeds configured max_txt_len budget.")
    used = sum(len(part) for part in parts)
    return payloads, len(items) - used


def _record_payloads(record_names: list[str], payloads: list[str], max_len: int) -> dict[str, str]:
    budget = _payload_budget(max_len)
    empty_payload = _encode_payload_for_dns(_json_dumps(EMPTY_RECORD))
    if not _payload_fits(empty_payload, budget):
        raise RuntimeError("max_txt_len is too small for empty record metadata.")

    result = {name: empty_payload for name in record_names}
    for name, payload in zip(record_names, payloads, strict=False):
        wire_payload = _encode_payload_for_dns(_wrap_payload(payload))
        if not _payload_fits(wire_payload, budget):
            logger.warning("Payload for %s exceeds TXT budget; publishing empty record instead", name)
            result[name] = empty_payload
            continue
        result[name] = wire_payload
    return result


def _assert_records_in_zone(record_names: list[str], zone: str) -> None:
    normalized_zone = zone.rstrip(".").lower()
    for name in record_names:
        normalized_name = name.rstrip(".").lower()
        if normalized_name == normalized_zone or normalized_name.endswith(f".{normalized_zone}"):
            continue
        raise RuntimeError(f"Record name {name} is outside authoritative zone {zone}")


async def _build_snapshot(cfg: AppConfig, client: TelegramClient, record_names: list[str]) -> dict[str, str]:
    items = await _collect_items(
        client,
        cfg.server.telegram.source_channels,
        cfg.server.max_messages,
        cfg.server.send_media,
    )
    payloads, dropped = _partition_items(items, cfg.server.max_txt_len, len(record_names))
    if dropped:
        logger.warning("Dropped %s messages that did not fit in current TXT snapshot", dropped)
    return _record_payloads(record_names, payloads, cfg.server.max_txt_len)


def _cloudflare_request(method: str, url: str, api_token: str, body: dict | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    resp = requests.request(method, url, headers=headers, json=body, timeout=15)
    if not resp.ok:
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text
        raise RuntimeError(f"Cloudflare HTTP {resp.status_code}: {detail}")

    data = resp.json()
    if not data.get("success", False):
        raise RuntimeError(f"Cloudflare error: {data}")
    return data


def _ensure_record(cfg: AppConfig, record_name: str, record_id: str | None = None) -> str:
    cf = cfg.server.cloudflare
    if record_id:
        return record_id

    url = f"https://api.cloudflare.com/client/v4/zones/{cf.zone_id}/dns_records"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {cf.api_token}"},
        params={"type": "TXT", "name": record_name},
        timeout=15,
    )
    if not resp.ok:
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text
        raise RuntimeError(f"Cloudflare HTTP {resp.status_code}: {detail}")

    data = resp.json()
    if data.get("success") and data.get("result"):
        return data["result"][0]["id"]

    body = {
        "type": "TXT",
        "name": record_name,
        "content": _json_dumps(EMPTY_RECORD),
        "ttl": cf.ttl,
        "proxied": cf.proxied,
    }
    created = _cloudflare_request("POST", url, cf.api_token, body)
    return created["result"]["id"]


def _update_record(cfg: AppConfig, record_id: str, record_name: str, content: str) -> None:
    cf = cfg.server.cloudflare
    url = f"https://api.cloudflare.com/client/v4/zones/{cf.zone_id}/dns_records/{record_id}"
    body = {
        "type": "TXT",
        "name": record_name,
        "content": content,
        "ttl": cf.ttl,
        "proxied": cf.proxied,
    }
    _cloudflare_request("PUT", url, cf.api_token, body)


async def _collect_items(
    client: TelegramClient,
    channels: list[str],
    limit: int,
    send_media: bool,
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for channel in channels:
        entity = await client.get_entity(channel)
        channel_name = getattr(entity, "title", channel)
        channel_id = getattr(entity, "id", None)
        for msg in await client.get_messages(entity, limit=limit):
            item = await _message_to_item(client, msg, channel_name, channel_id, send_media)
            if item is not None:
                items.append(item)
    return sorted(_dedupe_items(items), key=lambda item: item.get("d", ""), reverse=True)


async def _run_cloudflare_mode(cfg: AppConfig, client: TelegramClient) -> None:
    record_names = _derive_record_names(
        cfg.server.cloudflare.record_name,
        cfg.server.cloudflare.extra_records,
    )
    record_ids: dict[str, str] = {}
    last_payloads: dict[str, str] = {}

    for name in record_names:
        pinned_id = cfg.server.cloudflare.record_id if name == cfg.server.cloudflare.record_name else None
        record_ids[name] = _ensure_record(cfg, name, pinned_id)

    while True:
        try:
            for name, content in (await _build_snapshot(cfg, client, record_names)).items():
                if content == last_payloads.get(name):
                    continue
                logger.info("TXT payload bytes for %s: %s", name, _utf8_len(content))
                _update_record(cfg, record_ids[name], name, content)
                last_payloads[name] = content
        except Exception as exc:
            logger.exception("Cloudflare publish error: %s", exc)

        await asyncio.sleep(cfg.server.update_interval_sec)


async def _run_authoritative_mode(cfg: AppConfig, client: TelegramClient) -> None:
    auth_cfg = cfg.server.authoritative_dns
    record_names = _derive_record_names(
        cfg.server.cloudflare.record_name,
        cfg.server.cloudflare.extra_records,
    )
    _assert_records_in_zone(record_names, auth_cfg.domain)
    store = SnapshotStore()
    start_authoritative_dns(
        AuthoritativeDnsSettings(
            domain=auth_cfg.domain,
            host=auth_cfg.host,
            port=auth_cfg.port,
            ttl=auth_cfg.ttl,
            ns_host=auth_cfg.ns_host,
            soa_email=auth_cfg.soa_email,
            tcp=auth_cfg.tcp,
        ),
        store,
    )
    logger.info(
        "Authoritative DNS listening on %s:%s for zone %s",
        auth_cfg.host,
        auth_cfg.port,
        auth_cfg.domain,
    )

    while True:
        try:
            snapshot = await _build_snapshot(cfg, client, record_names)
            for name, content in snapshot.items():
                logger.info("TXT payload bytes for %s: %s", name, _utf8_len(content))
            store.set_payloads(snapshot)
        except Exception as exc:
            logger.exception("Authoritative DNS refresh error: %s", exc)

        await asyncio.sleep(cfg.server.update_interval_sec)


async def run_loop(cfg: AppConfig) -> None:
    setup_logging(cfg.server.log_level)
    logger.info("Starting server in %s mode", cfg.server.mode)

    client = TelegramClient(
        f"sessions/{cfg.server.session_name}",
        cfg.server.telegram.api_id,
        cfg.server.telegram.api_hash,
    )

    async with client:
        if cfg.server.mode == "authoritative_dns":
            await _run_authoritative_mode(cfg, client)
            return
        await _run_cloudflare_mode(cfg, client)


def main() -> None:
    asyncio.run(run_loop(load_config()))


if __name__ == "__main__":
    main()
