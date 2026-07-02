"""Phase 3 — deterministic replay and self-reproducing bugs.

    python examples/replay.py

Shows two things:
  1. `flight.deterministic()` records the non-determinism of a block so
     `flight.replay()` re-runs it bit-for-bit — even though time and randomness
     would normally differ.
  2. A flaky, random-dependent crash is captured to a `.flight` that carries
     BOTH the crash frames and the recorded randomness, so `flight repro` on it
     generates a script that reproduces the bug *deterministically*.
"""

import random
import time

import flight


def work():
    return {"t": round(time.time(), 6), "r": random.random(), "n": random.randint(1, 1_000_000)}


def flaky(items):
    # off-by-one: randint can return len(items) -> IndexError, but only sometimes
    return items[random.randint(0, len(items))]


def main():
    # 1. deterministic replay
    with flight.deterministic("run.flight"):
        original = work()
    replayed = flight.replay("run.flight", work)
    print("original :", original)
    print("replayed :", replayed)
    print("identical:", original == replayed)

    # 2. capture a flaky crash (frames + the exact randomness) for repro
    print("\nrecording a flaky crash to crash.flight …")
    with flight.deterministic("crash.flight"):
        data = [10, 20, 30]
        for _ in range(500):
            flaky(data)  # eventually indexes out of range


if __name__ == "__main__":
    main()
