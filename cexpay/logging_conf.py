"""日志配置。"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_CONFIGURED = False


def setup_logging(level: str | None = None, log_file: Path | None = None) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("cexpay")
    if _CONFIGURED:
        return logger

    level_name = (level or os.environ.get("CEXPAY_LOG_LEVEL", "INFO")).upper()
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    logger.propagate = False
    _CONFIGURED = True
    return logger
