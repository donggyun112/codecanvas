"""Regression tests for call graph resolution heuristics."""
from __future__ import annotations

import textwrap
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from codecanvas.graph.builder import FlowGraphBuilder
from codecanvas.graph.models import EdgeType, NodeType


def _build_flow(project_root: Path, method: str, path: str):
    builder = FlowGraphBuilder(str(project_root))
    entry = next(
        entry for entry in builder.get_endpoints()
        if entry.method == method and entry.path == path
    )
    return builder.build_flow(entry)


def _write_files(project_root: Path, files: dict[str, str]) -> None:
    for relative_path, content in files.items():
        target = project_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def test_sample_fastapi_keeps_service_and_repository_resolution() -> None:
    flow = _build_flow(ROOT / "sample-fastapi", "POST", "/api/v1/auth/login")

    assert "app.services.auth_service.AuthService.verify_user" in flow.nodes
    assert "app.services.auth_service.AuthService.issue_tokens" in flow.nodes
    assert "app.repositories.user_repo.UserRepository.find_by_email" in flow.nodes
    assert "app.repositories.token_repo.TokenRepository.store_refresh_token" in flow.nodes


def test_db_method_chains_do_not_bind_to_unrelated_execute_methods(tmp_path: Path) -> None:
    _write_files(
        tmp_path,
        {
            "app.py": """
                from fastapi import FastAPI
                from routes import router

                app = FastAPI()
                app.include_router(router)
            """,
            "routes.py": """
                from fastapi import APIRouter

                router = APIRouter()


                class WebSearchWorker:
                    async def execute(self, query: str):
                        return {"query": query}


                @router.post("/sessions")
                async def create_session(client):
                    await client.table("chat_sessions").insert({"id": "x"}).execute()
                    return {"ok": True}
            """,
        },
    )

    flow = _build_flow(tmp_path, "POST", "/sessions")

    assert "routes.WebSearchWorker.execute" not in flow.nodes
    assert "unresolved.client.table.insert.execute" in flow.nodes
    assert flow.nodes["unresolved.client.table.insert.execute"].node_type == NodeType.DATABASE
    assert any(
        edge.source_id == "routes.create_session"
        and edge.target_id == "unresolved.client.table.insert.execute"
        and edge.edge_type == EdgeType.QUERIES
        for edge in flow.edges
    )


def test_level4_logic_steps_describe_function_body() -> None:
    flow = _build_flow(ROOT / "sample-fastapi", "POST", "/api/v1/auth/login")

    logic_nodes = [node for node in flow.nodes.values() if node.level == 4]

    assert any(
        node.node_type == NodeType.ASSIGNMENT
        and "service = AuthService(db)" in node.display_name
        for node in logic_nodes
    )
    assert any(
        node.node_type == NodeType.BRANCH
        and node.metadata.get("condition") == "user is None"
        for node in logic_nodes
    )
    assert any(
        node.node_type == NodeType.RETURN
        and "LoginResponse" in node.display_name
        for node in logic_nodes
    )


def test_level4_logic_steps_include_loop_and_return_for_script_entrypoint() -> None:
    builder = FlowGraphBuilder(str(ROOT / "sample-script"))
    entry = next(entry for entry in builder.get_entrypoints() if entry.kind == "script")
    flow = builder.build_flow(entry)

    logic_nodes = [node for node in flow.nodes.values() if node.level == 4]

    assert any(
        node.node_type == NodeType.LOOP and "for item in items" in node.display_name
        for node in logic_nodes
    )
    assert any(
        node.node_type == NodeType.RETURN and "write_report(summary)" in node.display_name
        for node in logic_nodes
    )


def test_async_for_stream_source_is_marked_on_edges_and_loop_steps(tmp_path: Path) -> None:
    _write_files(
        tmp_path,
        {
            "app.py": """
                from fastapi import FastAPI

                app = FastAPI()


                class Supervisor:
                    async def process_stream(self, prompt: str):
                        yield prompt


                @app.get("/stream")
                async def stream_prompt():
                    supervisor = Supervisor()
                    async for event in supervisor.process_stream("hello"):
                        if event:
                            return {"event": event}
                    return {"event": None}
            """,
        },
    )

    flow = _build_flow(tmp_path, "GET", "/stream")

    stream_edge = next(
        edge for edge in flow.edges
        if edge.source_id == "app.stream_prompt"
        and edge.target_id == "app.Supervisor.process_stream"
    )
    stream_loop = next(
        node for node in flow.nodes.values()
        if node.node_type == NodeType.LOOP
        and node.metadata.get("loop_kind") == "async_for"
    )

    assert stream_edge.label == "async stream"
    assert stream_edge.metadata.get("iteration_kind") == "async_for"
    assert stream_edge.metadata.get("call_kind") == "async_stream"
    assert stream_loop.metadata.get("iterator_call") == "supervisor.process_stream"
    assert "async for event in supervisor.process_stream" in stream_loop.display_name


