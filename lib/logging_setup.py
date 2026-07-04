"""lib/logging_setup.py — единый логгер: файл с ротацией (10MB×5) + stderr."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from lib import config

_FMT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    os.makedirs(config.LOG_DIR, exist_ok=True)

    fh = RotatingFileHandler(
        os.path.join(config.LOG_DIR, f"{name}.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter(_FMT))
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter(_FMT))
    logger.addHandler(sh)
    return logger
