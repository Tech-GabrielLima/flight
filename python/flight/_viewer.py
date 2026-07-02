"""The Phase-1.5 TUI viewer — a Textual app over the reader's query surface.

It never touches bytes: everything comes from `flight.read(path)` and its
`Crash` / `Recording` objects (P3). The layout is a frames-and-object-graph
`Tree` on the left and, on the right, tabs for the source (with inline values),
object detail, the exception chain, the event ring, and — for a scope recording
— the mutation timeline.

Rendering-free logic lives in `_viewer_model`, so it is unit-tested without a
terminal; this module is the thin shell that wires it to widgets.
"""

from __future__ import annotations

import os

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane, Tree

from . import _viewer_model as vm
from ._read import read


class FlightViewer(App):
    """Navigate a `.flight` file: frames → locals → object graph, with source,
    aliasing, the event ring and the mutation timeline."""

    CSS = """
    Tree { width: 45%; border-right: solid $panel; }
    #src, #detail, #exc { padding: 0 1; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("a", "aliases", "Aliases"),
        Binding("e", "expand_all", "Expand frame"),
    ]

    def __init__(self, path):
        super().__init__()
        self.path = str(path)
        self.flight = read(self.path)
        self.crash = self.flight.crash() if self.flight.has_crash else None
        self.recording = self.flight.recording() if self.flight.has_mutations else None
        self.aliases = vm.alias_index(self.crash) if self.crash else {}
        self.title = "flight"
        self.sub_title = os.path.basename(self.path)

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            yield Tree("flight", id="tree")
            with TabbedContent(id="tabs"):
                if self.crash:
                    with TabPane("Source", id="tab-source"):
                        with VerticalScroll():
                            yield Static(id="src", expand=True)
                    with TabPane("Detail", id="tab-detail"):
                        yield Static(id="detail")
                    with TabPane("Exception", id="tab-exc"):
                        yield Static(id="exc")
                with TabPane("Events", id="tab-events"):
                    yield DataTable(id="ring", zebra_stripes=True)
                if self.recording is not None:
                    with TabPane("Timeline", id="tab-timeline"):
                        yield DataTable(id="muts", zebra_stripes=True)
        yield Footer()

    # -- population --------------------------------------------------------

    def on_mount(self) -> None:
        tree = self.query_one("#tree", Tree)
        tree.show_root = False
        if self.crash and self.crash.frames:
            for i, fr in enumerate(self.crash.frames):
                label = f"#{i} {fr.qualname} · {os.path.basename(fr.file)}:{fr.lineno}"
                fnode = tree.root.add(label, data=("frame", i), expand=(i == 0))
                for name, oid in fr.locals:
                    fnode.add(
                        self._label(oid, name),
                        data=("obj", oid),
                        allow_expand=vm.has_children(self.crash, oid),
                    )
            self._show_source(0)
            self._show_exception()
        else:
            tree.root.add_leaf("(no crash frames in this file)")

        self._build_ring()
        if self.recording is not None:
            self._build_timeline()

    def _label(self, oid: int, key) -> str:
        label = vm.object_label(self.crash, oid, key)
        return f"{label}  ↔" if oid in self.aliases else label

    def _build_ring(self) -> None:
        table = self.query_one("#ring", DataTable)
        table.add_columns("event", "where")
        for kind, file, qual, line in self.flight.events(limit=500):
            loc = f"{os.path.basename(file)}:{line}" if file else "?"
            where = f"{qual}  ({loc})" if qual else loc
            table.add_row(kind, where)

    def _build_timeline(self) -> None:
        table = self.query_one("#muts", DataTable)
        table.add_columns("#", "line", "kind", "target", "value")
        for m in self.recording.mutations:
            target = m.name if m.kind == "local" else f"{m.name}[{m.key}]"
            table.add_row(str(m.seq), str(m.line), m.kind, target, m.value_repr)

    def _show_exception(self) -> None:
        if not self.crash or not self.crash.exceptions:
            return
        txt = Text()
        for i, (exc_type, message, relation) in enumerate(self.crash.exceptions):
            if i:
                txt.append(f"\n  ({relation} of the above)\n", style="dim italic")
            txt.append(exc_type, style="bold red")
            txt.append(f": {message}\n")
        self.query_one("#exc", Static).update(txt)

    def _show_source(self, frame_index: int) -> None:
        fr = self.crash.frames[frame_index]
        rows, cur = vm.source_window(self.crash, frame_index, context=8)
        txt = Text()
        txt.append(f"{fr.qualname}\n", style="bold")
        txt.append(f"{fr.file}:{cur}\n\n", style="dim")
        if not rows:
            txt.append("(source not captured for this file)\n", style="dim italic")
        for n, line, vals in rows:
            is_cur = n == cur
            txt.append(f"{'▶' if is_cur else ' '}{n:>5} ", style="bold yellow" if is_cur else "dim")
            txt.append(line + "\n", style="bold white" if is_cur else "")
            if vals:
                ann = "        ‹ " + "   ".join(f"{k} = {v}" for k, v in vals) + " ›\n"
                txt.append(ann, style="italic cyan")
        self.query_one("#src", Static).update(txt)

    def _show_detail(self, oid: int) -> None:
        self.query_one("#detail", Static).update("\n".join(vm.object_detail(self.crash, oid)))

    # -- interaction -------------------------------------------------------

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        node = event.node
        data = node.data
        if data and data[0] == "obj" and not node.children:
            oid = data[1]
            for key, child in vm.object_children(self.crash, oid):
                k = str(key) if key is not None else None
                node.add(
                    self._label(child, k),
                    data=("obj", child),
                    allow_expand=vm.has_children(self.crash, child),
                )

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        data = event.node.data
        if not data:
            return
        if data[0] == "frame":
            self._show_source(data[1])
        elif data[0] == "obj":
            self._show_detail(data[1])

    def action_aliases(self) -> None:
        tree = self.query_one("#tree", Tree)
        node = tree.cursor_node
        if node is None or not node.data or node.data[0] != "obj":
            self.notify("select an object in the tree first")
            return
        apps = self.aliases.get(node.data[1])
        if apps:
            where = ", ".join(f"#{i} as {name}" for i, name in apps)
            self.notify(f"same object also in: {where}", title="aliases")
        else:
            self.notify("this object appears in only one place")

    def action_expand_all(self) -> None:
        tree = self.query_one("#tree", Tree)
        node = tree.cursor_node
        if node is not None:
            node.expand_all()


def run(path) -> None:
    """Launch the viewer on a `.flight` file."""
    FlightViewer(path).run()
