"""Type adapters — describing big/opaque objects without dumping their guts.

A DataFrame or an ndarray must never be serialized whole: we want shape, dtype,
a small preview and light stats, not the data (VISION.md §9.5). Adapters are
resolved by the fully-qualified type name (``module.qualname``) so numpy and
pandas are *not* dependencies — the adapter only runs if the user has the type.

An adapter takes the object and returns an :class:`Adapted`:
    kind    — a short discriminator the viewer renders on (e.g. "ndarray")
    summary — a one-line human rendering
    fields  — an ordered mapping of label -> small scalar value, shown as children
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class Adapted:
    kind: str
    summary: str
    fields: dict[str, Any] = field(default_factory=dict)


#: Registry: fully-qualified type name -> adapter function.
_REGISTRY: dict[str, Callable[[Any], Adapted]] = {}


def adapter(qualname: str) -> Callable[[Callable[[Any], Adapted]], Callable[[Any], Adapted]]:
    """Register an adapter for a fully-qualified type name.

        @flight.adapter("numpy.ndarray")
        def _(arr): return Adapted("ndarray", f"{arr.dtype}{arr.shape}", {...})
    """

    def deco(fn: Callable[[Any], Adapted]) -> Callable[[Any], Adapted]:
        _REGISTRY[qualname] = fn
        return fn

    return deco


def resolve(obj: Any) -> Optional[Callable[[Any], Adapted]]:
    """Return the adapter for `obj`'s type, if one is registered."""
    t = type(obj)
    name = f"{t.__module__}.{t.__qualname__}"
    return _REGISTRY.get(name)


def _register_builtins() -> None:
    """Adapters for the common scientific types, defined without importing them."""

    @adapter("numpy.ndarray")
    def _ndarray(arr: Any) -> Adapted:  # pragma: no cover - exercised only with numpy
        fields: dict[str, Any] = {"shape": tuple(arr.shape), "dtype": str(arr.dtype)}
        try:
            flat = arr.ravel()
            fields["head"] = [flat[i].item() for i in range(min(8, flat.size))]
            if arr.dtype.kind == "f":
                import math

                fields["nan_count"] = int(sum(1 for v in flat.tolist() if isinstance(v, float) and math.isnan(v)))
        except Exception:
            pass
        return Adapted("ndarray", f"ndarray{tuple(arr.shape)} {arr.dtype}", fields)

    @adapter("pandas.core.frame.DataFrame")
    def _dataframe(df: Any) -> Adapted:  # pragma: no cover - exercised only with pandas
        fields: dict[str, Any] = {}
        try:
            fields["shape"] = tuple(df.shape)
            fields["columns"] = [str(c) for c in list(df.columns)[:32]]
            fields["dtypes"] = {str(c): str(t) for c, t in list(df.dtypes.items())[:32]}
        except Exception:
            pass
        return Adapted("dataframe", f"DataFrame{getattr(df, 'shape', '')}", fields)

    @adapter("pandas.core.series.Series")
    def _series(s: Any) -> Adapted:  # pragma: no cover - exercised only with pandas
        fields: dict[str, Any] = {}
        try:
            fields["length"] = int(s.shape[0])
            fields["dtype"] = str(s.dtype)
            fields["head"] = [v for v in s.head(5).tolist()]
        except Exception:
            pass
        return Adapted("series", f"Series[{len(s)}] {getattr(s, 'dtype', '')}", fields)


_register_builtins()
