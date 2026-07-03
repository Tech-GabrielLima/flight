"""Always-on black box that survives the death of the plane (Phase 8).

An uncaught Python exception is the *easy* case: the interpreter is still alive,
so `flight`'s excepthook writes a full crash file. The hard cases are the ones
that kill a production process without giving it a chance to run any Python at
all — ``SIGKILL`` from an orchestrator, the OOM killer, a segfault in a C
extension, ``kill -9``. There is no hook for those. A black box that only works
when the program shuts down politely is not a black box.

So Flight keeps flying the recorder even through an uncatchable death, with two
cooperating parts:

* **A checkpoint writer** in the process: every ``interval`` seconds it dumps
  the current ring to a checkpoint file, written atomically (temp + rename) so a
  reader never sees a half-written file. On a sudden death the last checkpoint
  is already safely on disk — at most ``interval`` stale.

* **A supervisor subprocess** that outlives its parent. Parent and supervisor
  share a pipe; the supervisor blocks reading it. On a *clean* shutdown the
  parent sends one byte and the supervisor discards the checkpoint. On any death
  that does **not** send that byte — SIGKILL, OOM, segfault — the OS closes the
  pipe, the supervisor's read returns EOF, and it promotes the last checkpoint
  into a final ``flight-killed-*.flight``. The black box survives the crash that
  killed the recorder itself.

Honest scope (documented, not hidden): the checkpoint is periodic, so up to
``interval`` of the very last events can be lost in a hard kill; and this is a
supervisor-over-a-checkpoint design, not yet a shared-memory ring the supervisor
reads live (that is the future refinement noted in the roadmap). What it does
deliver, testably, is a real ``.flight`` after a real ``kill -9``.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

#: Single byte the parent sends to announce a clean, intentional shutdown.
_CLEAN = b"C"


# -- supervisor side (runs in the child process) ---------------------------


def _promote(checkpoint: Path, final: Path) -> Optional[Path]:
    """Turn the last checkpoint into the final black box. Returns the path
    written, or ``None`` if there was no checkpoint to promote."""
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
    """Block until the parent goes away; promote the checkpoint iff it died
    without announcing a clean shutdown. Returns the promoted path or ``None``.

    The pipe is the death detector: a clean shutdown writes ``_CLEAN`` first, and
    *any* death (including SIGKILL/OOM, which run no Python) closes the write end
    and gives us EOF. We read until EOF and then decide.
    """
    clean = False
    try:
        while True:
            try:
                chunk = os.read(read_fd, 64)
            except (OSError, InterruptedError):
                break
            if not chunk:  # EOF — the parent's write end is gone.
                break
            if _CLEAN in chunk:
                clean = True
                # Keep draining until EOF so we know the parent really left.
    finally:
        try:
            os.close(read_fd)
        except Exception:
            pass

    if clean:
        # Intentional shutdown: the in-process paths own the black box; drop
        # the checkpoint so we don't leave a stale duplicate behind.
        try:
            Path(checkpoint).unlink(missing_ok=True)
        except Exception:
            pass
        return None
    return _promote(Path(checkpoint), _final_path(Path(output_dir), parent_pid))


def _main(argv: list[str]) -> int:  # pragma: no cover - exercised via subprocess
    # argv: parent_pid checkpoint output_dir
    if len(argv) < 3:
        return 2
    parent_pid = int(argv[0])
    checkpoint = Path(argv[1])
    output_dir = Path(argv[2])
    # The control pipe is handed to us as stdin.
    read_fd = 0
    supervise(read_fd, checkpoint, output_dir, parent_pid)
    return 0


# -- parent side (runs in the recorded process) ----------------------------


class Daemon:
    """Owns the checkpoint-writer thread and the supervisor subprocess."""

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

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> "Daemon":
        if self._started:
            return self
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # Write a first checkpoint immediately so a death in the first interval
        # still yields a black box.
        self._write_checkpoint()
        self._spawn_supervisor()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="flight-daemon", daemon=True)
        self._thread.start()
        self._started = True
        # A normal interpreter shutdown (falling off the end of a script, an
        # uncaught exception, sys.exit) runs atexit — so we can announce the
        # clean shutdown even if the caller never calls uninstall(). A death
        # that runs no Python (SIGKILL/OOM/segfault) skips atexit, which is
        # exactly when the supervisor should promote the checkpoint.
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
            # The child owns the read end now; we keep only the write end.
            os.close(read_fd)
        self._write_fd = write_fd

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self._write_checkpoint()

    def _write_checkpoint(self) -> None:
        # Atomic: write to a temp sibling then rename over the checkpoint, so the
        # supervisor (and any reader) only ever sees a complete file.
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
        """Stop the daemon. ``clean=True`` (the default) tells the supervisor
        this was an intentional shutdown, so it discards the checkpoint."""
        if not self._started:
            return
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=self.interval * 4)
        self._thread = None
        # Announce the clean shutdown, then close the pipe so the supervisor's
        # read returns EOF and it exits.
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
        # Give the supervisor a moment to finish, but never block shutdown.
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main(sys.argv[1:]))
