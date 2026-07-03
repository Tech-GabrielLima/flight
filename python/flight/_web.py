"""Web middleware — a `.flight` per HTTP 500 (Phase 9).

The unhandled exception that returns a 500 is exactly the case a black box is
for, and a web server is exactly where you can't reproduce it: the request is
gone, the session is gone, the load is gone. This middleware records every
request under Flight and, when one raises, writes a full-detail `.flight` for it
— frames, locals, object graph, the request handler's state — before the
exception propagates to the framework's error handling.

It is **framework-agnostic** because it speaks the two protocols every Python
web stack is built on, not any one framework's API:

* :class:`FlightWSGI` wraps a **WSGI** app (Flask, Django, Pyramid, Bottle…).
* :class:`FlightASGI` wraps an **ASGI** app (FastAPI, Starlette, Quart,
  Django-async…).

If the incoming request carries a W3C ``traceparent`` (from an upstream service
or a tracing sidecar), the black box is stamped with that trace context, so a
500 in this service links straight back to the caller's flight (Phase 8's
cross-service graph). The trace context is passed *into* the capture rather than
set globally, so concurrent requests never clobber each other's correlation.

Obeys P1: a failure inside the middleware never changes the response — the
worst case is simply "no black box for this request", never a second error.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

_MAX_PATH_IN_NAME = 60


def _install_if_needed(output_dir: Path, install: bool):
    import flight

    if install and not flight.is_installed():
        flight.install(output_dir=output_dir)


def _safe_path_component(path: str) -> str:
    keep = [c if (c.isalnum() or c in "-._") else "_" for c in path]
    s = "".join(keep).strip("_") or "root"
    return s[:_MAX_PATH_IN_NAME]


def _flight_name(method: str, path: str) -> str:
    return f"http-{method.upper()}-{_safe_path_component(path)}-{int(time.time() * 1000)}.flight"


def _context_from_traceparent(traceparent: Optional[str], service: Optional[str]):
    if not traceparent:
        return None
    from ._correlation import TraceContext

    return TraceContext.parse(traceparent, service=service)


def _capture_request(
    output_dir: Path, method: str, path: str, traceparent: Optional[str], service: Optional[str]
) -> Optional[Path]:
    """Capture the exception currently being handled, tagged with the request's
    trace context. Best-effort — returns the path or ``None`` (never raises)."""
    try:
        import flight
        from ._capture import capture as _capture
        from ._config import Config
        from ._install import _active

        config = _active.config if _active is not None else Config()
        dest = output_dir / _flight_name(method, path)
        ctx = _context_from_traceparent(traceparent, service)
        return _capture(config, str(dest), correlation=ctx)
    except Exception:
        return None


# -- WSGI -------------------------------------------------------------------


class FlightWSGI:
    """WSGI middleware writing a `.flight` for any request that raises.

    Wrap your app once::

        app = FlightWSGI(app, output_dir="artifacts/flight", service="checkout")
    """

    def __init__(self, app, *, output_dir="./.flight", service: Optional[str] = None, install: bool = True):
        self.app = app
        self.output_dir = Path(output_dir)
        self.service = service
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        _install_if_needed(self.output_dir, install)

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/") or "/"
        traceparent = environ.get("HTTP_TRACEPARENT")

        def _capture():
            return _capture_request(self.output_dir, method, path, traceparent, self.service)

        try:
            result = self.app(environ, start_response)
        except Exception:
            _capture()
            raise
        # The body may also raise while being *iterated* (streaming views,
        # generators). Wrap it so those errors get a black box too.
        return _IterGuard(result, _capture)


class _IterGuard:
    """Wrap a WSGI response iterable, capturing on an error during iteration and
    forwarding ``close()`` so the server's resource handling is preserved."""

    def __init__(self, inner, on_error):
        self._inner = iter(inner)
        self._raw = inner
        self._on_error = on_error

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self._inner)
        except StopIteration:
            raise
        except Exception:
            self._on_error()
            raise

    def close(self):
        closer = getattr(self._raw, "close", None)
        if closer is not None:
            closer()


# -- ASGI -------------------------------------------------------------------


class FlightASGI:
    """ASGI middleware writing a `.flight` for any HTTP request that raises.

    Wrap your app once::

        app = FlightASGI(app, output_dir="artifacts/flight", service="checkout")
    """

    def __init__(self, app, *, output_dir="./.flight", service: Optional[str] = None, install: bool = True):
        self.app = app
        self.output_dir = Path(output_dir)
        self.service = service
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        _install_if_needed(self.output_dir, install)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            # Lifespan, websocket, etc. — pass through untouched.
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        path = scope.get("path", "/") or "/"
        traceparent = _asgi_header(scope, b"traceparent")
        try:
            await self.app(scope, receive, send)
        except Exception:
            _capture_request(self.output_dir, method, path, traceparent, self.service)
            raise


def _asgi_header(scope, name: bytes) -> Optional[str]:
    for key, value in scope.get("headers", []) or []:
        if key.lower() == name:
            try:
                return value.decode("latin-1")
            except Exception:
                return None
    return None
