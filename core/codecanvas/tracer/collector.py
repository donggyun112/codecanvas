"""Runtime trace collector based on ``sys.settrace``.

Uses ``sys.settrace`` instead of ``sys.setprofile`` to get proper
``exception`` events inline.

Exception unwind detection uses ``sys.exc_info()`` at return time to
distinguish real returns from exception propagation through ``finally``
blocks.  ``line`` events are no longer used for this purpose.

Coroutine suspend/resume cycles are collapsed: one CALL when the
coroutine first enters, one RETURN when it truly finishes.  Intermediate
await-suspend returns and resume-calls are silently dropped.  Async
machinery exceptions (``StopIteration``, ``GeneratorExit``,
``StopAsyncIteration``) are filtered out to avoid bogus EXCEPTION events.

v1 limitation: the trace hook is set on the current thread only.
Sync FastAPI handlers running in a threadpool will not be captured.
"""
from __future__ import annotations

import opcode
import os
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from types import FrameType, TracebackType
from typing import Any, Iterator

from codecanvas.tracer.models import TraceEvent, TraceEventType, TraceResult

_IGNORED_SEGMENTS = {".git", ".venv", "__pycache__", "node_modules", "venv"}

# CPython code-object flags for coroutine detection.
# Values differ across Python versions — use inspect as canonical source.
import inspect as _inspect

_CO_COROUTINE = getattr(_inspect, "CO_COROUTINE", 0x80)
_CO_ASYNC_GENERATOR = getattr(_inspect, "CO_ASYNC_GENERATOR", 0x200)
_CO_ITERABLE_COROUTINE = getattr(_inspect, "CO_ITERABLE_COROUTINE", 0x100)

# Exceptions that are part of the async protocol, not application errors.
_ASYNC_MACHINERY_EXCEPTIONS = (StopIteration, StopAsyncIteration, GeneratorExit)

# Opcodes that represent a real function return (not exception unwind).
_RETURN_OPCODES: set[int] = set()
for _name in ("RETURN_VALUE", "RETURN_CONST"):
    _op = opcode.opmap.get(_name)
    if _op is not None:
        _RETURN_OPCODES.add(_op)


@dataclass
class _CallState:
    """Internal state for one active Python frame."""

    file_path: str
    func_name: str
    line: int
    started_at_ns: int
    depth: int


