from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from html import escape
import base64
import json
import logging
from pathlib import Path
import sqlite3
from typing import Iterable
from zoneinfo import ZoneInfo
import zlib

import dns.asyncresolver
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import uvicorn

from app.config import ApiConfig, load_config
from app.utils import setup_logging, utc_now_iso

logger = logging.getLogger("newsgate.client")
PAYLOAD_VERSION = 2


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _normalize_path(path: str) -> str:
    path = (path or "/news").strip()
    if not path.startswith("/"):
        path = f"/{path}"
    return path.rstrip("/") or "/"


try:
    TEHRAN_TZ = ZoneInfo("Asia/Tehran")
except Exception:
    TEHRAN_TZ = timezone.utc


def _format_ui_timestamp(value: str) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(TEHRAN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def _load_resolvers(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]


def _load_ok_resolvers(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _save_ok_resolvers(path: Path, resolvers: Iterable[str]) -> None:
    _ensure_parent(path)
    path.write_text("\n".join(resolvers) + "\n", encoding="utf-8")


def _open_db(path: Path) -> sqlite3.Connection:
    _ensure_parent(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
          id TEXT PRIMARY KEY,
          rev TEXT NOT NULL,
          channel TEXT NOT NULL,
          text TEXT NOT NULL,
          sent_at TEXT,
          fetched_at TEXT NOT NULL,
          media_b64 TEXT,
          media_mime TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
          key TEXT PRIMARY KEY,
          value TEXT
        )
        """
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "media_b64" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN media_b64 TEXT")
    if "media_mime" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN media_mime TEXT")
    return conn


def _maybe_migrate_json(path: Path, conn: sqlite3.Connection) -> None:
    legacy_path = path.with_suffix(".json")
    if not legacy_path.exists():
        return
    existing = conn.execute("SELECT 1 FROM messages LIMIT 1").fetchone()
    if existing is not None:
        return
    try:
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    items = legacy.get("messages", [])
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str):
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO messages (id, rev, channel, text, sent_at, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                str(item.get("rev", "")),
                str(item.get("channel", "")),
                str(item.get("text", "")),
                str(item.get("sent_at", "")) or None,
                str(item.get("fetched_at", "")),
            ),
        )
    if isinstance(legacy.get("last_batch_id"), str):
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("last_batch_id", legacy["last_batch_id"]))
    if isinstance(legacy.get("last_updated"), str):
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("last_updated", legacy["last_updated"]))
    conn.commit()


def _load_messages(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        SELECT id, rev, channel, text, sent_at, fetched_at, media_b64, media_mime
        FROM messages
        ORDER BY COALESCE(sent_at, fetched_at)
        """
    ).fetchall()
    messages = [dict(row) for row in rows]
    meta_rows = conn.execute("SELECT key, value FROM meta").fetchall()
    meta = {row["key"]: row["value"] for row in meta_rows}
    result: dict = {"messages": messages}
    if "last_batch_id" in meta:
        result["last_batch_id"] = meta["last_batch_id"]
    if "last_updated" in meta:
        result["last_updated"] = meta["last_updated"]
    return result


def _derive_record_names(base_name: str, extra_records: int) -> list[str]:
    if extra_records <= 0:
        return [base_name]
    if "." not in base_name:
        return [base_name] + [f"{base_name}-{idx}" for idx in range(1, extra_records + 1)]

    head, tail = base_name.split(".", 1)
    return [base_name] + [f"{head}-{idx}.{tail}" for idx in range(1, extra_records + 1)]


async def _resolve_txt(resolver_ip: str, name: str, timeout: float) -> str | None:
    resolver = dns.asyncresolver.Resolver(configure=False)
    resolver.nameservers = [resolver_ip]
    resolver.timeout = timeout
    resolver.lifetime = timeout
    try:
        answer = await resolver.resolve(name, "TXT", tcp=False)
    except Exception:
        return None

    for rdata in answer:
        try:
            return b"".join(rdata.strings).decode("utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            continue
    return None


async def _pick_resolver(
    name: str,
    ok_resolvers: list[str],
    all_resolvers: list[str],
    timeout: float,
    max_parallel: int,
) -> tuple[str | None, str | None]:
    for resolver in ok_resolvers:
        payload = await _resolve_txt(resolver, name, timeout)
        if payload:
            return resolver, payload

    pending = [resolver for resolver in all_resolvers if resolver not in ok_resolvers]
    if not pending:
        return None, None

    semaphore = asyncio.Semaphore(max_parallel)

    async def run_one(resolver_ip: str) -> tuple[str, str | None]:
        async with semaphore:
            return resolver_ip, await _resolve_txt(resolver_ip, name, timeout)

    tasks = [asyncio.create_task(run_one(resolver)) for resolver in pending]
    try:
        while tasks:
            done, pending_tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            tasks = list(pending_tasks)
            for task in done:
                resolver_ip, payload = task.result()
                if payload:
                    for pending_task in tasks:
                        pending_task.cancel()
                    return resolver_ip, payload
    finally:
        for task in tasks:
            task.cancel()

    return None, None


def _parse_payload(payload: str) -> dict | None:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("v")
    if version == 1:
        return data
    if version == PAYLOAD_VERSION and data.get("k") == "empty":
        return data
    if version != PAYLOAD_VERSION:
        return None
    if data.get("z") != 1 or not isinstance(data.get("p"), str):
        return None
    try:
        raw = base64.b64decode(data["p"])
        inflated = zlib.decompress(raw).decode("utf-8")
        inner = json.loads(inflated)
        if not isinstance(inner, dict):
            return None
        return inner
    except Exception:
        return None


def _decode_payload(payload: str) -> str:
    return payload.replace("\\\\", "\\")


def _select_batch(payloads: list[str]) -> tuple[str | None, list[dict]]:
    groups: dict[str, dict[int, dict]] = {}
    for payload in payloads:
        data = _parse_payload(_decode_payload(payload))
        if not data or data.get("k") == "empty":
            continue
        batch_id = data.get("b")
        part_index = data.get("i")
        part_count = data.get("n")
        if not isinstance(batch_id, str) or not isinstance(part_index, int) or not isinstance(part_count, int):
            continue
        if part_count <= 0 or part_index < 0 or part_index >= part_count:
            continue
        groups.setdefault(batch_id, {})[part_index] = data

    complete: list[tuple[str, dict[int, dict]]] = []
    for batch_id, parts in groups.items():
        expected = next(iter(parts.values())).get("n", 0)
        if isinstance(expected, int) and len(parts) == expected and set(parts) == set(range(expected)):
            complete.append((batch_id, parts))

    if not complete:
        return None, []

    batch_id, parts = max(
        complete,
        key=lambda item: (
            len(item[1]),
            sum(len(part.get("m", [])) for part in item[1].values() if isinstance(part.get("m"), list)),
        ),
    )
    items: list[dict] = []
    for idx in range(len(parts)):
        payload_items = parts[idx].get("m", [])
        if isinstance(payload_items, list):
            items.extend(item for item in payload_items if isinstance(item, dict))
    return batch_id, items


async def _process_payloads(payloads: list[str], data_path: Path) -> bool:
    batch_id, incoming = _select_batch(payloads)
    if not batch_id:
        logger.info("No complete metadata batch available")
        return False

    conn = _open_db(data_path)
    try:
        _maybe_migrate_json(data_path, conn)
        existing_rows = conn.execute("SELECT id, rev FROM messages").fetchall()
        existing_revs = {row["id"]: row["rev"] for row in existing_rows}
        seen_in_batch: set[str] = set()
        changed = False

        for item in incoming:
            item_id = item.get("id")
            text = item.get("t")
            channel = item.get("c")
            sent_at = item.get("d", "")
            revision = item.get("r", "")
            if not isinstance(item_id, str) or not isinstance(text, str) or not isinstance(channel, str):
                continue
            media_b64 = item.get("mb")
            media_mime = item.get("mm")
            if not isinstance(media_b64, str):
                media_b64 = ""
            if not isinstance(media_mime, str):
                media_mime = ""
            if item_id in seen_in_batch:
                continue
            seen_in_batch.add(item_id)
            candidate = (
                item_id,
                revision if isinstance(revision, str) else "",
                channel,
                text,
                sent_at if isinstance(sent_at, str) else "",
                utc_now_iso(),
                media_b64 or None,
                media_mime or None,
            )
            existing_rev = existing_revs.get(item_id)
            if existing_rev is None:
                conn.execute(
                    """
                    INSERT INTO messages (id, rev, channel, text, sent_at, fetched_at, media_b64, media_mime)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    candidate,
                )
                changed = True
                continue
            if existing_rev != candidate[1]:
                conn.execute(
                    """
                    UPDATE messages
                    SET rev = ?, channel = ?, text = ?, sent_at = ?, fetched_at = ?, media_b64 = ?, media_mime = ?
                    WHERE id = ?
                    """,
                    (candidate[1], candidate[2], candidate[3], candidate[4], candidate[5], candidate[6], candidate[7], item_id),
                )
                changed = True

        if not changed:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("last_batch_id", batch_id))
            conn.commit()
            return False

        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("last_batch_id", batch_id))
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("last_updated", utc_now_iso()))
        conn.commit()
        return True
    finally:
        conn.close()


async def run_loop(use_api: bool) -> None:
    cfg = load_config()
    setup_logging(cfg.client.log_level)

    data_path = Path(cfg.client.data_file)
    conn = _open_db(data_path)
    _maybe_migrate_json(data_path, conn)
    conn.close()
    ok_path = Path(cfg.client.ok_resolvers_file)
    resolvers_path = Path(cfg.client.resolvers_file)

    if use_api and cfg.client.api.enabled:
        asyncio.get_running_loop().create_task(start_api_server(data_path, cfg.client.api))

    while True:
        ok_resolvers = _load_ok_resolvers(ok_path)
        all_resolvers = _load_resolvers(resolvers_path)
        record_names = _derive_record_names(cfg.client.dns_name, cfg.client.extra_records)

        resolver, first_payload = await _pick_resolver(
            record_names[0],
            ok_resolvers,
            all_resolvers,
            cfg.client.timeout_sec,
            cfg.client.max_parallel,
        )

        if resolver and first_payload:
            payloads = [first_payload]
            for name in record_names[1:]:
                payload = await _resolve_txt(resolver, name, cfg.client.timeout_sec)
                if payload:
                    payloads.append(payload)

            updated = await _process_payloads(payloads, data_path)
            if updated:
                logger.info("Updated local store using resolver %s", resolver)
            else:
                logger.info("No new messages")

            if resolver not in ok_resolvers:
                ok_resolvers.insert(0, resolver)
                _save_ok_resolvers(ok_path, ok_resolvers)
        else:
            logger.warning("No working resolver found")

        await asyncio.sleep(cfg.client.update_interval_sec)


async def start_api_server(data_path: Path, api_cfg: ApiConfig) -> None:
    app = FastAPI(title="NewsGate API", version="0.1.0")
    base_path = _normalize_path(api_cfg.path)
    data_path_route = "/data" if base_path == "/" else f"{base_path}/data"

    if api_cfg.allow_cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get(data_path_route)
    def get_news() -> dict:
        conn = _open_db(data_path)
        _maybe_migrate_json(data_path, conn)
        data = _load_messages(conn)
        conn.close()
        return data

    @app.get(base_path, response_class=HTMLResponse)
    def ui() -> str:
        conn = _open_db(data_path)
        _maybe_migrate_json(data_path, conn)
        data = _load_messages(conn)
        conn.close()
        items = data.get("messages", [])
        body = "".join(
            (
                "<article class='card'>"
                f"<div class='channel'>{escape(item.get('channel', 'Update'))}</div>"
                f"<div class='meta'>{escape(_format_ui_timestamp(item.get('sent_at') or item.get('fetched_at', '')))}</div>"
                + (
                    f"<img class='media' src='data:{item.get('media_mime') or 'image/jpeg'};base64,{item.get('media_b64')}' alt='media' />"
                    if item.get("media_b64")
                    else ""
                )
                + f"<pre class='message' dir='auto'>{escape(item.get('text', ''))}</pre>"
                + "</article>"
            )
            for item in reversed(items[-50:])
        )
        return f"""
        <!doctype html>
        <html>
        <head>
          <meta charset='utf-8' />
          <meta name='viewport' content='width=device-width, initial-scale=1' />
          <title>NewsGate</title>
          <style>
            :root {{
              --bg: #f4efe6;
              --bg-accent: #d8c3a5;
              --card: rgba(255, 252, 246, 0.9);
              --card-border: rgba(94, 59, 35, 0.12);
              --accent: #9a3412;
              --text: #2f241f;
              --muted: #6b5b53;
            }}
            * {{ box-sizing: border-box; }}
            body {{ margin: 0; font-family: "IBM Plex Sans Arabic", "Noto Sans Arabic", "Segoe UI", sans-serif; background:
              radial-gradient(circle at top left, rgba(154, 52, 18, 0.14), transparent 35%),
              radial-gradient(circle at top right, rgba(120, 53, 15, 0.12), transparent 30%),
              linear-gradient(135deg, var(--bg), var(--bg-accent)); color: var(--text); }}
            header {{ padding: 40px 24px 28px; text-align: center; }}
            h1 {{ margin: 0; letter-spacing: 0.04em; font-size: 32px; font-weight: 700; }}
            .meta-top {{ margin-top: 10px; color: var(--muted); font-size: 13px; }}
            .wrap {{ max-width: 900px; margin: 0 auto; padding: 0 16px 40px; }}
            .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; }}
            .card {{ background: var(--card); border: 1px solid var(--card-border); border-radius: 20px; padding: 18px; box-shadow: 0 16px 40px rgba(67, 43, 27, 0.12); backdrop-filter: blur(6px); }}
            .channel {{ margin: 0 0 8px; font-size: 14px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--accent); }}
            .meta {{ margin-bottom: 12px; font-size: 12px; color: var(--muted); }}
            .media {{ width: 100%; border-radius: 14px; display: block; margin-bottom: 12px; object-fit: cover; box-shadow: 0 10px 24px rgba(67, 43, 27, 0.12); }}
            .message {{ margin: 0; white-space: pre-wrap; word-break: break-word; line-height: 1.8; unicode-bidi: plaintext; text-align: start; font: inherit; }}
            .empty {{ margin: 0; padding: 24px; text-align: center; color: var(--muted); background: var(--card); border: 1px solid var(--card-border); border-radius: 20px; }}
            footer {{ padding: 24px; text-align: center; color: var(--muted); font-size: 12px; }}
            footer a {{ color: var(--accent); text-decoration: none; border-bottom: 1px solid rgba(154, 52, 18, 0.25); }}
            .gh {{ display: inline-flex; gap: 8px; align-items: center; justify-content: center; margin-left: 6px; }}
            .gh svg {{ width: 16px; height: 16px; fill: currentColor; }}
          </style>
        </head>
        <body>
          <header>
            <h1>NewsGate Live Feed</h1>
            <p class='meta-top'>Updated {escape(_format_ui_timestamp(data.get('last_updated', '')))}</p>
          </header>
          <div class='wrap'>
            <div class='grid'>
              {body or "<p class='empty'>No messages yet.</p>"}
            </div>
          </div>
          <footer>
            made with ❤️ by devmmk
            <span class='gh'>
              <a href='https://github.com/devmmk' target='_blank' rel='noopener'>
                <svg viewBox='0 0 16 16' aria-hidden='true' focusable='false'>
                  <path d='M8 0a8 8 0 0 0-2.53 15.59c.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.5 7.5 0 0 1 2-.27c.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8 8 0 0 0 8 0Z' />
                </svg>
                GitHub
              </a>
            </span>
          </footer>
        </body>
        </html>
        """

    config = uvicorn.Config(app, host=api_cfg.host, port=api_cfg.port, log_level="info")
    await uvicorn.Server(config).serve()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", action="store_true", help="Run FastAPI server")
    args = parser.parse_args()
    asyncio.run(run_loop(args.api))


if __name__ == "__main__":
    main()
