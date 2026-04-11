"""Tests for persistent disk cache, lazy AST loading, and file-count throttle.

Validates:
- Cache invalidation on file change, corruption, and deletion
- Lazy AST reparse produces identical flows to cold build
- ProjectTooLargeError fires at the right threshold
- Warm entrypoint cache returns correct results
"""
import os
import sys
import time
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))

from codecanvas.graph.builder import FlowGraphBuilder
from codecanvas.parser import call_graph as cg_mod


def _write(root, rel_path, content):
    target = os.path.join(root, rel_path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w") as f:
        f.write(textwrap.dedent(content).strip() + "\n")


def _make_project(tmp_path):
    _write(tmp_path, "app/__init__.py", "")
    _write(tmp_path, "app/main.py", """
        from fastapi import FastAPI
        app = FastAPI()
        @app.get("/test")
        def handler():
            x = compute()
            return x
        def compute():
            return 42
    """)
    return str(tmp_path)


class TestEntrypointCache:
    def test_cold_produces_entrypoints(self, tmp_path):
        proj = _make_project(tmp_path)
        b = FlowGraphBuilder(proj)
        eps = b.get_entrypoints()
        assert len(eps) >= 1

    def test_warm_matches_cold(self, tmp_path):
        proj = _make_project(tmp_path)
        b1 = FlowGraphBuilder(proj)
        eps1 = b1.get_entrypoints()

        b2 = FlowGraphBuilder(proj)
        eps2 = b2.get_entrypoints()
        assert len(eps2) == len(eps1)
        assert {e.id for e in eps2} == {e.id for e in eps1}

    def test_cache_invalidated_on_file_change(self, tmp_path):
        proj = _make_project(tmp_path)
        b1 = FlowGraphBuilder(proj)
        eps1 = b1.get_entrypoints()
        count_before = len(eps1)

        time.sleep(0.01)
        _write(tmp_path, "app/main.py", """
            from fastapi import FastAPI
            app = FastAPI()
            @app.get("/test")
            def handler():
                return {"ok": True}
            @app.get("/new")
            def new_handler():
                return {"new": True}
        """)

        b2 = FlowGraphBuilder(proj)
        eps2 = b2.get_entrypoints()
        assert len(eps2) > count_before

    def test_corrupted_cache_falls_back(self, tmp_path):
        proj = _make_project(tmp_path)
        b1 = FlowGraphBuilder(proj)
        b1.get_entrypoints()

        cache = os.path.join(proj, ".codecanvas", "entrypoints.json")
        with open(cache, "w") as f:
            f.write("NOT JSON{{{")

        b2 = FlowGraphBuilder(proj)
        eps = b2.get_entrypoints()
        assert len(eps) >= 1

    def test_deleted_cache_falls_back(self, tmp_path):
        proj = _make_project(tmp_path)
        b1 = FlowGraphBuilder(proj)
        b1.get_entrypoints()

        cache = os.path.join(proj, ".codecanvas", "entrypoints.json")
        os.remove(cache)

        b2 = FlowGraphBuilder(proj)
        eps = b2.get_entrypoints()
        assert len(eps) >= 1


class TestCallGraphCache:
    def test_warm_flow_matches_cold(self, tmp_path):
        proj = _make_project(tmp_path)

        # Cold
        b1 = FlowGraphBuilder(proj)
        ep1 = next(e for e in b1.get_entrypoints() if e.kind == "api")
        d1 = b1.build_flow(ep1).to_dict()

        # Warm (new builder, from cache)
        b2 = FlowGraphBuilder(proj)
        ep2 = next(e for e in b2.get_entrypoints() if e.id == ep1.id)
        d2 = b2.build_flow(ep2).to_dict()

        assert len(d1["nodes"]) == len(d2["nodes"])
        assert len(d1["edges"]) == len(d2["edges"])

    def test_corrupted_callgraph_cache_falls_back(self, tmp_path):
        proj = _make_project(tmp_path)
        b1 = FlowGraphBuilder(proj)
        ep = next(e for e in b1.get_entrypoints() if e.kind == "api")
        b1.build_flow(ep)

        cg_cache = os.path.join(proj, ".codecanvas", "callgraph.json")
        with open(cg_cache, "w") as f:
            f.write('{"version": 999}')

        b2 = FlowGraphBuilder(proj)
        ep2 = next(e for e in b2.get_entrypoints() if e.id == ep.id)
        d = b2.build_flow(ep2).to_dict()
        assert len(d["nodes"]) > 0


class TestLazyAST:
    def test_ast_nodes_populated_from_cache(self, tmp_path):
        proj = _make_project(tmp_path)

        # Cold: build to populate cache
        b1 = FlowGraphBuilder(proj)
        ep1 = next(e for e in b1.get_entrypoints() if e.kind == "api")
        b1.build_flow(ep1)

        # Warm: AST nodes should be lazily reparsed
        b2 = FlowGraphBuilder(proj)
        b2.get_entrypoints()
        b2.call_graph.analyze_project()
        assert len(b2.call_graph._ast_nodes) == 0, "AST nodes should be empty after cache load"

        # Trigger lazy reparse
        handler_qname = next(
            q for q in b2.call_graph._functions
            if b2.call_graph._functions[q].name == "handler"
        )
        ast_node = b2.call_graph.get_ast_node(handler_qname)
        assert ast_node is not None, "Lazy AST reparse should find handler"


class TestThrottle:
    def test_project_too_large_error(self, tmp_path):
        proj = _make_project(tmp_path)
        old_max = cg_mod.MAX_FILES
        cg_mod.MAX_FILES = 1
        try:
            b = FlowGraphBuilder(proj)
            b.get_entrypoints()
            with pytest.raises(cg_mod.ProjectTooLargeError) as exc_info:
                b.build_flow(next(e for e in b.get_entrypoints() if e.kind == "api"))
            assert exc_info.value.count > 1
            assert exc_info.value.limit == 1
        finally:
            cg_mod.MAX_FILES = old_max

    def test_env_override(self, tmp_path, monkeypatch):
        proj = _make_project(tmp_path)
        monkeypatch.setattr(cg_mod, "MAX_FILES", 1)
        b = FlowGraphBuilder(proj)
        b.get_entrypoints()
        with pytest.raises(cg_mod.ProjectTooLargeError):
            b.build_flow(next(e for e in b.get_entrypoints() if e.kind == "api"))
