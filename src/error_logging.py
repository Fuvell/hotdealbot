from __future__ import annotations

import atexit
import logging
import sys
from datetime import datetime
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from queue import SimpleQueue
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ERROR_LOG_MAX_BYTES = 2 * 1024 * 1024
ERROR_LOG_BACKUP_COUNT = 5
AUDIT_LOG_MAX_BYTES = 2 * 1024 * 1024
AUDIT_LOG_BACKUP_COUNT = 5
RUNTIME_LOG_MAX_BYTES = 2 * 1024 * 1024
RUNTIME_LOG_BACKUP_COUNT = 3

# Loggers hand records to an in-memory queue; a background listener thread
# does the actual file/console I/O. This keeps the asyncio event loop from
# ever blocking on log writes (notably: Windows consoles pause all writers
# while the user has text selected in Quick-Edit mode).
_CONFIGURED_LOGGERS: dict[str, logging.Logger] = {}
_LISTENERS: list[QueueListener] = []


class KSTFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        kst_datetime = datetime.fromtimestamp(record.created, KST)
        if datefmt:
            return kst_datetime.strftime(datefmt)
        return kst_datetime.strftime("%Y-%m-%d %H:%M:%S")


def get_error_logger(base_dir: Path | None = None) -> logging.Logger:
    return _get_queued_logger(
        logger_name="hotdealbot.error",
        filename="error_log.txt",
        level=logging.ERROR,
        max_bytes=ERROR_LOG_MAX_BYTES,
        backup_count=ERROR_LOG_BACKUP_COUNT,
        base_dir=base_dir,
    )


def get_audit_logger(base_dir: Path | None = None) -> logging.Logger:
    return _get_queued_logger(
        logger_name="hotdealbot.audit",
        filename="audit_log.txt",
        level=logging.INFO,
        max_bytes=AUDIT_LOG_MAX_BYTES,
        backup_count=AUDIT_LOG_BACKUP_COUNT,
        base_dir=base_dir,
    )


def get_runtime_logger(base_dir: Path | None = None) -> logging.Logger:
    """Console + file logger that replaces print() for status output."""
    return _get_queued_logger(
        logger_name="hotdealbot.runtime",
        filename="runtime_log.txt",
        level=logging.INFO,
        max_bytes=RUNTIME_LOG_MAX_BYTES,
        backup_count=RUNTIME_LOG_BACKUP_COUNT,
        base_dir=base_dir,
        with_console=True,
    )


def _get_queued_logger(
    *,
    logger_name: str,
    filename: str,
    level: int,
    max_bytes: int,
    backup_count: int,
    base_dir: Path | None = None,
    with_console: bool = False,
) -> logging.Logger:
    existing = _CONFIGURED_LOGGERS.get(logger_name)
    if existing is not None:
        return existing

    log_dir = base_dir or Path(__file__).resolve().parent.parent
    log_file = (log_dir / filename).resolve()

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(
        KSTFormatter(
            fmt="%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    handlers: list[logging.Handler] = [file_handler]
    if with_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(
            KSTFormatter(fmt="%(asctime)s | %(message)s", datefmt="%H:%M:%S")
        )
        handlers.append(console_handler)

    queue: SimpleQueue = SimpleQueue()
    listener = QueueListener(queue, *handlers, respect_handler_level=True)
    listener.start()
    _LISTENERS.append(listener)

    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    logger.propagate = False
    logger.handlers.clear()
    logger.addHandler(QueueHandler(queue))

    _CONFIGURED_LOGGERS[logger_name] = logger
    return logger


@atexit.register
def _stop_listeners() -> None:
    for listener in _LISTENERS:
        try:
            listener.stop()
        except Exception:
            pass
    _LISTENERS.clear()
