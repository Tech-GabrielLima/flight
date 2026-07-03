"""Distributed correlation (Phase 8, VISION.md §5.6).

In a service mesh a single user action fans out across many processes, and a
crash in service B is only half the story if you can't get back to the request
in service A that caused it. Flight solves this the same way distributed tracing
does — with the **W3C Trace Context** (`traceparent` / `tracestate`) that most
frameworks and OpenTelemetry already propagate.

When a black box is written we stamp the current trace context onto it, plus any
explicit **links** to upstream `.flight` files. Both are carried on the NONDET
tape (arbitrary `(seq, source, tag, payload)` string tuples the format already
round-trips) so nothing in the Rust format or writer had to change — Phase 8 is
pure Python, exactly like Phases 4–7.

Reading them back groups a directory of black boxes by `trace_id` into a
**cross-service crash graph**: the `.flight` of service A references the one of
service B, and `flight trace` walks the chain.

Everything is best-effort and never raises into a dying program (P1): a
malformed `traceparent`, a missing OpenTelemetry, an unreadable file — all
degrade to "no correlation", never to a second exception.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

# NONDET sources used to carry the trace context. Chosen to be inert to the
# replay engine (they are read by name, never fed back as recorded values).
_SRC_TRACEPARENT = "otel.traceparent"
_SRC_TRACESTATE = "otel.tracestate"
_SRC_SERVICE = "otel.service"
_SRC_LINK = "flight.link"

_HEX32 = re.compile(r"\A[0-9a-f]{32}\Z")
_HEX16 = re.compile(r"\A[0-9a-f]{16}\Z")
_ZERO32 = "0" * 32
_ZERO16 = "0" * 16


@dataclass(frozen=True)
class Link:
    """A reference from this black box to another one (usually upstream)."""

    #: The trace id shared with the referenced flight, if known (32 hex).
    trace_id: str
    #: A pointer to the other black box — a `.flight` path, a span id, or a URL.
    ref: str
    #: The service that produced the referenced flight, if known.
    service: Optional[str] = None

    def render(self) -> str:
        who = f" [{self.service}]" if self.service else ""
        return f"{self.ref}{who}"


@dataclass(frozen=True)
class TraceContext:
    """A parsed W3C Trace Context plus the local service identity and links.

    This is what ties a `.flight` into a distributed trace: the ``trace_id`` is
    shared by every service that handled the same request, so grouping black
    boxes by it reconstructs the cross-service story.
    """

    trace_id: str
    span_id: str
    flags: int = 1  # bit 0 = sampled
    trace_state: str = ""
    service: Optional[str] = None
    links: tuple[Link, ...] = field(default_factory=tuple)

    # -- construction -------------------------------------------------------

    @classmethod
    def parse(
        cls,
        traceparent: str,
        *,
        trace_state: str = "",
        service: Optional[str] = None,
        links: tuple[Link, ...] = (),
    ) -> Optional["TraceContext"]:
        """Parse a `traceparent` header. Returns ``None`` if it is malformed."""
        if not traceparent:
            return None
        parts = traceparent.strip().split("-")
        if len(parts) != 4:
            return None
        version, trace_id, span_id, flags = parts
        trace_id = trace_id.lower()
        span_id = span_id.lower()
        if not (_HEX32.match(trace_id) and _HEX16.match(span_id)):
            return None
        if trace_id == _ZERO32 or span_id == _ZERO16:
            return None
        if len(version) != 2 or not re.match(r"\A[0-9a-f]{2}\Z", version.lower()):
            return None
        try:
            flag_int = int(flags, 16)
        except ValueError:
            return None
        return cls(
            trace_id=trace_id,
            span_id=span_id,
            flags=flag_int & 0xFF,
            trace_state=trace_state or "",
            service=service,
            links=tuple(links),
        )

    @classmethod
    def from_env(cls, environ=None) -> Optional["TraceContext"]:
        """Read a trace context propagated through the environment.

        Honours the same variables a shell-level tracer would set:
        ``TRACEPARENT`` / ``TRACESTATE`` (W3C) and ``OTEL_SERVICE_NAME``.
        """
        env = os.environ if environ is None else environ
        tp = env.get("TRACEPARENT")
        if not tp:
            return None
        return cls.parse(
            tp,
            trace_state=env.get("TRACESTATE", ""),
            service=env.get("OTEL_SERVICE_NAME") or env.get("FLIGHT_SERVICE"),
        )

    @classmethod
    def from_otel(cls, service: Optional[str] = None) -> Optional["TraceContext"]:
        """Read the *live* OpenTelemetry span context, if the SDK is installed
        and a span is active. OpenTelemetry is an optional dependency — its
        absence simply yields ``None``."""
        try:  # pragma: no cover - exercised only where opentelemetry is present
            from opentelemetry import trace as _ot

            span = _ot.get_current_span()
            ctx = span.get_span_context()
            if ctx is None or not ctx.is_valid:
                return None
            trace_id = format(ctx.trace_id, "032x")
            span_id = format(ctx.span_id, "016x")
            flags = int(getattr(ctx, "trace_flags", 1))
            state = ""
            try:
                state = ctx.trace_state.to_header()
            except Exception:
                state = ""
            return cls(
                trace_id=trace_id,
                span_id=span_id,
                flags=flags & 0xFF,
                trace_state=state,
                service=service or os.environ.get("OTEL_SERVICE_NAME"),
            )
        except Exception:
            return None

    @classmethod
    def new_root(cls, service: Optional[str] = None) -> "TraceContext":
        """Mint a brand-new root context (no upstream). Useful when Flight is
        the first thing to correlate a request that had no inbound trace."""
        return cls(
            trace_id=os.urandom(16).hex(),
            span_id=os.urandom(8).hex(),
            flags=1,
            service=service or os.environ.get("OTEL_SERVICE_NAME") or os.environ.get("FLIGHT_SERVICE"),
        )

    # -- mutation (returns a new value; the type is frozen) -----------------

    def with_link(self, link: Link) -> "TraceContext":
        return TraceContext(
            trace_id=self.trace_id,
            span_id=self.span_id,
            flags=self.flags,
            trace_state=self.trace_state,
            service=self.service,
            links=self.links + (link,),
        )

    def with_service(self, service: str) -> "TraceContext":
        return TraceContext(
            trace_id=self.trace_id,
            span_id=self.span_id,
            flags=self.flags,
            trace_state=self.trace_state,
            service=service,
            links=self.links,
        )

    # -- rendering ----------------------------------------------------------

    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.flags:02x}"

    @property
    def sampled(self) -> bool:
        return bool(self.flags & 0x01)

    # -- (de)serialisation onto the NONDET tape -----------------------------

    def to_nondet(self) -> list[tuple[int, str, str, str]]:
        """Encode this context as NONDET tape entries for a `.flight`."""
        out: list[tuple[int, str, str, str]] = [(0, _SRC_TRACEPARENT, "w", self.traceparent())]
        if self.trace_state:
            out.append((1, _SRC_TRACESTATE, "w", self.trace_state))
        if self.service:
            out.append((2, _SRC_SERVICE, "w", self.service))
        for i, link in enumerate(self.links):
            payload = "\t".join((link.trace_id, link.ref, link.service or ""))
            out.append((10 + i, _SRC_LINK, "w", payload))
        return out

    @classmethod
    def from_nondet(cls, rows) -> Optional["TraceContext"]:
        """Rebuild a context from NONDET rows `(seq, source, tag, payload)`.
        Returns ``None`` if no trace context is present."""
        traceparent = None
        trace_state = ""
        service = None
        links: list[Link] = []
        for row in rows:
            try:
                _seq, source, _tag, payload = row[0], row[1], row[2], row[3]
            except Exception:
                continue
            if source == _SRC_TRACEPARENT:
                traceparent = payload
            elif source == _SRC_TRACESTATE:
                trace_state = payload
            elif source == _SRC_SERVICE:
                service = payload
            elif source == _SRC_LINK:
                bits = payload.split("\t")
                tid = bits[0] if len(bits) > 0 else ""
                ref = bits[1] if len(bits) > 1 else ""
                svc = bits[2] if len(bits) > 2 and bits[2] else None
                links.append(Link(trace_id=tid, ref=ref, service=svc))
        if traceparent is None:
            return None
        ctx = cls.parse(traceparent, trace_state=trace_state, service=service, links=tuple(links))
        return ctx


def resolve(
    *,
    traceparent: Optional[str] = None,
    service: Optional[str] = None,
    trace_state: str = "",
    from_env: bool = True,
    from_otel: bool = False,
) -> Optional[TraceContext]:
    """Work out the trace context to stamp on this process's black boxes.

    Precedence: an explicit ``traceparent`` argument, then a live OpenTelemetry
    span (if ``from_otel``), then the environment (if ``from_env``). Returns
    ``None`` when nothing is available — the caller may choose to mint a root.
    """
    if traceparent:
        ctx = TraceContext.parse(traceparent, trace_state=trace_state, service=service)
        if ctx is not None:
            return ctx
    if from_otel:
        ctx = TraceContext.from_otel(service=service)
        if ctx is not None:
            return ctx
    if from_env:
        ctx = TraceContext.from_env()
        if ctx is not None:
            return ctx.with_service(service) if service else ctx
    return None


# -- cross-service graph over a set of black boxes -------------------------


@dataclass
class TraceNode:
    """One black box inside a distributed trace."""

    path: str
    context: TraceContext

    @property
    def service(self) -> str:
        return self.context.service or "?"


def trace_graph(flights) -> dict[str, list[TraceNode]]:
    """Group parsed `Flight`s by ``trace_id`` → the cross-service crash graph.

    `flights` is an iterable of objects exposing ``.path`` and a
    ``.correlation()`` returning a :class:`TraceContext` (i.e. read `.flight`
    summaries). Black boxes with no trace context are skipped.
    """
    groups: dict[str, list[TraceNode]] = {}
    for f in flights:
        try:
            ctx = f.correlation()
        except Exception:
            ctx = None
        if ctx is None:
            continue
        groups.setdefault(ctx.trace_id, []).append(TraceNode(path=str(f.path), context=ctx))
    return groups
