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
    output_dir: Path, method: str, path: str, traceparent: Optional[str], service: Optional[str],
    report_to_url: Optional[str] = None,
) -> Optional[Path]:
    try:
        import flight
        from ._capture import capture as _capture
        from ._config import Config
        from ._install import _active

        config = _active.config if _active is not None else Config()
        dest = output_dir / _flight_name(method, path)
        ctx = _context_from_traceparent(traceparent, service)
        written = _capture(config, str(dest), correlation=ctx)
        if written is not None and report_to_url:
            from ._fleet import report_to

            report_to(report_to_url, written, strict=True)
        return written
    except Exception:
        return None


class FlightWSGI:

    def __init__(self, app, *, output_dir="./.flight", service: Optional[str] = None,
                 install: bool = True, report_to: Optional[str] = None):
        self.app = app
        self.output_dir = Path(output_dir)
        self.service = service
        self.report_to = report_to
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
            return _capture_request(
                self.output_dir, method, path, traceparent, self.service, self.report_to
            )

        try:
            result = self.app(environ, start_response)
        except Exception:
            _capture()
            raise
        return _IterGuard(result, _capture)


class _IterGuard:

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


class FlightASGI:

    def __init__(self, app, *, output_dir="./.flight", service: Optional[str] = None,
                 install: bool = True, report_to: Optional[str] = None):
        self.app = app
        self.output_dir = Path(output_dir)
        self.service = service
        self.report_to = report_to
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        _install_if_needed(self.output_dir, install)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        path = scope.get("path", "/") or "/"
        traceparent = _asgi_header(scope, b"traceparent")
        try:
            await self.app(scope, receive, send)
        except Exception:
            _capture_request(
                self.output_dir, method, path, traceparent, self.service, self.report_to
            )
            raise


def _asgi_header(scope, name: bytes) -> Optional[str]:
    for key, value in scope.get("headers", []) or []:
        if key.lower() == name:
            try:
                return value.decode("latin-1")
            except Exception:
                return None
    return None
