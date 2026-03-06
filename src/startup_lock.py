import errno
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class StartupLockError(RuntimeError):
    pass


class StartupLock:
    """
    Single-instance process lock using an OS-level file lock.
    """
    STALE_LOCK_MAX_AGE_HOURS = 72

    def __init__(self, lock_path: Path):
        self.lock_path = Path(lock_path)
        self._file: TextIO | None = None

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.lock_path.open("a+", encoding="utf-8")
        self._recover_stale_metadata_if_needed()

        # Windows lock APIs expect at least one byte in the file.
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:
            self._file.write(" ")
            self._file.flush()

        try:
            _lock_file(self._file)
        except OSError as exc:
            owner_info = self._read_owner_info()

            # Rare recovery path: stale metadata may have just been replaced by another process.
            # Retry lock once when metadata looks stale/dead.
            owner_pid = self._safe_int(owner_info.get("pid"))
            if owner_pid is not None and not _is_pid_alive(owner_pid):
                try:
                    _lock_file(self._file)
                    self._write_owner_info(_build_owner_info())
                    return
                except OSError:
                    pass

            self._file.close()
            self._file = None
            pid_text = owner_info.get("pid")
            started_at_text = owner_info.get("started_at")
            pid_part = f" (pid {pid_text})" if pid_text else ""
            started_part = f", started_at={started_at_text}" if started_at_text else ""
            raise StartupLockError(
                f"Another bot instance is already running{pid_part}{started_part}."
            ) from exc

        self._write_owner_info(_build_owner_info())

    def release(self) -> None:
        if self._file is None:
            return
        try:
            _unlock_file(self._file)
        finally:
            self._file.close()
            self._file = None

    def _read_owner_info(self) -> dict[str, str]:
        if self._file is None:
            return {}
        try:
            self._file.seek(0)
            raw = self._file.read().strip()
        except OSError:
            return {}

        if not raw:
            return {}

        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                return {
                    "pid": str(payload.get("pid", "")).strip(),
                    "started_at": str(payload.get("started_at", "")).strip(),
                }
        except json.JSONDecodeError:
            pass

        # Backward compatibility with legacy plain pid lock files.
        if raw.strip().isdigit():
            return {"pid": raw.strip(), "started_at": ""}
        return {}

    def _write_owner_info(self, owner_info: dict[str, Any]) -> None:
        if self._file is None:
            return
        self._file.seek(0)
        self._file.truncate()
        self._file.write(json.dumps(owner_info, ensure_ascii=True))
        self._file.flush()

    def _recover_stale_metadata_if_needed(self) -> None:
        owner_info = self._read_owner_info()
        if not owner_info:
            return

        owner_pid = self._safe_int(owner_info.get("pid"))
        owner_started_at = _parse_iso8601(owner_info.get("started_at", ""))
        max_age = timedelta(hours=self.STALE_LOCK_MAX_AGE_HOURS)
        owner_age_too_old = (
            owner_started_at is not None
            and datetime.now(timezone.utc) - owner_started_at > max_age
        )

        if owner_pid is None:
            return

        if _is_pid_alive(owner_pid):
            return

        # If process is dead, clear stale metadata.
        # OS lock ownership remains authoritative for single-instance safety.
        self._clear_owner_info()

        # Extra cleanup path for very old lock metadata.
        if owner_age_too_old:
            self._clear_owner_info()

    @staticmethod
    def _safe_int(raw: Any) -> int | None:
        try:
            value = int(str(raw).strip())
            if value <= 0:
                return None
            return value
        except (TypeError, ValueError):
            return None

    def _clear_owner_info(self) -> None:
        if self._file is None:
            return
        self._file.seek(0)
        self._file.truncate()
        self._file.write(" ")
        self._file.flush()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


def _lock_file(file_obj: TextIO) -> None:
    file_obj.seek(0)
    if os.name == "nt":
        msvcrt.locking(file_obj.fileno(), msvcrt.LK_NBLCK, 1)
        return
    fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(file_obj: TextIO) -> None:
    file_obj.seek(0)
    if os.name == "nt":
        msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)


def _build_owner_info() -> dict[str, str]:
    return {
        "pid": str(os.getpid()),
        "started_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


def _parse_iso8601(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True

    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # Process exists but current user may not have permission.
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ESRCH:
            return False
        return True
