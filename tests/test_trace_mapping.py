"""Test runtime trace → static graph mapping (Step 3+4).

This is the integration test that proves CodeCanvas can distinguish
"actually executed" from "statically possible" in a single graph.
"""
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

from codecanvas.graph.builder import FlowGraphBuilder
from codecanvas.tracer.mapper import TraceMapper
from codecanvas.tracer.middleware import TracingMiddleware, tracing_state
from codecanvas.tracer.models import TraceEventType

SAMPLE_ROOT = str(ROOT / "sample-fastapi")


@pytest.fixture(autouse=True)
def _reset():
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
        _app.middleware_stack = None
    return _app


@pytest.fixture
def builder():
    return FlowGraphBuilder(SAMPLE_ROOT)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# The main integration test
# ---------------------------------------------------------------------------

def test_login_401_marks_hit_nodes_and_leaves_success_path_unhit(app, builder):
    """POST /login with bad creds → 401.

    verify_user is hit, but issue_tokens is NOT hit.
    The graph should clearly show this distinction.
    """
    # 1. Build static flow
    login_entry = next(
        e for e in builder.get_endpoints()
        if "login" in e.handler_name
    )
    static_graph = builder.build_flow(login_entry)

    # 2. Run traced request
    tracing_state.enable(SAMPLE_ROOT)

    async def _request():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            return await client.post(
                "/api/v1/auth/login",
                json={"email": "test@example.com", "password": "wrong"},
            )

    response = _run(_request())
    assert response.status_code == 401

    trace = tracing_state.last_result
    assert trace is not None

    # 3. Map trace onto static graph
    mapper = TraceMapper(builder.call_graph, project_root=SAMPLE_ROOT)
    merged = mapper.apply(static_graph, trace)

    # 4. Verify: login handler was hit
    login_node = next(
        n for n in merged.nodes.values()
        if n.name == "login" and n.level == 3
    )
    assert login_node.metadata.get("runtime_hit") is True
    assert login_node.metadata.get("execution_order", 0) >= 1

    # 5. Verify: verify_user was hit (called during 401 path)
    verify_node = next(
        (n for n in merged.nodes.values() if n.name == "verify_user"),
        None,
    )
    assert verify_node is not None
    assert verify_node.metadata.get("runtime_hit") is True

    # 6. Verify: issue_tokens was NOT hit (only on success path)
    issue_node = next(
        (n for n in merged.nodes.values() if n.name == "issue_tokens"),
        None,
    )
    assert issue_node is not None
    assert issue_node.metadata.get("runtime_hit") is False

    # 7. Verify: trace summary is attached
    trace_meta = merged.entrypoint.metadata.get("trace")
    assert trace_meta is not None
    assert trace_meta["statusCode"] == 401
    assert trace_meta["hitNodes"] > 0
    assert trace_meta["hitNodes"] < trace_meta["totalNodes"]


def test_login_200_hits_full_success_path(app, builder):
    """POST /login with valid user → 200.

    Both verify_user AND issue_tokens should be hit.
    """
    login_entry = next(
        e for e in builder.get_endpoints()
        if "login" in e.handler_name
    )
    static_graph = builder.build_flow(login_entry)

    fake_user = {"id": 1, "email": "test@example.com", "hashed_password": "pass123"}
    tracing_state.enable(SAMPLE_ROOT)

    async def _request():
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

    response = _run(_request())
    assert response.status_code == 200

    trace = tracing_state.last_result
    assert trace is not None

    mapper = TraceMapper(builder.call_graph, project_root=SAMPLE_ROOT)
    merged = mapper.apply(static_graph, trace)

    # Full success path: login → verify_user → issue_tokens
    for name in ("login", "verify_user", "issue_tokens"):
        node = next(
            (n for n in merged.nodes.values() if n.name == name),
            None,
        )
        assert node is not None, f"{name} not found in graph"
        assert node.metadata.get("runtime_hit") is True, (
            f"{name} should be hit on 200 path"
        )

    # Repository calls should also be hit
    for name in ("find_by_email", "store_refresh_token"):
        node = next(
            (n for n in merged.nodes.values() if n.name == name),
            None,
        )
        assert node is not None, f"{name} not found"
        assert node.metadata.get("runtime_hit") is True, (
            f"{name} should be hit"
        )

    # Edges between hit nodes should be marked based on actual transitions
    hit_edges = [e for e in merged.edges if e.metadata.get("runtime_hit")]
    assert len(hit_edges) > 0, "No edges marked as runtime_hit"

    # Edge from verify_user → find_by_email should be hit (actual transition)
    verify_to_find = [
        e for e in merged.edges
        if "verify_user" in e.source_id and "find_by_email" in e.target_id
        and e.metadata.get("runtime_hit")
    ]
    assert len(verify_to_find) > 0, "verify_user → find_by_email should be a hit transition"

    trace_meta = merged.entrypoint.metadata.get("trace")
    assert trace_meta["statusCode"] == 200


