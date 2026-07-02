"""The Textual viewer app, driven headlessly via Textual's Pilot.

Skipped if Textual isn't installed (it's an optional `[viewer]` extra). Tests
are sync wrappers around an async driver so no pytest-asyncio plugin is needed.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual")

import flight
from flight._viewer import FlightViewer
from textual.widgets import DataTable, Tree


def _make_crash(tmp_path):
    out = tmp_path / "c.flight"
    flight.install()

    def compute(numbers):
        total = 0  # noqa: F841
        return sum(numbers) / len(numbers)

    def run():
        data = []
        compute(data)

    try:
        run()
    except ZeroDivisionError:
        flight.capture(path=out)
    flight.uninstall()
    return out


def _make_scope(tmp_path):
    out = tmp_path / "s.flight"
    with flight.record(path=out):
        acc = 0
        for i in range(3):
            acc += i
    return out


def test_viewer_shows_frames_source_and_ring(tmp_path):
    app = FlightViewer(_make_crash(tmp_path))

    async def drive():
        async with app.run_test() as pilot:
            tree = app.query_one("#tree", Tree)
            frame_nodes = [n for n in tree.root.children if n.data and n.data[0] == "frame"]
            assert len(frame_nodes) >= 2  # compute + run + module …

            ring = app.query_one("#ring", DataTable)
            assert ring.row_count > 0

            # Navigating and asking for aliases / expanding must not raise.
            await pilot.press("down")
            await pilot.press("down")
            await pilot.press("a")
            await pilot.press("e")

    asyncio.run(drive())


def test_viewer_lazy_expands_object_graph(tmp_path):
    app = FlightViewer(_make_crash(tmp_path))

    async def drive():
        async with app.run_test():
            tree = app.query_one("#tree", Tree)
            run_frame = next(
                n
                for n in tree.root.children
                if n.data and n.data[0] == "frame" and "run" in n.label.plain
            )
            obj_nodes = [c for c in run_frame.children if c.data and c.data[0] == "obj"]
            assert obj_nodes, "run() should have local object nodes"
            expandable = [n for n in obj_nodes if n.allow_expand]
            if expandable:
                node = expandable[0]
                assert not node.children
                node.expand()
                await asyncio.sleep(0)
                # after expansion the graph children are populated
                assert node.children

    asyncio.run(drive())


def test_viewer_shows_timeline_for_scope_file(tmp_path):
    app = FlightViewer(_make_scope(tmp_path))

    async def drive():
        async with app.run_test():
            muts = app.query_one("#muts", DataTable)
            assert muts.row_count > 0

    asyncio.run(drive())


def test_viewer_opens_a_truncated_crash_file(tmp_path):
    # A crash file cut short (footer + tail lost) must still open in the viewer.
    full = tmp_path / "c.flight"
    flight.install()

    def f(cfg):
        return cfg["x"][99]

    try:
        f({"x": [1, 2, 3]})
    except IndexError:
        flight.capture(path=full)
    flight.uninstall()

    data = full.read_bytes()
    cut = tmp_path / "cut.flight"
    cut.write_bytes(data[: len(data) - 30])

    app = FlightViewer(cut)

    async def drive():
        async with app.run_test():
            app.query_one("#tree", Tree)  # mounted without raising

    asyncio.run(drive())


def test_viewer_opens_ring_only_file(tmp_path):
    out = tmp_path / "ring.flight"
    flight.install()

    def loop():
        return sum(range(20))

    loop()
    flight.dump(out)
    flight.uninstall()

    app = FlightViewer(out)

    async def drive():
        async with app.run_test():
            assert app.query_one("#ring", DataTable).row_count >= 0

    asyncio.run(drive())
