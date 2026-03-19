"""Runtime tracing primitives for CodeCanvas."""

from codecanvas.tracer.collector import TraceCollector
from codecanvas.tracer.models import TraceEvent, TraceEventType, TraceResult

__all__ = [
    "TraceCollector",
    "TraceEvent",
    "TraceEventType",
    "TraceResult",
]


def __getattr__(name: str):
    """Lazy-load middleware to avoid hard Starlette dependency."""
    if name == "TracingMiddleware":
        from codecanvas.tracer.middleware import TracingMiddleware
        return TracingMiddleware
    if name == "tracing_state":
        from codecanvas.tracer.middleware import tracing_state
        return tracing_state
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
