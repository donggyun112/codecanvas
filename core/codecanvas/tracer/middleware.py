"""FastAPI middleware for per-request runtime tracing.

v1 design: single-request mode.  The ``sys.settrace`` hook is
thread-global so overlapping requests would mix events.  The middleware
enforces single-shot tracing with a concurrent-request guard.

For sync handlers that run in a threadpool, the middleware temporarily
patches ``loop.run_in_executor`` to install the trace hook in worker
threads.

Usage::

    from codecanvas.tracer.middleware import TracingMiddleware, tracing_state

    app.add_middleware(TracingMiddleware)

    # Enable tracing for the next request
    tracing_state.enable("/path/to/project")

    # After the request completes
    result = tracing_state.last_result
"""
from __future__ import annotations

import asyncio
import functools
import sys
from typing import Any, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from codecanvas.tracer.collector import TraceCollector
from codecanvas.tracer.models import TraceResult


class _TracingState:
    """Shared tracing state accessible from outside the middleware."""

    def __init__(self) -> None:
        self._enabled = False
        self._in_progress = False
        self._project_root: str | None = None
        self.last_result: TraceResult | None = None

    def enable(self, project_root: str) -> None:
        """Arm tracing for the next incoming request."""
        self._project_root = project_root
        self._enabled = True
        self.last_result = None

    def disable(self) -> None:
        self._enabled = False

    @property
    def should_trace(self) -> bool:
        """True only when armed AND no other request is being traced."""
        return self._enabled and not self._in_progress

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def project_root(self) -> str | None:
        return self._project_root

    def _acquire(self) -> bool:
        """Try to claim the single trace slot. Returns False if busy."""
        if self._in_progress:
            return False
        self._in_progress = True
        return True

    def _release(self) -> None:
        self._in_progress = False


# Module-level singleton — imported by both the middleware and the caller.
tracing_state = _TracingState()


class TracingMiddleware(BaseHTTPMiddleware):
    """Capture a runtime trace for every request while tracing is enabled."""

    async def dispatch(
        self, request: Request, call_next: Callable[..., Any],
    ) -> Response:
        if not tracing_state.should_trace or not tracing_state.project_root:
            return await call_next(request)

        if not tracing_state._acquire():
            return await call_next(request)

        collector = TraceCollector(tracing_state.project_root)

        # Patch executor so sync handlers in threadpool get the trace hook
        loop = asyncio.get_running_loop()
        original_run_in_executor = loop.run_in_executor

        def _traced_run_in_executor(executor, fn, /, *args):
            @functools.wraps(fn)
            def _wrapper(*a):
                sys.settrace(collector._trace_dispatch)
                try:
                    return fn(*a)
                finally:
                    sys.settrace(None)
            return original_run_in_executor(executor, _wrapper, *args)

        loop.run_in_executor = _traced_run_in_executor  # type: ignore[assignment]
        collector.start()
        status_code: int | None = None

        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception as exc:
            collector.record_exception(exc)
            status_code = 500
            raise
        finally:
            result = collector.stop()
            loop.run_in_executor = original_run_in_executor  # type: ignore[assignment]
            result.metadata.update({
                "method": request.method,
                "path": str(request.url.path),
                "statusCode": status_code,
            })
            tracing_state.last_result = result
            tracing_state.disable()
            tracing_state._release()
