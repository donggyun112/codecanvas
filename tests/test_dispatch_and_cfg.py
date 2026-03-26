"""Tests for attribute dispatch, nested control flow, and chain call improvements."""
from __future__ import annotations

import textwrap
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from codecanvas.graph.builder import FlowGraphBuilder
from codecanvas.graph.models import EdgeType, NodeType
from codecanvas.parser.call_graph import CallGraphBuilder


def _write_files(project_root: Path, files: dict[str, str]) -> None:
    for relative_path, content in files.items():
        target = project_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def _build_flow(project_root: Path, method: str, path: str):
    builder = FlowGraphBuilder(str(project_root))
    entry = next(
        entry for entry in builder.get_endpoints()
        if entry.method == method and entry.path == path
    )
    return builder.build_flow(entry)


# -----------------------------------------------------------------------
# 1. Attribute / dynamic dispatch: self.attr.attr.method() chain
# -----------------------------------------------------------------------

class TestSelfChainResolution:
    """self.repo.session.method() should follow the chain."""

    def test_self_two_level_chain(self, tmp_path: Path) -> None:
        _write_files(
            tmp_path,
            {
                "app.py": """
                    from fastapi import FastAPI, Depends

                    app = FastAPI()

                    class DbSession:
                        def execute_query(self, q: str) -> list:
                            return []

                    class UserRepo:
                        def __init__(self):
                            self.session = DbSession()

                        def find_user(self, uid: str):
                            return self.session.execute_query(f"SELECT * WHERE id={uid}")

                    class UserService:
                        def __init__(self):
                            self.repo = UserRepo()

                        def get_user(self, uid: str):
                            return self.repo.find_user(uid)

                    def get_service():
                        return UserService()

                    @app.get("/users/{uid}")
                    async def get_user(uid: str, svc: UserService = Depends(get_service)):
                        return svc.get_user(uid)
                """,
            },
        )

        flow = _build_flow(tmp_path, "GET", "/users/{uid}")

        # svc.get_user() should resolve to UserService.get_user
        assert "app.UserService.get_user" in flow.nodes
        # self.repo.find_user() should resolve to UserRepo.find_user
        assert "app.UserRepo.find_user" in flow.nodes
        # self.session.execute_query() should resolve to DbSession.execute_query
        assert "app.DbSession.execute_query" in flow.nodes

    def test_local_var_chain_follows_type(self, tmp_path: Path) -> None:
        """local_var.attr.method() should follow the chain via param annotation."""
        _write_files(
            tmp_path,
            {
                "app.py": """
                    from fastapi import FastAPI, Depends

                    app = FastAPI()

                    class Engine:
                        def run(self, cmd: str) -> str:
                            return cmd

                    class Processor:
                        def __init__(self):
                            self.engine = Engine()

                        def process(self, data: str) -> str:
                            return self.engine.run(data)

                    def get_proc():
                        return Processor()

                    @app.post("/process")
                    async def handle(proc: Processor = Depends(get_proc)):
                        return proc.process("data")
                """,
            },
        )

        flow = _build_flow(tmp_path, "POST", "/process")

        assert "app.Processor.process" in flow.nodes
        assert "app.Engine.run" in flow.nodes


# -----------------------------------------------------------------------
# 2. DI / Protocol resolution fallback
# -----------------------------------------------------------------------

class TestDIProtocolFallback:
    """When protocol method not found, fall back to concrete implementation."""

    def test_protocol_method_resolves_to_protocol_first(self, tmp_path: Path) -> None:
        """Protocol method should resolve to the protocol type, preserving BINDS edges."""
        _write_files(
            tmp_path,
            {
                "app.py": """
                    from typing import Protocol
                    from fastapi import Depends, FastAPI

                    app = FastAPI()

                    class CachePort(Protocol):
                        async def get_value(self, key: str) -> str | None: ...

                    class RedisCache(CachePort):
                        async def get_value(self, key: str) -> str | None:
                            return "cached"

                    async def get_cache() -> CachePort:
                        return RedisCache()

                    @app.get("/cache/{key}")
                    async def read_cache(key: str, cache: CachePort = Depends(get_cache)):
                        return await cache.get_value(key)
                """,
            },
        )

        flow = _build_flow(tmp_path, "GET", "/cache/{key}")

        # Protocol method node should exist
        assert "app.CachePort.get_value" in flow.nodes

    def test_concrete_fallback_when_protocol_method_missing(self, tmp_path: Path) -> None:
        """If protocol has no method def, fall back to concrete implementation."""
        _write_files(
            tmp_path,
            {
                "app.py": """
                    from typing import Protocol
                    from fastapi import Depends, FastAPI

                    app = FastAPI()

                    class StoragePort(Protocol):
                        pass

                    class FileStorage(StoragePort):
                        def save(self, data: bytes) -> str:
                            return "saved"

                    async def get_storage() -> StoragePort:
                        return FileStorage()

                    @app.post("/upload")
                    async def upload(storage: StoragePort = Depends(get_storage)):
                        return storage.save(b"data")
                """,
            },
        )

        flow = _build_flow(tmp_path, "POST", "/upload")

        # Since StoragePort has no save(), should fall back to FileStorage.save()
        assert "app.FileStorage.save" in flow.nodes