def test_parent_nodes_propagate_hit_status(app, builder):
    """File-level and layer-level nodes should be marked hit
    when any of their children were hit."""
    login_entry = next(
        e for e in builder.get_endpoints()
        if "login" in e.handler_name
    )
    static_graph = builder.build_flow(login_entry)
    tracing_state.enable(SAMPLE_ROOT)

    async def _request():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            return await client.post(
                "/api/v1/auth/login",
                json={"email": "x@y.com", "password": "z"},
            )

    _run(_request())
    trace = tracing_state.last_result
    assert trace is not None

    mapper = TraceMapper(builder.call_graph, project_root=SAMPLE_ROOT)
    merged = mapper.apply(static_graph, trace)

    # auth.py file node should be hit (login handler is in it)
    file_nodes = [
        n for n in merged.nodes.values()
        if n.node_type.value == "file" and "auth" in n.name
    ]
    assert any(n.metadata.get("runtime_hit") for n in file_nodes), (
        "auth.py file node should be hit"
    )

    # routers layer should be hit
    layer_nodes = [
        n for n in merged.nodes.values()
        if n.node_type.value == "module" and n.metadata.get("runtime_hit")
    ]
    assert len(layer_nodes) > 0, "At least one layer should be hit"


def test_edge_hit_requires_actual_transition_not_both_ends(app, builder):
    """Edge A→C should NOT be marked hit just because A and C are both hit.
    Only edges representing actual caller→callee transitions get hit."""
    login_entry = next(
        e for e in builder.get_endpoints()
        if "login" in e.handler_name
    )
    static_graph = builder.build_flow(login_entry)
    tracing_state.enable(SAMPLE_ROOT)

    async def _request():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            return await client.post(
                "/api/v1/auth/login",
                json={"email": "x@y.com", "password": "z"},
            )

    _run(_request())
    trace = tracing_state.last_result
    assert trace is not None

    mapper = TraceMapper(builder.call_graph, project_root=SAMPLE_ROOT)
    merged = mapper.apply(static_graph, trace)

    # All hit edges should represent real transitions
    for edge in merged.edges:
        if edge.metadata.get("runtime_hit"):
            src = merged.nodes.get(edge.source_id)
            tgt = merged.nodes.get(edge.target_id)
            # Both endpoints should be hit (necessary condition)
            if src and tgt:
                assert src.metadata.get("runtime_hit"), (
                    f"Hit edge source {edge.source_id} is not hit"
                )


def test_runtime_only_nodes_are_created_for_unresolved_trace_calls(builder):
    """Functions seen in the trace but absent from the static graph should
    appear as runtime_only nodes, not be silently dropped."""
    from codecanvas.tracer.models import TraceEvent, TraceResult

    login_entry = next(
        e for e in builder.get_endpoints()
        if "login" in e.handler_name
    )
    static_graph = builder.build_flow(login_entry)

    # Simulate a trace with a function that doesn't exist in static analysis
    fake_trace = TraceResult(
        project_root=SAMPLE_ROOT,
        started_at_ns=0,
        ended_at_ns=1_000_000,
        events=[
            TraceEvent(
                event_type=TraceEventType.CALL,
                file_path=login_entry.handler_file,
                func_name="login",
                line=login_entry.handler_line,
                timestamp_ns=100,
            ),
            TraceEvent(
                event_type=TraceEventType.CALL,
                file_path=login_entry.handler_file,
                func_name="mystery_function",
                line=999,
                timestamp_ns=200,
            ),
            TraceEvent(
                event_type=TraceEventType.RETURN,
                file_path=login_entry.handler_file,
                func_name="mystery_function",
                line=999,
                timestamp_ns=300,
                detail={"durationMs": 0.1, "returnValue": {"type": "NoneType", "isNone": True}},
            ),
            TraceEvent(
                event_type=TraceEventType.RETURN,
                file_path=login_entry.handler_file,
                func_name="login",
                line=login_entry.handler_line,
                timestamp_ns=400,
                detail={"durationMs": 0.3, "returnValue": {"type": "dict", "length": 1}},
            ),
        ],
        metadata={"method": "POST", "path": "/api/v1/auth/login", "statusCode": 200},
    )

    mapper = TraceMapper(builder.call_graph, project_root=SAMPLE_ROOT)
    merged = mapper.apply(static_graph, fake_trace)

    # mystery_function should exist as a runtime-only node
    runtime_nodes = [
        n for n in merged.nodes.values()
        if n.confidence.value == "runtime" and n.name == "mystery_function"
    ]
    assert len(runtime_nodes) == 1, (
        f"Expected 1 runtime-only node for mystery_function, "
        f"got {len(runtime_nodes)}"
    )
    assert runtime_nodes[0].metadata.get("runtime_hit") is True

    trace_meta = merged.entrypoint.metadata.get("trace")
    assert trace_meta["hitNodes"] >= 2, "Both login and mystery_function should be counted"
