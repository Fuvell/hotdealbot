from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

import psutil

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class StartupLockError(RuntimeError):
    pass


class StartupLock:
    """
    Single-instance process lock with takeover semantics.

    If another verified hotdealbot instance holds the lock, it is asked to
    shut down (SIGTERM on POSIX, terminate on Windows) and this process takes
    over. A PID is only ever terminated when both the stored PID and its
    process creation time match, so a recycled PID belonging to an unrelated
    process is never touched.
    """

    OWNER_APP_TAG = "hotdealbot"
    # How closely the live process creation time must match the recorded one.
    PROC_CREATED_AT_TOLERANCE_SECONDS = 2.0
    GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = 10.0
    TAKEOVER_TOTAL_TIMEOUT_SECONDS = 25.0
    LOCK_RETRY_INTERVAL_SECONDS = 0.25

    def __init__(self, lock_path: Path):
        self.lock_path = Path(lock_path)
        self._file: TextIO | None = None

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.lock_path.open("a+", encoding="utf-8")

        # Windows lock APIs expect at least one byte in the file.
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:
            self._file.write(" ")
            self._file.flush()

        deadline = time.monotonic() + self.TAKEOVER_TOTAL_TIMEOUT_SECONDS
        takeover_requested = False
        owner_info: dict[str, str] = {}

        while True:
            try:
                _lock_file(self._file)
                break
            except OSError:
                owner_info = self._read_owner_info()

                if not takeover_requested:
                    takeover_requested = True
                    self._request_owner_shutdown(owner_info)

                if time.monotonic() >= deadline:
                    self._file.close()
                    self._file = None
                    pid_text = owner_info.get("pid", "")
                    pid_part = f" (pid {pid_text})" if pid_text else ""
                    raise StartupLockError(
                        "Could not take over the startup lock"
                        f"{pid_part}. Another instance may still be running "
                        "or the lock owner could not be identified safely."
                    )

                time.sleep(self.LOCK_RETRY_INTERVAL_SECONDS)

        self._write_owner_info(_build_owner_info())

    def release(self) -> None:
        if self._file is None:
            return
        try:
            # Clear metadata while still holding the lock so a stale PID is
            # never left behind for the next startup to reason about.
            self._clear_owner_info()
            _unlock_file(self._file)
        finally:
            self._file.close()
            self._file = None

    def _request_owner_shutdown(self, owner_info: dict[str, str]) -> None:
        """Terminate the current lock owner only if its identity is verified."""
        owner_process = self._find_verified_owner_process(owner_info)
        if owner_process is None:
            return

        try:
            owner_process.terminate()
            owner_process.wait(timeout=self.GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS)
        except psutil.TimeoutExpired:
            try:
                owner_process.kill()
                owner_process.wait(timeout=self.GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS)
            except psutil.Error:
                pass
        except psutil.NoSuchProcess:
            pass
        except psutil.Error:
            pass

    def _find_verified_owner_process(
        self,
        owner_info: dict[str, str],
    ) -> psutil.Process | None:
        """
        Return the owner process only when the stored (pid, creation time)
        pair matches a live process. A bare PID match is never enough:
        Windows recycles PIDs aggressively.
        """
        owner_pid = self._safe_int(owner_info.get("pid"))
        if owner_pid is None or owner_pid == os.getpid():
            return None

        recorded_created_at = self._safe_float(owner_info.get("proc_created_at"))
        if recorded_created_at is None:
            # Legacy or partial metadata: without a creation time we cannot
            # prove identity, so we refuse to kill anything.
            return None

        try:
            process = psutil.Process(owner_pid)
            live_created_at = float(process.create_time())
        except psutil.NoSuchProcess:
            return None
        except psutil.Error:
            return None

        if abs(live_created_at - recorded_created_at) > self.PROC_CREATED_AT_TOLERANCE_SECONDS:
            return None

        return process

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
                    "proc_created_at": str(payload.get("proc_created_at", "")).strip(),
                    "app": str(payload.get("app", "")).strip(),
                }
        except json.JSONDecodeError:
            pass
        return {}

    def _write_owner_info(self, owner_info: dict[str, Any]) -> None:
        if self._file is None:
            return
        self._file.seek(0)
        self._file.truncate()
        self._file.write(json.dumps(owner_info, ensure_ascii=True))
        self._file.flush()

    def _clear_owner_info(self) -> None:
        if self._file is None:
            return
        self._file.seek(0)
        self._file.truncate()
        self._file.write(" ")
        self._file.flush()

    @staticmethod
    def _safe_int(raw: Any) -> int | None:
        try:
            value = int(str(raw).strip())
            if value <= 0:
                return None
            return value
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_float(raw: Any) -> float | None:
        try:
            text = str(raw).strip()
            if not text:
                return None
            return float(text)
        except (TypeError, ValueError):
            return None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


# Windows byte-range locks are MANDATORY: other processes cannot even read a
# locked region. Locking a byte far beyond EOF keeps the metadata at offset 0
# readable by a second instance (it must identify the owner to take over).
_NT_LOCK_REGION_OFFSET = 1 << 30


def _lock_file(file_obj: TextIO) -> None:
    if os.name == "nt":
        file_obj.seek(_NT_LOCK_REGION_OFFSET)
        msvcrt.locking(file_obj.fileno(), msvcrt.LK_NBLCK, 1)
        file_obj.seek(0)
        return
    file_obj.seek(0)
    fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(file_obj: TextIO) -> None:
    if os.name == "nt":
        file_obj.seek(_NT_LOCK_REGION_OFFSET)
        msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)
        file_obj.seek(0)
        return
    file_obj.seek(0)
    fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)


def _build_owner_info() -> dict[str, str]:
    try:
        proc_created_at = str(psutil.Process(os.getpid()).create_time())
    except psutil.Error:
        proc_created_at = ""
    return {
        "app": StartupLock.OWNER_APP_TAG,
        "pid": str(os.getpid()),
        "proc_created_at": proc_created_at,
        "started_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
