"""Pure viewer logic — no terminal needed (the Textual app is a thin shell)."""

from __future__ import annotations

from pathlib import Path

import pytest

import flight
from flight import _viewer_model as vm


@pytest.fixture
def crash(tmp_path):
    out = tmp_path / "c.flight"
    flight.install()

    def inner(cfg):
        scale = 2  # noqa: F841
        raise ValueError("boom")

    def outer():
        config = {"mode": "prod", "retries": 3}
        password = "s3cret"  # noqa: F841
        inner(config)

    try:
        outer()
    except ValueError:
        flight.capture(path=out)
    flight.uninstall()
    return flight.read(out).crash()


def _find(crash, qualname):
    for i, fr in enumerate(crash.frames):
        if fr.qualname.endswith(qualname):
            return i
    raise AssertionError(qualname)


def test_frame_locals(crash):
    i = _find(crash, "inner")
    locs = vm.frame_locals(crash, i)
    assert "scale" in locs and locs["scale"][1] == "2"
    assert "cfg" in locs


def test_inline_values_matches_names_on_a_line(crash):
    i = _find(crash, "inner")
    locs = vm.frame_locals(crash, i)
    vals = vm.inline_values("result = scale + cfg", locs)
    names = [n for n, _v in vals]
    assert "scale" in names and "cfg" in names
    # a name not in locals is not annotated
    assert vm.inline_values("nothing_here = 1", locs) == []


def test_alias_index_finds_shared_object_excludes_scalars(crash):
    ci = _find(crash, "inner")
    co = _find(crash, "outer")
    cfg_id = dict(crash.frames[ci].locals)["cfg"]
    config_id = dict(crash.frames[co].locals)["config"]
    assert cfg_id == config_id
    aliases = vm.alias_index(crash)
    assert cfg_id in aliases
    appearances = {name for _i, name in aliases[cfg_id]}
    assert {"cfg", "config"} <= appearances


def test_object_children_and_labels(crash):
    co = _find(crash, "outer")
    config_id = dict(crash.frames[co].locals)["config"]
    assert vm.has_children(crash, config_id)
    kids = dict(vm.object_children(crash, config_id))
    assert "mode" in kids
    label = vm.object_label(crash, config_id, key="config")
    assert label.startswith("config = ")


def test_object_detail_reports_aliasing(crash):
    co = _find(crash, "outer")
    config_id = dict(crash.frames[co].locals)["config"]
    detail = "\n".join(vm.object_detail(crash, config_id))
    assert "kind" in detail
    assert "aliased" in detail  # config is shared with inner's cfg


def test_source_window_centers_on_current_line_with_inline_values(crash):
    i = _find(crash, "inner")
    rows, cur = vm.source_window(crash, i, context=5)
    assert rows, "inner's source should have been captured"
    assert any(n == cur for n, _line, _vals in rows)
    # somewhere in the window, an inline value annotation appears
    assert any(vals for _n, _line, vals in rows)


def test_scrubbed_value_shows_redacted(crash):
    co = _find(crash, "outer")
    locs = vm.frame_locals(crash, co)
    assert locs["password"][1] == "<redacted>"
