"""Phase 2 — time-travel of scope: record every state write in a block.

Run it:

    python examples/timetravel.py

Then explore the recording's timeline:

    python -m flight timeline flight-scope-*.flight
    python -m flight timeline --var running flight-scope-*.flight
    python -m flight timeline --who cache   flight-scope-*.flight
"""

import flight


def accumulate(items):
    cache = {}
    with flight.record() as rec:
        rec.watch(cache, name="cache")  # also track writes into this dict
        running = 0
        for it in items:
            running = running + it
            cache[it] = running
    return cache


if __name__ == "__main__":
    accumulate([5, 3, 8])
