from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone


def setup_logging(level: str) -> None:
    level = level.upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