class TraceCollector:
    """Collect one runtime trace for a target project root.

    When ``request_context_var`` is provided, the collector only captures
    frames that execute within the matching async context.  This prevents
    events from unrelated concurrent coroutines leaking into the trace.
    """

    def __init__(
        self,
        project_root: str,
        request_context_var: Any | None = None,
        request_id: str | None = None,
    ):
        self.project_root = self._normalize_path(project_root)
        self.events: list[TraceEvent] = []
        self.result: TraceResult | None = None

        self._call_states: dict[int, _CallState] = {}
        self._frame_stack: list[int] = []
        self._active = False
        self._started_at_ns: int | None = None
        self._ended_at_ns: int | None = None
        self._previous_trace: Any = None

        # Async context isolation
        self._request_context_var = request_context_var
        self._request_id = request_id

        # Coroutine tracking
        self._coroutine_frames: set[int] = set()
        self._pending_returns: dict[int, TraceEvent] = {}

        # Exception tracking: frames that received an exception event
        # and have NOT yet had that exception caught.
        self._exception_frames: set[int] = set()

        # Cleanup hooks for runtime instrumentation (SQLAlchemy, httpx, etc.)
        self._cleanup_hooks: list[Any] = []

    # -- public API --

    def start(self) -> None:
        """Start capturing runtime events for the current thread."""
        if self._active:
            raise RuntimeError("TraceCollector is already active")

        self.events = []
        self.result = None
        self._call_states = {}
        self._frame_stack = []
        self._coroutine_frames = set()
        self._pending_returns = {}
        self._exception_frames = set()
        self._started_at_ns = time.perf_counter_ns()
        self._ended_at_ns = None
        self._previous_trace = sys.gettrace()
        self._active = True
        sys.settrace(self._trace_dispatch)
        # Also install for new threads (covers threadpool workers on first use)
        threading.settrace(self._trace_dispatch)

    def stop(self) -> TraceResult:
        """Stop capturing and build the trace result."""
        if not self._active:
            raise RuntimeError("TraceCollector is not active")

        sys.settrace(self._previous_trace)
        threading.settrace(None)
        self._active = False
        self._ended_at_ns = time.perf_counter_ns()
        self._run_cleanup_hooks()

        # Flush deferred coroutine returns (their last return was a real finish)
        for frame_id, event in self._pending_returns.items():
            self.events.append(event)
            self._call_states.pop(frame_id, None)
            self._discard_frame(frame_id)
        self._pending_returns.clear()
        self._coroutine_frames.clear()

        # Sort by timestamp so deferred coroutine returns appear in correct order
        self.events.sort(key=lambda e: e.timestamp_ns)

        self.result = self._build_result()
        return self.result

    @contextmanager
    def trace(self) -> Iterator[TraceCollector]:
        """Context manager for a single traced execution."""
        self.start()
        try:
            yield self
        except BaseException as exc:
            self.record_exception(exc)
            raise
        finally:
            if self._active:
                self.stop()

    def record_event(
        self,
        event_type: TraceEventType,
        *,
        file_path: str,
        func_name: str,
        line: int,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Record a manual event such as DB or HTTP activity."""
        normalized = self._normalize_path(file_path)
        if not self._is_project_file(normalized):
            return

        self.events.append(
            TraceEvent(
                event_type=event_type,
                file_path=normalized,
                func_name=func_name,
                line=line,
                timestamp_ns=time.perf_counter_ns(),
                detail=detail or {},
            )
        )

    def record_exception(self, exc: BaseException) -> None:
        """Record an exception that escaped the traced boundary.

        Skips recording if ``settrace`` already captured the same exception
        inline (avoids duplicates).
        """
        frame, line = self._find_project_traceback_frame(exc.__traceback__)
        if frame is None:
            return

        file_path = self._normalize_path(frame.f_code.co_filename)
        func_name = frame.f_code.co_name

        # Deduplicate against inline exception events from settrace
        for prev in reversed(self.events[-20:]):
            if (
                prev.event_type == TraceEventType.EXCEPTION
                and prev.func_name == func_name
                and prev.file_path == file_path
            ):
                return

        self.record_event(
            TraceEventType.EXCEPTION,
            file_path=frame.f_code.co_filename,
            func_name=func_name,
            line=line,
            detail=self._summarize_exception(exc),
        )

    # -- sys.settrace callbacks --

    def _trace_dispatch(self, frame: FrameType, event: str, arg: Any) -> Any:
        """Global trace function — decides whether to trace a new frame."""
        if event != "call":
            return None

        # Context isolation: skip frames from other async contexts.
        if self._request_context_var is not None and self._request_id is not None:
            current_id = self._request_context_var.get(None)
            if current_id != self._request_id:
                return None

        info = self._frame_info(frame)
        if info is None:
            return None  # skip non-project frames entirely

        frame_id = id(frame)

        # Coroutine resume: same frame entering again after a suspend
        if frame_id in self._coroutine_frames:
            self._pending_returns.pop(frame_id, None)
            return self._frame_trace

        file_path, func_name, line = info
        depth = len(self._frame_stack)

        self._call_states[frame_id] = _CallState(
            file_path=file_path,
            func_name=func_name,
            line=line,
            started_at_ns=time.perf_counter_ns(),
            depth=depth,
        )
        self._frame_stack.append(frame_id)

        if self._is_coroutine(frame):
            self._coroutine_frames.add(frame_id)

        self.events.append(
            TraceEvent(
                event_type=TraceEventType.CALL,
                file_path=file_path,
                func_name=func_name,
                line=line,
                timestamp_ns=time.perf_counter_ns(),
                detail={"depth": depth},
            )
        )
        return self._frame_trace

    def _frame_trace(self, frame: FrameType, event: str, arg: Any) -> Any:
        """Per-frame trace function for project files."""
        if event == "exception":
            self._handle_exception_event(frame, arg)
        elif event == "return":
            self._handle_return(frame, arg)
        # 'line' events: no-op (we keep the trace function active for
        # return/exception, but line events are not used for logic).
        return self._frame_trace

    def _handle_exception_event(self, frame: FrameType, arg: tuple) -> None:
        """An exception was raised (or is propagating through) this frame."""
        frame_id = id(frame)
        call_state = self._call_states.get(frame_id)
        if call_state is None:
            return

        exc_type = arg[0] if arg else None

        # Filter async machinery exceptions — these are protocol signals,
        # not application errors (e.g. StopIteration from awaiting a finished
        # coroutine, GeneratorExit on cleanup).
        if exc_type and issubclass(exc_type, _ASYNC_MACHINERY_EXCEPTIONS):
            return

        self._exception_frames.add(frame_id)

        self.events.append(
            TraceEvent(
                event_type=TraceEventType.EXCEPTION,
                file_path=call_state.file_path,
                func_name=call_state.func_name,
                line=frame.f_lineno,
                timestamp_ns=time.perf_counter_ns(),
                detail={
                    "exceptionType": exc_type.__name__ if exc_type else "Unknown",
                    "depth": call_state.depth,
                },
            )
        )

    def _handle_return(self, frame: FrameType, return_value: Any) -> None:
        """Handle a function return — real return, coroutine suspend, or exception unwind."""
        frame_id = id(frame)
        call_state = self._call_states.get(frame_id)
        if call_state is None:
            return

        # Exception unwind detection.
        # Check the bytecode at f_lasti: a real return executes RETURN_VALUE
        # or RETURN_CONST, while an exception unwind executes RERAISE (or
        # another non-return opcode).  This is reliable across finally blocks,
        # except blocks, and bare raises.
        if frame_id in self._exception_frames:
            self._exception_frames.discard(frame_id)
            current_opcode = frame.f_code.co_code[frame.f_lasti]
            if current_opcode not in _RETURN_OPCODES:
                # Exception still propagating — this is an unwind, not a real return.
                self._call_states.pop(frame_id, None)
                self._discard_frame(frame_id)
                self._coroutine_frames.discard(frame_id)
                self._pending_returns.pop(frame_id, None)
                return
            # else: exception was caught, frame is doing a real return.

        ended_at_ns = time.perf_counter_ns()
        return_event = TraceEvent(
            event_type=TraceEventType.RETURN,
            file_path=call_state.file_path,
            func_name=call_state.func_name,
            line=call_state.line,
            timestamp_ns=ended_at_ns,
            detail={
                "depth": call_state.depth,
                "durationMs": (ended_at_ns - call_state.started_at_ns) / 1_000_000,
                "returnValue": self._summarize_value(return_value),
            },
        )

        # Coroutine: defer — this might be a suspend, not the final return
        if frame_id in self._coroutine_frames:
            self._pending_returns[frame_id] = return_event
            return

        # Normal sync return
        self._call_states.pop(frame_id, None)
        self._discard_frame(frame_id)
        self.events.append(return_event)

    # -- internal helpers --

    def _build_result(self) -> TraceResult:
        if self._started_at_ns is None or self._ended_at_ns is None:
            raise RuntimeError("TraceCollector has not completed a trace")

        return TraceResult(
            project_root=self.project_root,
            started_at_ns=self._started_at_ns,
            ended_at_ns=self._ended_at_ns,
            events=list(self.events),
        )

    def _discard_frame(self, frame_id: int) -> None:
        for index in range(len(self._frame_stack) - 1, -1, -1):
            if self._frame_stack[index] == frame_id:
                del self._frame_stack[index]
                break

    def _frame_info(self, frame: FrameType) -> tuple[str, str, int] | None:
        file_path = self._normalize_path(frame.f_code.co_filename)
        if not self._is_project_file(file_path):
            return None
        return file_path, frame.f_code.co_name, frame.f_code.co_firstlineno

    def _find_project_traceback_frame(
        self,
        tb: TracebackType | None,
    ) -> tuple[FrameType | None, int]:
        candidate: tuple[FrameType | None, int] = (None, 0)
        cursor = tb
        while cursor is not None:
            frame = cursor.tb_frame
            file_path = self._normalize_path(frame.f_code.co_filename)
            if self._is_project_file(file_path):
                candidate = (frame, cursor.tb_lineno)
            cursor = cursor.tb_next
        return candidate

    def _is_project_file(self, file_path: str) -> bool:
        if not file_path or file_path.startswith("<") or not file_path.endswith(".py"):
            return False

        path_parts = set(os.path.normpath(file_path).split(os.sep))
        if path_parts & _IGNORED_SEGMENTS:
            return False

        try:
            return os.path.commonpath([self.project_root, file_path]) == self.project_root
        except ValueError:
            return False

    @staticmethod
    def _is_coroutine(frame: FrameType) -> bool:
        return bool(
            frame.f_code.co_flags
            & (_CO_COROUTINE | _CO_ASYNC_GENERATOR | _CO_ITERABLE_COROUTINE)
        )

    @staticmethod
    def _normalize_path(path: str) -> str:
        return os.path.realpath(os.path.abspath(path))

    @staticmethod
    def _summarize_value(value: Any) -> dict[str, Any]:
        if value is None:
            return {"type": "NoneType", "isNone": True}
        if isinstance(value, bool):
            return {"type": "bool", "value": value}
        if isinstance(value, (int, float)):
            return {"type": type(value).__name__, "value": value}
        if isinstance(value, str):
            return {"type": "str", "length": len(value)}
        if isinstance(value, (bytes, bytearray)):
            return {"type": type(value).__name__, "length": len(value)}
        if isinstance(value, dict):
            return {"type": "dict", "length": len(value)}
        if isinstance(value, (list, tuple, set, frozenset)):
            return {"type": type(value).__name__, "length": len(value)}
        return {"type": type(value).__name__}

    # -- runtime hooks for DB/HTTP instrumentation --

    def install_sqlalchemy_hook(self, engine: Any) -> None:
        """Attach SQLAlchemy event listeners to capture queries.

        Call after ``start()`` and detach automatically on ``stop()``.
        Requires ``sqlalchemy`` to be importable.
        """
        try:
            from sqlalchemy import event as sa_event
        except ImportError:
            return

        def _before_execute(conn, clauseelement, multiparams, params, execution_options):
            stmt = str(clauseelement)
            # Redact literal values for safety
            self.record_event(
                TraceEventType.DB,
                file_path=self.project_root,
                func_name="sqlalchemy.execute",
                line=0,
                detail={
                    "query": stmt[:500],
                    "queryLength": len(stmt),
                    "params": "REDACTED",
                },
            )

        sa_event.listen(engine, "before_cursor_execute", _before_execute)
        self._cleanup_hooks.append(
            lambda: sa_event.remove(engine, "before_cursor_execute", _before_execute)
        )

    def install_httpx_hook(self) -> None:
        """Monkey-patch httpx transport to capture outgoing HTTP requests.

        Captures method, URL, status code, and duration.
        """
        try:
            import httpx
        except ImportError:
            return

        original_send = httpx.AsyncClient.send

        collector = self

        async def _traced_send(self_client, request, **kwargs):
            start_ns = time.perf_counter_ns()
            response = await original_send(self_client, request, **kwargs)
            duration_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
            collector.record_event(
                TraceEventType.HTTP,
                file_path=collector.project_root,
                func_name="httpx.request",
                line=0,
                detail={
                    "method": request.method,
                    "url": str(request.url)[:200],
                    "statusCode": response.status_code,
                    "durationMs": round(duration_ms, 2),
                },
            )
            return response

        httpx.AsyncClient.send = _traced_send  # type: ignore[assignment]
        self._cleanup_hooks.append(
            lambda: setattr(httpx.AsyncClient, "send", original_send)
        )

    def _run_cleanup_hooks(self) -> None:
        """Detach all installed runtime hooks."""
        for hook in self._cleanup_hooks:
            try:
                hook()
            except Exception:
                pass
        self._cleanup_hooks.clear()

    @staticmethod
    def _summarize_exception(exc: BaseException) -> dict[str, Any]:
        detail: dict[str, Any] = {"exceptionType": type(exc).__name__}
        message = str(exc)
        if message:
            detail["messageLength"] = len(message)
        return detail
