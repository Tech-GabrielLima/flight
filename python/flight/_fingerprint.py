from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass


@dataclass
class Signature:
    exceptions: list[str]
    frames: list[tuple]
    state_kinds: list[str]

    def as_dict(self) -> dict:
        return {
            "exceptions": self.exceptions,
            "frames": [list(f) for f in self.frames],
            "state_kinds": self.state_kinds,
        }


def signature(flight_path) -> Signature:
    from ._read import read

    fl = read(flight_path)
    if not fl.has_crash:
        return Signature([], [], [])
    crash = fl.crash()
    excs = [e[0] for e in crash.exceptions]
    frames = []
    for fr in crash.frames:
        offset = max(0, fr.lineno - fr.first_lineno)
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
    sig = signature(flight_path)
    blob = json.dumps(sig.as_dict(), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.blake2b(blob, digest_size=8).hexdigest()