# -----------------------------------------------------------------------
# 3. Nested control flow in L4 logic steps
# -----------------------------------------------------------------------

class TestNestedControlFlowL4:
    """for/while loop bodies should be flattened into L4 steps."""

    def test_for_loop_body_flattened(self, tmp_path: Path) -> None:
        _write_files(
            tmp_path,
            {
                "app.py": """
                    from fastapi import FastAPI

                    app = FastAPI()

                    def process_item(item: dict) -> dict:
                        return {"processed": item}

                    @app.post("/batch")
                    async def batch_process(items: list):
                        results = []
                        for item in items:
                            processed = process_item(item)
                            results.append(processed)
                        return results
                """,
            },
        )

        flow = _build_flow(tmp_path, "POST", "/batch")

        # Should have a LOOP node for the for loop
        loop_nodes = [
            n for n in flow.nodes.values()
            if n.node_type == NodeType.LOOP and n.level == 4
        ]
        assert len(loop_nodes) >= 1
        loop = loop_nodes[0]
        assert "for" in loop.display_name.lower()

        # Inner assignment should appear as L4 step with loop_id
        inner_steps = [
            n for n in flow.nodes.values()
            if n.level == 4
            and n.metadata.get("loop_path") == "body"
        ]
        assert len(inner_steps) >= 1

    def test_while_loop_body_flattened(self, tmp_path: Path) -> None:
        _write_files(
            tmp_path,
            {
                "app.py": """
                    from fastapi import FastAPI

                    app = FastAPI()

                    def fetch_page(cursor: int) -> tuple:
                        return ([], None)

                    @app.get("/paginated")
                    async def paginated():
                        all_items = []
                        cursor = 0
                        while cursor is not None:
                            items, cursor = fetch_page(cursor)
                            all_items.extend(items)
                        return all_items
                """,
            },
        )

        flow = _build_flow(tmp_path, "GET", "/paginated")

        loop_nodes = [
            n for n in flow.nodes.values()
            if n.node_type == NodeType.LOOP and n.level == 4
        ]
        assert len(loop_nodes) >= 1
        assert "while" in loop_nodes[0].display_name.lower()

    def test_nested_if_inside_for_flattened(self, tmp_path: Path) -> None:
        """if inside for should produce both LOOP and BRANCH L4 nodes."""
        _write_files(
            tmp_path,
            {
                "app.py": """
                    from fastapi import FastAPI

                    app = FastAPI()

                    def validate(item: dict) -> bool:
                        return True

                    def save(item: dict) -> None:
                        pass

                    @app.post("/ingest")
                    async def ingest(items: list):
                        saved = 0
                        for item in items:
                            if validate(item):
                                save(item)
                                saved += 1
                        return {"saved": saved}
                """,
            },
        )

        flow = _build_flow(tmp_path, "POST", "/ingest")

        loop_nodes = [
            n for n in flow.nodes.values()
            if n.node_type == NodeType.LOOP and n.level == 4
        ]
        branch_nodes = [
            n for n in flow.nodes.values()
            if n.node_type == NodeType.BRANCH
            and n.level == 4
            and n.metadata.get("loop_path") == "body"
        ]
        assert len(loop_nodes) >= 1
        assert len(branch_nodes) >= 1


# -----------------------------------------------------------------------
# 4. DB / HTTP chain call decomposition
# -----------------------------------------------------------------------

