"""Phase 7 — crash de-duplication by frame + state (Sentry-style grouping).

Sentry groups errors by their stack. flight can do better: group by the crash's
**frame path and the shape of its state**, so two reports of the same bug collapse
to one fingerprint even when line numbers shift slightly, and two *different* bugs
that happen to share a stack stay apart. The fingerprint is a stable short hash of:
the exception type chain, each frame's `(qualname, file basename, offset within
the function)`, and the *kinds* of the crash frame's locals (the state shape, not
its volatile values). Deterministic and content-only — the same bug hashes the
same across machines and runs.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass


@dataclass
class Signature:
    exceptions: list[str]  # exception type chain, most recent first
    frames: list[tuple]  # (qualname, basename, line-offset-in-function)
    state_kinds: list[str]  # sorted kinds of the crash frame's locals

    def as_dict(self) -> dict:
        return {
            "exceptions": self.exceptions,
            "frames": [list(f) for f in self.frames],
            "state_kinds": self.state_kinds,
        }


def signature(flight_path) -> Signature:
    """The structural components a fingerprint is built from."""
    from ._read import read

    fl = read(flight_path)
    if not fl.has_crash:
        return Signature([], [], [])
    crash = fl.crash()
    excs = [e[0] for e in crash.exceptions]
    frames = []
    for fr in crash.frames:
        offset = max(0, fr.lineno - fr.first_lineno)  # stable under code moving around
        frames.append((fr.qualname, os.path.basename(fr.file), offset))
    kinds: list[str] = []
    if crash.frames:
        for name, oid in crash.frames[0].locals:
            if name.startswith("__") and name.endswith("__"):
                continue
            node = crash.node(oid)
            if node is not None:
                kinds.append(node.get("kind", "?"))
    return Signature(excs, frames, sorted(kinds))


def fingerprint(flight_path) -> str:
    """A stable short hex id grouping crashes that are "the same bug"."""
    sig = signature(flight_path)
    blob = json.dumps(sig.as_dict(), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.blake2b(blob, digest_size=8).hexdigest()
