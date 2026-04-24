"""Logging configuration."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler


def setup_logging(level: int = logging.INFO) -> None:
    """Configure logging for the application.

    Always logs to stdout. Additionally, when ``CCDB_LOG_FILE`` env var is set,
    appends to that file with rotation (10MB * 3 backups).

    Pre-A instrumentation: set ``CCDB_LOG_FILE=/tmp/ccdb-bot.log`` before
    starting the bot to capture permission/approval event traces.
    """
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    root.addHandler(stdout_handler)

    log_file = os.environ.get("CCDB_LOG_FILE", "").strip()
    if log_file:
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
        root.info("File logging enabled: %s", log_file)

    # Quiet down discord.py's verbose logging
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
