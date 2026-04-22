from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from arbiter.runtime_lock import RuntimeLock, RuntimeLockError


def test_runtime_lock_rejects_second_process(tmp_path: Path):
    lock_path = tmp_path / "arbiter.lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys, time; "
                "from pathlib import Path; "
                "from arbiter.runtime_lock import RuntimeLock; "
                "lock = RuntimeLock(Path(sys.argv[1])); "
                "lock.acquire(); "
                "print('locked', flush=True); "
                "time.sleep(10)"
            ),
            str(lock_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"

        contender = RuntimeLock(lock_path)
        with pytest.raises(RuntimeLockError):
            contender.acquire()
    finally:
        holder.terminate()
        holder.wait(timeout=5)
