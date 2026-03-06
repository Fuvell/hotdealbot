import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pytz import timezone

KST = timezone("Asia/Seoul")
ERROR_LOG_MAX_BYTES = 2 * 1024 * 1024
ERROR_LOG_BACKUP_COUNT = 5
AUDIT_LOG_MAX_BYTES = 2 * 1024 * 1024
AUDIT_LOG_BACKUP_COUNT = 5


class KSTFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        kst_datetime = datetime.fromtimestamp(record.created, KST)
        if datefmt:
            return kst_datetime.strftime(datefmt)
        return kst_datetime.strftime("%Y-%m-%d %H:%M:%S")


def get_error_logger(base_dir: Path | None = None) -> logging.Logger:
    return _get_logger_with_rotating_file(
        logger_name="hotdealbot.error",
        filename="error_log.txt",
        level=logging.ERROR,
        max_bytes=ERROR_LOG_MAX_BYTES,
        backup_count=ERROR_LOG_BACKUP_COUNT,
        base_dir=base_dir,
    )


def get_audit_logger(base_dir: Path | None = None) -> logging.Logger:
    return _get_logger_with_rotating_file(
        logger_name="hotdealbot.audit",
        filename="audit_log.txt",
        level=logging.INFO,
        max_bytes=AUDIT_LOG_MAX_BYTES,
        backup_count=AUDIT_LOG_BACKUP_COUNT,
        base_dir=base_dir,
    )


def _get_logger_with_rotating_file(
    *,
    logger_name: str,
    filename: str,
    level: int,
    max_bytes: int,
    backup_count: int,
    base_dir: Path | None = None,
) -> logging.Logger:
    log_dir = base_dir or Path(__file__).resolve().parent.parent
    log_file = (log_dir / filename).resolve()

    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    logger.propagate = False

    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            try:
                if Path(handler.baseFilename).resolve() == log_file:
                    return logger
            except Exception:
                continue

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
    logger.addHandler(file_handler)
    return logger
