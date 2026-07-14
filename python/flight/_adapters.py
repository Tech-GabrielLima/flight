from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class Adapted:
    kind: str
    summary: str
    fields: dict[str, Any] = field(default_factory=dict)


_REGISTRY: dict[str, Callable[[Any], Adapted]] = {}


def adapter(qualname: str) -> Callable[[Callable[[Any], Adapted]], Callable[[Any], Adapted]]:

    def deco(fn: Callable[[Any], Adapted]) -> Callable[[Any], Adapted]:
        _REGISTRY[qualname] = fn
        return fn

    return deco


def resolve(obj: Any) -> Optional[Callable[[Any], Adapted]]:
    t = type(obj)
    name = f"{t.__module__}.{t.__qualname__}"
    return _REGISTRY.get(name)


def _register_builtins() -> None:

    @adapter("numpy.ndarray")
    def _ndarray(arr: Any) -> Adapted:
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
    def _dataframe(df: Any) -> Adapted:
        fields: dict[str, Any] = {}
        try:
            fields["shape"] = tuple(df.shape)
            fields["columns"] = [str(c) for c in list(df.columns)[:32]]
            fields["dtypes"] = {str(c): str(t) for c, t in list(df.dtypes.items())[:32]}
        except Exception:
            pass
        return Adapted("dataframe", f"DataFrame{getattr(df, 'shape', '')}", fields)

    @adapter("pandas.core.series.Series")
    def _series(s: Any) -> Adapted:
        fields: dict[str, Any] = {}
        try:
            fields["length"] = int(s.shape[0])
            fields["dtype"] = str(s.dtype)
            fields["head"] = [v for v in s.head(5).tolist()]
        except Exception:
            pass
        return Adapted("series", f"Series[{len(s)}] {getattr(s, 'dtype', '')}", fields)

    _REGISTRY["pandas.Series"] = _REGISTRY["pandas.core.series.Series"]
    _REGISTRY["pandas.DataFrame"] = _REGISTRY["pandas.core.frame.DataFrame"]


_register_builtins()