def test_dependency_flow_surfaces_injected_user_type_and_constructor_calls(tmp_path: Path) -> None:
    _write_files(
        tmp_path,
        {
            "app.py": """
                from fastapi import Depends, FastAPI

                app = FastAPI()


                class UserIdentity:
                    def __init__(self, sub: str):
                        self.sub = sub


                class User:
                    def __init__(self, identity: UserIdentity):
                        self.identity = identity


                async def verify_current_user() -> User:
                    identity = UserIdentity("abc")
                    user = User(identity)
                    return user


                @app.get("/me")
                async def get_me(current_user: User = Depends(verify_current_user)):
                    return current_user
            """,
        },
    )

    flow = _build_flow(tmp_path, "GET", "/me")

    constructor_edges = [
        edge for edge in flow.edges
        if edge.source_id == "app.verify_current_user"
        and edge.label == "constructs"
    ]
    dependency_edge = next(
        edge for edge in flow.edges
        if edge.source_id == "app.verify_current_user"
        and edge.target_id == "app.get_me"
        and edge.edge_type == EdgeType.INJECTS
    )

    assert any(edge.target_id == "app.UserIdentity" for edge in constructor_edges)
    assert any(edge.target_id == "app.User" for edge in constructor_edges)
    assert dependency_edge.label == "injects current_user: User"
    assert dependency_edge.metadata.get("dependency_param") == "current_user"
    assert dependency_edge.metadata.get("dependency_type") == "User"


def test_raise_inside_branch_routes_from_branch_node() -> None:
    flow = _build_flow(ROOT / "sample-fastapi", "POST", "/api/v1/auth/login")

    # Find the L4 branch node for "user is None" scoped to the login handler
    login_node = next(
        node for node in flow.nodes.values()
        if node.name == "login" and node.level == 3
    )
    branch_node = next(
        node for node in flow.nodes.values()
        if node.node_type == NodeType.BRANCH
        and node.metadata.get("condition") == "user is None"
        and node.metadata.get("function_id") == login_node.id
        and node.level == 4
    )
    # The RAISES edge must originate from the branch node, not the function node
    raise_edges = [
        edge for edge in flow.edges
        if edge.edge_type == EdgeType.RAISES
        and edge.condition == "user is None"
        and edge.target_id.startswith("error.")
    ]
    assert raise_edges, "Expected at least one RAISES edge with condition 'user is None'"
    assert all(
        edge.source_id == branch_node.id for edge in raise_edges
    ), f"Expected raise edge source={branch_node.id!r}, got {[e.source_id for e in raise_edges]}"


def test_raise_inside_elif_routes_from_elif_branch_node(tmp_path: Path) -> None:
    _write_files(
        tmp_path,
        {
            "app.py": """
                from fastapi import FastAPI
                from routes import router

                app = FastAPI()
                app.include_router(router)
            """,
            "routes.py": """
                from fastapi import APIRouter, HTTPException

                router = APIRouter()

                @router.post("/check")
                def check(x: int):
                    if x == 1:
                        return "one"
                    elif x == 2:
                        raise HTTPException(status_code=400, detail="bad two")
                    elif x == 3:
                        raise HTTPException(status_code=403, detail="bad three")
                    return "other"
            """,
        },
    )

    flow = _build_flow(tmp_path, "POST", "/check")

    branch_nodes = {
        node.metadata.get("condition"): node
        for node in flow.nodes.values()
        if node.node_type == NodeType.BRANCH and node.level == 4
    }
    assert "x == 1" in branch_nodes, f"Missing 'x == 1' branch, got: {list(branch_nodes)}"
    assert "x == 2" in branch_nodes, f"Missing 'x == 2' branch, got: {list(branch_nodes)}"
    assert "x == 3" in branch_nodes, f"Missing 'x == 3' branch, got: {list(branch_nodes)}"

    # elif x == 2 raise must route from x==2 branch node
    raise_400 = next(
        edge for edge in flow.edges
        if edge.edge_type == EdgeType.RAISES and edge.condition == "x == 2"
    )
    assert raise_400.source_id == branch_nodes["x == 2"].id

    # elif x == 3 raise must route from x==3 branch node
    raise_403 = next(
        edge for edge in flow.edges
        if edge.edge_type == EdgeType.RAISES and edge.condition == "x == 3"
    )
    assert raise_403.source_id == branch_nodes["x == 3"].id


def test_api_pipeline_edges_reflect_middleware_and_dependency_order() -> None:
    flow = _build_flow(ROOT / "sample-fastapi", "POST", "/api/v1/auth/login")

    handler = next(
        node for node in flow.nodes.values()
        if node.name == "login" and node.level == 3
    )
    dependency = next(
        node for node in flow.nodes.values()
        if node.node_type == NodeType.DEPENDENCY and node.name == "get_db"
    )
    middleware_nodes = sorted(
        (node for node in flow.nodes.values() if node.node_type == NodeType.MIDDLEWARE),
        key=lambda node: node.line_start or 0,
    )
    last_middleware = middleware_nodes[-1]

    assert not any(
        edge.source_id == "api" and edge.target_id == handler.id
        for edge in flow.edges
    )
    assert any(
        edge.source_id == "api" and edge.target_id == middleware_nodes[0].id
        and edge.edge_type == EdgeType.MIDDLEWARE_CHAIN
        for edge in flow.edges
    )
    assert any(
        edge.source_id == last_middleware.id and edge.target_id == dependency.id
        and edge.edge_type == EdgeType.CALLS
        for edge in flow.edges
    )
    assert any(
        edge.source_id == dependency.id and edge.target_id == handler.id
        and edge.edge_type == EdgeType.INJECTS
        for edge in flow.edges
    )
    assert any(
        edge.source_id == dependency.id and edge.target_id == "layer.routers"
        and edge.edge_type == EdgeType.INJECTS
        for edge in flow.edges
    )
    assert not any(
        edge.source_id == "api" and edge.target_id == "layer.routers"
        for edge in flow.edges
    )
    assert dependency.metadata.get("pipeline_phase") == "dependency"
    assert handler.metadata.get("pipeline_phase") == "handler"
