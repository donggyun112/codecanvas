"""Tests for opening CodeCanvas flow from a file/line location."""
from __future__ import annotations

from pathlib import Path
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from codecanvas.graph.builder import FlowGraphBuilder


def _write_files(project_root: Path, files: dict[str, str]) -> None:
    for relative_path, content in files.items():
        target = project_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def test_locate_service_method_inside_fastapi_project() -> None:
    project_root = ROOT / "sample-fastapi"
    builder = FlowGraphBuilder(str(project_root))
    builder.get_entrypoints()

    entry = builder.entrypoint_extractor.locate_function_entrypoint(
        str(project_root / "app/services/auth_service.py"),
        14,
    )

    assert entry is not None
    assert entry.kind == "function"
    assert entry.handler_name == "verify_user"
    assert entry.metadata["qualname"] == "AuthService.verify_user"

    flow = builder.build_flow(entry)
    assert "app.repositories.user_repo.UserRepository.find_by_email" in flow.nodes


def test_locate_route_function_from_decorator_line() -> None:
    project_root = ROOT / "sample-fastapi"
    builder = FlowGraphBuilder(str(project_root))
    builder.get_entrypoints()

    entry = builder.entrypoint_extractor.locate_function_entrypoint(
        str(project_root / "app/routers/auth.py"),
        13,
    )

    assert entry is not None
    assert entry.kind == "function"
    assert entry.handler_name == "login"
    assert entry.metadata["qualname"] == "login"

    flow = builder.build_flow(entry)
    assert any(node.name == "verify_user" for node in flow.nodes.values())
    focused = [node.id for node in flow.nodes.values() if node.metadata.get("context_root")]
    assert focused == ["app.routers.auth.login"]


def test_function_flow_includes_upstream_callers_to_two_depths(tmp_path: Path) -> None:
    _write_files(
        tmp_path,
        {
            "helpers.py": """
                def normalize(value: str):
                    return value.strip().lower()
            """,
            "service.py": """
                from helpers import normalize

                def save_user(value: str):
                    cleaned = normalize(value)
                    return {"value": cleaned}
            """,
            "routes.py": """
                from service import save_user

                def create_user(value: str):
                    return save_user(value)

                def admin_create(value: str):
                    return create_user(value)

                def import_user(value: str):
                    return save_user(value)
            """,
        },
    )

    builder = FlowGraphBuilder(str(tmp_path))
    builder.get_entrypoints()

    entry = builder.entrypoint_extractor.locate_function_entrypoint(
        str(tmp_path / "service.py"),
        4,
    )

    assert entry is not None
    flow = builder.build_flow(entry)

    assert "routes.create_user" in flow.nodes
    assert "routes.admin_create" in flow.nodes
    assert "routes.import_user" in flow.nodes
    assert flow.nodes["service.save_user"].metadata.get("context_root") is True
    assert flow.nodes["service.save_user"].metadata.get("downstream_distance") == 0
    assert flow.nodes["routes.create_user"].metadata.get("upstream_distance") == 1
    assert flow.nodes["routes.import_user"].metadata.get("upstream_distance") == 1
    assert flow.nodes["routes.admin_create"].metadata.get("upstream_distance") == 2

    assert any(
        edge.source_id == "routes.create_user"
        and edge.target_id == "service.save_user"
        and edge.metadata.get("upstream_edge")
        for edge in flow.edges
    )
    assert any(
        edge.source_id == "routes.import_user"
        and edge.target_id == "service.save_user"
        and edge.metadata.get("upstream_edge")
        for edge in flow.edges
    )
    assert any(
        edge.source_id == "routes.admin_create"
        and edge.target_id == "routes.create_user"
        and edge.metadata.get("upstream_edge")
        for edge in flow.edges
    )
    assert not any(
        edge.source_id == "entrypoint"
        and edge.target_id == "service.save_user"
        for edge in flow.edges
    )


def test_api_flow_does_not_mark_route_or_dependency_as_context_root() -> None:
    project_root = ROOT / "sample-fastapi"
    builder = FlowGraphBuilder(str(project_root))
    entry = next(
        entry for entry in builder.get_endpoints()
        if entry.method == "GET" and entry.path == "/api/v1/users/me"
    )

    flow = builder.build_flow(entry)
    focused = [node.id for node in flow.nodes.values() if node.metadata.get("context_root")]
    assert focused == []


def test_function_flow_includes_reference_based_upstream_context(tmp_path: Path) -> None:
    _write_files(
        tmp_path,
        {
            "graph.py": """
                class Graph:
                    def add_node(self, name, fn):
                        return fn
            """,
            "builder.py": """
                from graph import Graph

                def build_graph():
                    graph = Graph()

                    def supervisor_node(state):
                        return state

                    graph.add_node("supervisor", supervisor_node)
                    return graph
            """,
        },
    )

    builder = FlowGraphBuilder(str(tmp_path))
    builder.get_entrypoints()
    entry = builder.entrypoint_extractor.locate_function_entrypoint(
        str(tmp_path / "builder.py"),
        6,
    )

    assert entry is not None
    flow = builder.build_flow(entry)
    assert "builder.build_graph" in flow.nodes
    assert flow.nodes["builder.build_graph"].metadata.get("upstream_distance") == 1
    assert any(
        edge.source_id == "builder.build_graph"
        and edge.target_id == "builder.build_graph.supervisor_node"
        and edge.metadata.get("upstream_relation") == "reference"
        for edge in flow.edges
    )


def test_function_flow_includes_depends_based_route_callers(tmp_path: Path) -> None:
    _write_files(
        tmp_path,
        {
            "app.py": """
                from fastapi import Depends, FastAPI

                app = FastAPI()


                async def get_user_scoped_client():
                    return {"scoped": True}


                @app.get("/me")
                async def get_me(client = Depends(get_user_scoped_client)):
                    return client
            """,
        },
    )

    builder = FlowGraphBuilder(str(tmp_path))
    builder.get_entrypoints()
    entry = builder.entrypoint_extractor.locate_function_entrypoint(
        str(tmp_path / "app.py"),
        6,
    )

    assert entry is not None
    flow = builder.build_flow(entry)

    assert "app.get_me" in flow.nodes
    assert flow.nodes["app.get_me"].metadata.get("upstream_distance") == 1
    assert any(
        edge.source_id == "app.get_me"
        and edge.target_id == "app.get_user_scoped_client"
        and edge.metadata.get("upstream_relation") == "dependency"
        and edge.metadata.get("upstream_edge")
        for edge in flow.edges
    )
