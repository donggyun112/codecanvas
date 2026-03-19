"""FastAPI middleware for per-request runtime tracing.

Single-request mode: ``sys.settrace`` is thread-global, so only one
request is traced at a time.  A thread-safe lock prevents concurrent
trace corruption.

Handles:
- Async handlers (coroutine suspend/resume collapsed by collector)
- Sync handlers in threadpool (``run_in_executor`` patching)
- Background tasks (collector stays active until task completes)
- Streaming responses (``BaseHTTPMiddleware`` consumes body in dispatch)

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
import threading
from contextvars import ContextVar
from typing import Any, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from codecanvas.tracer.collector import TraceCollector
from codecanvas.tracer.models import TraceResult

# Context var to tag the traced request — lets the collector (or hooks)
# know which async context is being traced even when other coroutines
# interleave on the same event loop thread.
_trace_request_id: ContextVar[str | None] = ContextVar("_trace_request_id", default=None)


class _TracingState:
    """Thread-safe shared tracing state.

    Uses a separate Lock for the trace slot (one trace at a time)
    and simple atomic attributes for the enable/disable flag.
    """

    def __init__(self) -> None:
        self._slot_lock = threading.Lock()
        self._enabled = False
        self._project_root: str | None = None
        self.last_result: TraceResult | None = None

    def enable(self, project_root: str) -> bool:
        """Arm tracing for the next incoming request.

        Returns False if a previous trace is still in progress (e.g.
        a background task is still running).  The caller should wait
        or retry.
        """
        if self._slot_lock.locked():
            return False
        self._project_root = project_root
        self._enabled = True
        self.last_result = None
        return True

    def disable(self) -> None:
        self._enabled = False

    @property
    def should_trace(self) -> bool:
        return self._enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def project_root(self) -> str | None:
        return self._project_root

    def _acquire(self) -> bool:
        """Try to claim the single trace slot (thread-safe, non-blocking)."""
        return self._slot_lock.acquire(blocking=False)

    def _release(self) -> None:
        try:
            self._slot_lock.release()
        except RuntimeError:
            pass


# Module-level singleton.
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

        # Tag this async context so background tasks share the trace ID.
        request_id = f"{request.method}:{request.url.path}"
        token = _trace_request_id.set(request_id)

        collector = TraceCollector(
            tracing_state.project_root,
            request_context_var=_trace_request_id,
            request_id=request_id,
        )

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

            # Wrap background tasks to keep the collector active.
            if hasattr(response, "background") and response.background is not None:
                original_bg = response.background
                # Clear context var so the outer finally doesn't double-finalize.
                _trace_request_id.set(None)

                async def _traced_background() -> None:
                    try:
                        if asyncio.iscoroutinefunction(original_bg):
                            await original_bg()
                        elif callable(original_bg):
                            result = original_bg()
                            if asyncio.iscoroutine(result):
                                await result
                    finally:
                        self._finalize_trace(
                            collector, loop, original_run_in_executor,
                            request, status_code, token,
                        )
                response.background = _traced_background  # type: ignore[assignment]
                return response

            return response
        except Exception as exc:
            collector.record_exception(exc)
            status_code = 500
            raise
        finally:
            # If we didn't defer to a background task, finalize now.
            if _trace_request_id.get() is not None:
                self._finalize_trace(
                    collector, loop, original_run_in_executor,
                    request, status_code, token,
                )

    @staticmethod
    def _finalize_trace(
        collector: TraceCollector,
        loop: asyncio.AbstractEventLoop,
        original_run_in_executor: Any,
        request: Request,
        status_code: int | None,
        context_token: Any,
    ) -> None:
        """Stop collector, restore patches, store result."""
        if collector._active:
            result = collector.stop()
        else:
            result = collector.result
        loop.run_in_executor = original_run_in_executor  # type: ignore[assignment]
        if result:
            result.metadata.update({
                "method": request.method,
                "path": str(request.url.path),
                "statusCode": status_code,
            })
            tracing_state.last_result = result
        tracing_state.disable()
        tracing_state._release()
        _trace_request_id.set(None)