class TestChainCallDecomposition:
    """Supabase-style and HTTP chain patterns should be detected."""

    def test_supabase_table_insert_execute(self, tmp_path: Path) -> None:
        _write_files(
            tmp_path,
            {
                "app.py": """
                    from fastapi import FastAPI

                    app = FastAPI()

                    class SupabaseClient:
                        def table(self, name: str):
                            return self
                        def insert(self, data: dict):
                            return self
                        def execute(self):
                            return {}

                    supabase = SupabaseClient()

                    @app.post("/items")
                    async def create_item(data: dict):
                        result = supabase.table("items").insert(data).execute()
                        return result
                """,
            },
        )

        cg = CallGraphBuilder(str(tmp_path))
        cg.analyze_project()

        handler = cg._find_function("create_item", str(tmp_path / "app.py"))
        assert handler is not None

        # The chain call should be detected as DB
        db_calls = [c for c in handler.calls if c.is_db_call]
        assert len(db_calls) >= 1
        db_call = db_calls[0]
        assert db_call.db_detail is not None
        assert db_call.db_detail.get("table") == "items"

    def test_supabase_from_select_execute(self, tmp_path: Path) -> None:
        _write_files(
            tmp_path,
            {
                "app.py": """
                    from fastapi import FastAPI

                    app = FastAPI()

                    class Client:
                        def from_(self, name: str):
                            return self
                        def select(self, cols: str):
                            return self
                        def execute(self):
                            return {}

                    supabase = Client()

                    @app.get("/items")
                    async def list_items():
                        return supabase.from_("items").select("*").execute()
                """,
            },
        )

        cg = CallGraphBuilder(str(tmp_path))
        cg.analyze_project()

        handler = cg._find_function("list_items", str(tmp_path / "app.py"))
        assert handler is not None

        db_calls = [c for c in handler.calls if c.is_db_call]
        assert len(db_calls) >= 1
        db_call = db_calls[0]
        assert db_call.db_detail is not None
        assert db_call.db_detail.get("table") == "items"

    def test_http_chain_with_headers(self, tmp_path: Path) -> None:
        """HTTP call through chain: client.headers({...}).get(url)."""
        _write_files(
            tmp_path,
            {
                "app.py": """
                    from fastapi import FastAPI

                    app = FastAPI()

                    class HttpClient:
                        def headers(self, h: dict):
                            return self
                        def get(self, url: str):
                            return {}

                    client = HttpClient()

                    @app.get("/proxy")
                    async def proxy():
                        return client.headers({"Auth": "token"}).get("/external")
                """,
            },
        )

        cg = CallGraphBuilder(str(tmp_path))
        cg.analyze_project()

        handler = cg._find_function("proxy", str(tmp_path / "app.py"))
        assert handler is not None

        http_calls = [c for c in handler.calls if c.is_http_call]
        assert len(http_calls) >= 1

    def test_sqlalchemy_select_top_level(self, tmp_path: Path) -> None:
        """Top-level select(User) should be detected as DB call."""
        _write_files(
            tmp_path,
            {
                "app.py": """
                    from fastapi import FastAPI

                    app = FastAPI()

                    class User:
                        pass

                    def select(model):
                        pass

                    @app.get("/users")
                    async def get_users():
                        stmt = select(User)
                        return stmt
                """,
            },
        )

        cg = CallGraphBuilder(str(tmp_path))
        cg.analyze_project()

        handler = cg._find_function("get_users", str(tmp_path / "app.py"))
        assert handler is not None

        db_calls = [c for c in handler.calls if c.is_db_call]
        assert len(db_calls) >= 1


# -----------------------------------------------------------------------
# 5. CFG correctness for nested structures
# -----------------------------------------------------------------------

class TestCFGNestedStructures:
    """CFG builder handles nested if/for/while/try correctly."""

    def test_if_inside_for_has_proper_edges(self, tmp_path: Path) -> None:
        """if inside for loop should produce true/false edges within loop body."""
        _write_files(
            tmp_path,
            {
                "app.py": """
                    from fastapi import FastAPI
                    app = FastAPI()

                    @app.get("/test")
                    async def handler():
                        results = []
                        for i in range(10):
                            if i % 2 == 0:
                                results.append(i)
                            else:
                                pass
                        return results
                """,
            },
        )

        from codecanvas.graph.cfg import CFGBuilder

        cg = CallGraphBuilder(str(tmp_path))
        cg.analyze_project()
        cfg_builder = CFGBuilder(cg)
        cfg = cfg_builder.build("handler", str(tmp_path / "app.py"))

        # Should have entry, loop header, branch test, true/false bodies,
        # merge, back_edge, exit
        assert len(cfg.blocks) >= 5

        # Should have back_edge
        back_edges = [e for e in cfg.edges if e.kind == "back_edge"]
        assert len(back_edges) >= 1

        # Should have true and false edges for the if
        true_edges = [e for e in cfg.edges if e.kind == "true"]
        false_edges = [e for e in cfg.edges if e.kind == "false"]
        assert len(true_edges) >= 1
        assert len(false_edges) >= 1

    def test_try_inside_for_has_exception_edges(self, tmp_path: Path) -> None:
        _write_files(
            tmp_path,
            {
                "app.py": """
                    from fastapi import FastAPI
                    app = FastAPI()

                    def risky(x):
                        return x

                    @app.get("/test2")
                    async def handler2():
                        results = []
                        for item in [1, 2, 3]:
                            try:
                                result = risky(item)
                                results.append(result)
                            except ValueError:
                                pass
                        return results
                """,
            },
        )

        from codecanvas.graph.cfg import CFGBuilder

        cg = CallGraphBuilder(str(tmp_path))
        cg.analyze_project()
        cfg_builder = CFGBuilder(cg)
        cfg = cfg_builder.build("handler2", str(tmp_path / "app.py"))

        exception_edges = [e for e in cfg.edges if e.kind == "exception"]
        assert len(exception_edges) >= 1

        back_edges = [e for e in cfg.edges if e.kind == "back_edge"]
        assert len(back_edges) >= 1
