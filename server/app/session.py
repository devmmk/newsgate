from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telethon import TelegramClient

from app.config import load_config
from app.utils import setup_logging

logger = logging.getLogger("newsgate.session")


async def generate_session() -> None:
    cfg = load_config()
    setup_logging(cfg.server.log_level)

    sessions_dir = Path("sessions")
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_path = sessions_dir / cfg.server.session_name
    logger.info("Generating session at %s", session_path)

    client = TelegramClient(str(session_path), cfg.server.telegram.api_id, cfg.server.telegram.api_hash)
    async with client:
        await client.start(phone=cfg.server.telegram.phone)
        logger.info("Session generated")


def main() -> None:
    asyncio.run(generate_session())


if __name__ == "__main__":
    main()
