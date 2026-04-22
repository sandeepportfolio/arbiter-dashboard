"""Process-level runtime lock helpers for ARBITER."""
from __future__ import annotations

import os
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


class RuntimeLockError(RuntimeError):
    """Raised when another ARBITER process already owns the runtime lock."""


class RuntimeLock:
    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> "RuntimeLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def acquire(self) -> None:
        if fcntl is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            owner = self._read_owner(fd)
            os.close(fd)
            detail = f" by pid {owner}" if owner else ""
            raise RuntimeLockError(
                f"Another ARBITER instance already holds {self.path}{detail}"
            ) from exc

        self._fd = fd
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        os.fsync(fd)

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _read_owner(fd: int) -> str:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 64).decode("utf-8", errors="ignore").strip()
            return raw
        except OSError:
            return ""


def default_lock_path(*, api_only: bool, port: int) -> Path:
    explicit = os.getenv("ARBITER_RUNTIME_LOCK_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if api_only:
        return Path(f"/tmp/arbiter-api-{port}.lock")
    return Path("/tmp/arbiter-engine.lock")


def acquire_runtime_lock(*, api_only: bool, port: int) -> RuntimeLock:
    return RuntimeLock(default_lock_path(api_only=api_only, port=port))
