from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

_CLEAN = b"C"


def _promote(checkpoint: Path, final: Path) -> Optional[Path]:
    try:
        if not checkpoint.exists():
            return None
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(checkpoint), str(final))
        return final
    except Exception:
        return None


def _final_path(output_dir: Path, pid: int) -> Path:
    return output_dir / f"flight-killed-{pid}-{int(time.time() * 1000)}.flight"


def supervise(read_fd: int, checkpoint: Path, output_dir: Path, parent_pid: int) -> Optional[Path]:
    clean = False
    try:
        while True:
            try:
                chunk = os.read(read_fd, 64)
            except (OSError, InterruptedError):
                break
            if not chunk:
                break
            if _CLEAN in chunk:
                clean = True
    finally:
        try:
            os.close(read_fd)
        except Exception:
            pass

    if clean:
        try:
            Path(checkpoint).unlink(missing_ok=True)
        except Exception:
            pass
        return None
    return _promote(Path(checkpoint), _final_path(Path(output_dir), parent_pid))


def _main(argv: list[str]) -> int:
    if len(argv) < 3:
        return 2
    parent_pid = int(argv[0])
    checkpoint = Path(argv[1])
    output_dir = Path(argv[2])
    read_fd = 0
    supervise(read_fd, checkpoint, output_dir, parent_pid)
    return 0


class Daemon:

    def __init__(self, config, *, interval: float = 1.0, checkpoint: Optional[Path] = None):
        self.config = config
        self.interval = interval
        pid = os.getpid()
        self.output_dir = Path(config.output_dir)
        self.checkpoint = Path(checkpoint) if checkpoint else self.output_dir / f".flight-ckpt-{pid}.flight"
        self._write_fd: Optional[int] = None
        self._proc = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._started = False


    def start(self) -> "Daemon":
        if self._started:
            return self
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._write_checkpoint()
        self._spawn_supervisor()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="flight-daemon", daemon=True)
        self._thread.start()
        self._started = True
        import atexit

        atexit.register(self._atexit)
        return self

    def _atexit(self) -> None:
        try:
            self.stop(clean=True)
        except Exception:
            pass

    def _spawn_supervisor(self) -> None:
        import subprocess

        read_fd, write_fd = os.pipe()
        os.set_inheritable(read_fd, True)
        try:
            self._proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "flight._daemon",
                    str(os.getpid()),
                    str(self.checkpoint),
                    str(self.output_dir),
                ],
                stdin=read_fd,
                close_fds=True,
            )
        finally:
            os.close(read_fd)
        self._write_fd = write_fd

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self._write_checkpoint()

    def _write_checkpoint(self) -> None:
        tmp = self.checkpoint.with_suffix(self.checkpoint.suffix + ".tmp")
        try:
            from ._install import _write_ring_dump

            ok = _write_ring_dump(tmp, self.config)
            if ok:
                os.replace(str(tmp), str(self.checkpoint))
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def stop(self, *, clean: bool = True) -> None:
        if not self._started:
            return
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=self.interval * 4)
        self._thread = None
        if self._write_fd is not None:
            try:
                if clean:
                    os.write(self._write_fd, _CLEAN)
            except Exception:
                pass
            try:
                os.close(self._write_fd)
            except Exception:
                pass
            self._write_fd = None
        proc = self._proc
        if proc is not None:
            try:
                proc.wait(timeout=2.0)
            except Exception:
                pass
        self._proc = None
        self._started = False
        try:
            import atexit

            atexit.unregister(self._atexit)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
