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
