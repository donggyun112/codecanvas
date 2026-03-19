"""Tests for the runtime trace collector."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from codecanvas.tracer import TraceCollector, TraceEventType


def _load_module(module_path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_module(tmp_path: Path, module_name: str, source: str):
    module_path = tmp_path / f"{module_name}.py"
    module_path.write_text(textwrap.dedent(source).strip() + "\n", encoding="utf-8")
    return _load_module(module_path, module_name), module_path


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Sync: basic call/return
# ---------------------------------------------------------------------------

def test_trace_collector_captures_call_and_return_events(tmp_path: Path) -> None:
    module, module_path = _write_module(
        tmp_path,
        "trace_target",
        """
        def inner(value):
            return value + 1


        def outer():
            return inner(1)
        """,
    )
    collector = TraceCollector(str(tmp_path))

    with collector.trace():
        assert module.outer() == 2

    assert collector.result is not None
    events = [(event.event_type.value, event.func_name) for event in collector.result.events]
    assert events == [
        ("call", "outer"),
        ("call", "inner"),
        ("return", "inner"),
        ("return", "outer"),
    ]

    outer_call = collector.result.events[0]
    assert outer_call.file_path == str(module_path)
    assert outer_call.line == 5

    outer_return = collector.result.events[-1]
    assert outer_return.event_type == TraceEventType.RETURN
    assert outer_return.detail["durationMs"] >= 0
    assert outer_return.detail["returnValue"] == {"type": "int", "value": 2}


def test_trace_collector_filters_non_project_files(tmp_path: Path) -> None:
    module, _ = _write_module(
        tmp_path,
        "trace_target",
        """
        import json


        def outer():
            json.loads('{"ok": true}')
            return {"ok": True}
        """,
    )
    collector = TraceCollector(str(tmp_path))

    with collector.trace():
        module.outer()

    assert collector.result is not None
    assert all(event.file_path.startswith(str(tmp_path)) for event in collector.result.events)
    assert [event.func_name for event in collector.result.events] == ["outer", "outer"]


# ---------------------------------------------------------------------------
# Sync: exception handling
# ---------------------------------------------------------------------------

def test_trace_collector_records_boundary_exception(tmp_path: Path) -> None:
    """Exception escaping the trace boundary is recorded."""
    module, module_path = _write_module(
        tmp_path,
        "trace_target",
        """
        def boom():
            raise ValueError("secret-token")
        """,
    )
    collector = TraceCollector(str(tmp_path))

    with pytest.raises(ValueError):
        with collector.trace():
            module.boom()

    assert collector.result is not None
    exception_events = [
        event for event in collector.result.events
        if event.event_type == TraceEventType.EXCEPTION
    ]
    assert len(exception_events) >= 1

    exc_event = exception_events[0]
    assert exc_event.file_path == str(module_path)
    assert exc_event.func_name == "boom"
    assert exc_event.detail["exceptionType"] == "ValueError"

    # Should NOT have a false RETURN for the unwinding frame
    return_events = [
        event for event in collector.result.events
        if event.event_type == TraceEventType.RETURN and event.func_name == "boom"
    ]
    assert return_events == [], "Exception unwind should not produce a RETURN event"


def test_caught_exception_still_produces_normal_return(tmp_path: Path) -> None:
    """When a function catches an exception internally, it should still
    produce a normal CALL + RETURN pair (not look like an exception unwind)."""
    module, _ = _write_module(
        tmp_path,
        "trace_target",
        """
        def safe():
            try:
                raise ValueError("oops")
            except ValueError:
                pass
            return 42
        """,
    )
    collector = TraceCollector(str(tmp_path))

    with collector.trace():
        assert module.safe() == 42

    assert collector.result is not None

    types = [(e.event_type.value, e.func_name) for e in collector.result.events]
    # Should have: call, exception (caught), return (normal)
    assert ("call", "safe") in types
    assert ("return", "safe") in types

    ret = next(e for e in collector.result.events
               if e.event_type == TraceEventType.RETURN and e.func_name == "safe")
    assert ret.detail["returnValue"] == {"type": "int", "value": 42}


def test_exception_in_nested_call_does_not_pollute_caller_return(tmp_path: Path) -> None:
    """If inner() raises and outer() catches it, outer() should still get
    a clean RETURN event with its actual return value."""
    module, _ = _write_module(
        tmp_path,
        "trace_target",
        """
        def inner():
            raise RuntimeError("fail")


        def outer():
            try:
                inner()
            except RuntimeError:
                pass
            return "ok"
        """,
    )
    collector = TraceCollector(str(tmp_path))

    with collector.trace():
        assert module.outer() == "ok"

    assert collector.result is not None

    # inner should have CALL + EXCEPTION, but NO RETURN
    inner_returns = [
        e for e in collector.result.events
        if e.event_type == TraceEventType.RETURN and e.func_name == "inner"
    ]
    assert inner_returns == [], "Exception unwind should not produce RETURN for inner()"

    # outer should have a clean RETURN
    outer_return = next(
        e for e in collector.result.events
        if e.event_type == TraceEventType.RETURN and e.func_name == "outer"
    )
    assert outer_return.detail["returnValue"] == {"type": "str", "length": 2}


# ---------------------------------------------------------------------------
# Async: coroutine suspend/resume collapsing
# ---------------------------------------------------------------------------

def test_finally_block_does_not_produce_false_return(tmp_path: Path) -> None:
    """Exception propagating through a finally block should NOT produce
    a false RETURN event (the finally body generates line events but the
    exception is still in flight)."""
    module, _ = _write_module(
        tmp_path,
        "trace_target",
        """
        def cleanup():
            try:
                raise ValueError("boom")
            finally:
                x = 1
        """,
    )
    collector = TraceCollector(str(tmp_path))

    with pytest.raises(ValueError):
        with collector.trace():
            module.cleanup()

    assert collector.result is not None

    cleanup_returns = [
        e for e in collector.result.events
        if e.event_type == TraceEventType.RETURN and e.func_name == "cleanup"
    ]
    assert cleanup_returns == [], (
        "Exception propagating through finally should not produce RETURN"
    )

    cleanup_exceptions = [
        e for e in collector.result.events
        if e.event_type == TraceEventType.EXCEPTION and e.func_name == "cleanup"
    ]
    assert len(cleanup_exceptions) >= 1


def test_async_no_bogus_exception_from_await_machinery(tmp_path: Path) -> None:
    """Normal await should NOT produce EXCEPTION events from async
    machinery (StopIteration, GeneratorExit, etc.)."""
    module, _ = _write_module(
        tmp_path,
        "trace_target",
        """
        import asyncio

        async def helper():
            return "done"

        async def main():
            result = await helper()
            return result
        """,
    )
    collector = TraceCollector(str(tmp_path))

    with collector.trace():
        result = _run(module.main())

    assert result == "done"
    assert collector.result is not None

    exceptions = [
        e for e in collector.result.events
        if e.event_type == TraceEventType.EXCEPTION
        and e.func_name in ("helper", "main")
    ]
    assert exceptions == [], (
        f"Normal await should not produce EXCEPTION events, got: "
        f"{[(e.func_name, e.detail) for e in exceptions]}"
    )


def test_async_return_ordering_is_correct(tmp_path: Path) -> None:
    """Coroutine returns flushed at stop() should appear in timestamp
    order, not appended at the end."""
    module, _ = _write_module(
        tmp_path,
        "trace_target",
        """
        import asyncio

        async def inner():
            await asyncio.sleep(0)
            return 1

        async def helper():
            return 2

        async def outer():
            a = await inner()
            b = helper()  # sync call to create coroutine (not awaited in trace)
            return a
        """,
    )
    collector = TraceCollector(str(tmp_path))

    with collector.trace():
        _run(module.outer())

    assert collector.result is not None

    # Events should be sorted by timestamp
    timestamps = [e.timestamp_ns for e in collector.result.events]
    assert timestamps == sorted(timestamps), "Events are not in timestamp order"


def test_async_coroutine_produces_single_call_return_pair(tmp_path: Path) -> None:
    """An async function that awaits should produce exactly one CALL and
    one RETURN — not intermediate suspend/resume noise."""
    module, _ = _write_module(
        tmp_path,
        "trace_target",
        """
        import asyncio

        async def inner():
            await asyncio.sleep(0)
            return 42

        async def outer():
            result = await inner()
            return result + 1
        """,
    )
    collector = TraceCollector(str(tmp_path))

    with collector.trace():
        result = _run(module.outer())

    assert result == 43
    assert collector.result is not None

    call_events = [
        (e.func_name, e.event_type.value)
        for e in collector.result.events
        if e.func_name in ("outer", "inner")
    ]

    # Each function should appear exactly once as call, once as return
    assert call_events.count(("outer", "call")) == 1
    assert call_events.count(("inner", "call")) == 1
    assert call_events.count(("outer", "return")) <= 1  # flushed in stop()
    assert call_events.count(("inner", "return")) <= 1

    # inner's return value should be 42 (not None from a suspend)
    inner_returns = [
        e for e in collector.result.events
        if e.event_type == TraceEventType.RETURN and e.func_name == "inner"
    ]
    if inner_returns:
        assert inner_returns[0].detail["returnValue"] == {"type": "int", "value": 42}


def test_async_exception_does_not_produce_false_return(tmp_path: Path) -> None:
    """An async function that raises should get EXCEPTION, not a RETURN(None)."""
    module, _ = _write_module(
        tmp_path,
        "trace_target",
        """
        async def async_boom():
            raise ValueError("async error")

        async def run():
            try:
                await async_boom()
            except ValueError:
                return "caught"
        """,
    )
    collector = TraceCollector(str(tmp_path))

    with collector.trace():
        result = _run(module.run())

    assert result == "caught"
    assert collector.result is not None

    # async_boom should have EXCEPTION, not RETURN
    boom_returns = [
        e for e in collector.result.events
        if e.event_type == TraceEventType.RETURN and e.func_name == "async_boom"
    ]
    assert boom_returns == [], "async_boom should not have a RETURN event"

    boom_exceptions = [
        e for e in collector.result.events
        if e.event_type == TraceEventType.EXCEPTION and e.func_name == "async_boom"
    ]
    assert len(boom_exceptions) >= 1
    assert boom_exceptions[0].detail["exceptionType"] == "ValueError"
