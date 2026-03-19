"""Tests for the FastAPI tracing middleware."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "sample-fastapi"))

from httpx import ASGITransport, AsyncClient

from codecanvas.tracer.middleware import TracingMiddleware, tracing_state
from codecanvas.tracer.models import TraceEventType

SAMPLE_ROOT = str(ROOT / "sample-fastapi")


@pytest.fixture(autouse=True)
def _reset_tracing_state():
    """Reset shared state between tests."""
    tracing_state.disable()
    tracing_state.last_result = None
    yield
    tracing_state.disable()
    tracing_state.last_result = None


@pytest.fixture
def app():
    from app.main import app as _app

    already = any(
        getattr(m, "cls", None) is TracingMiddleware
        for m in getattr(_app, "user_middleware", [])
    )
    if not already:
        _app.add_middleware(TracingMiddleware)
        _app.middleware_stack = None  # force rebuild
    return _app


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_middleware_traces_login_401(app):
    """POST /api/v1/auth/login with bad credentials -> 401, trace captured."""
    tracing_state.enable(SAMPLE_ROOT)

    async def _test():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            return await client.post(
                "/api/v1/auth/login",
                json={"email": "test@example.com", "password": "wrong"},
            )

    response = _run(_test())

    assert response.status_code == 401

    result = tracing_state.last_result
    assert result is not None, "Trace was not captured"
    assert result.metadata["method"] == "POST"
    assert result.metadata["path"] == "/api/v1/auth/login"
    assert result.metadata["statusCode"] == 401

    call_events = [e for e in result.events if e.event_type == TraceEventType.CALL]
    called_funcs = [e.func_name for e in call_events]
    assert len(call_events) > 0, "No call events captured"
    assert "login" in called_funcs, f"login not in {called_funcs}"
    assert "verify_user" in called_funcs, f"verify_user not in {called_funcs}"

    for event in result.events:
        assert event.file_path.startswith(SAMPLE_ROOT), (
            f"Non-project file in trace: {event.file_path}"
        )


def test_middleware_traces_login_success(app):
    """POST /api/v1/auth/login with valid user -> 200, full path traced."""
    fake_user = {"id": 1, "email": "test@example.com", "hashed_password": "pass123"}
    tracing_state.enable(SAMPLE_ROOT)

    async def _test():
        with patch(
            "app.dependencies.FakeDB.fetchone",
            new_callable=AsyncMock,
            return_value=fake_user,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test",
            ) as client:
                return await client.post(
                    "/api/v1/auth/login",
                    json={"email": "test@example.com", "password": "pass123"},
                )

    response = _run(_test())
    assert response.status_code == 200

    result = tracing_state.last_result
    assert result is not None, "Trace was not captured"
    assert result.metadata["statusCode"] == 200

    call_events = [e for e in result.events if e.event_type == TraceEventType.CALL]
    called_funcs = [e.func_name for e in call_events]

    assert "login" in called_funcs
    assert "verify_user" in called_funcs
    assert "issue_tokens" in called_funcs
    assert "find_by_email" in called_funcs
    # store_refresh_token may or may not appear depending on FakeDB mock depth


def test_middleware_auto_disables_after_one_request(app):
    """After tracing one request, middleware auto-disables (single-shot)."""
    tracing_state.enable(SAMPLE_ROOT)

    async def _test():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            await client.post(
                "/api/v1/auth/login",
                json={"email": "a@b.com", "password": "x"},
            )
            first_result = tracing_state.last_result

            await client.post(
                "/api/v1/auth/login",
                json={"email": "a@b.com", "password": "x"},
            )
            return first_result

    first_result = _run(_test())

    assert first_result is not None
    # Second request should not have overwritten the result
    assert tracing_state.last_result is first_result


def test_middleware_no_trace_when_disabled(app):
    """When tracing is not enabled, no trace is captured."""

    async def _test():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            return await client.post(
                "/api/v1/auth/login",
                json={"email": "a@b.com", "password": "x"},
            )

    response = _run(_test())
    assert response.status_code == 401
    assert tracing_state.last_result is None
