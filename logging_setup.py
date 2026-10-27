"""Loguru configuration: console + rotated file sink at INFO level."""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

import config


def setup_logging() -> None:
    """Configure loguru sinks for the application.

    Adds a stderr sink and a rotated file sink based on values in ``config``.
    Safe to call multiple times: existing sinks are removed first.
    """
    logger.remove()

    logger.add(
        sys.stderr,
        level=config.LOG_LEVEL,
        backtrace=False,
        diagnose=False,
    )

    log_path = Path(config.LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_path,
        level=config.LOG_LEVEL,
        rotation=config.LOG_ROTATION,
        retention=config.LOG_RETENTION,
        compression=config.LOG_COMPRESSION,
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )
