"""Tests for change impact analysis: diff parsing, function mapping, reachability."""
from __future__ import annotations
import sys, textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from codecanvas.graph.impact import (
    parse_unified_diff, ImpactAnalyzer, ChangedHunk,
)
from codecanvas.graph.builder import FlowGraphBuilder


def _write_files(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


class TestDiffParsing:
    def test_single_hunk(self):
        diff = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -10,3 +10,5 @@
 line1
+added
 line2
"""
        hunks = parse_unified_diff(diff)
        assert len(hunks) == 1
        assert hunks[0].file_path == "app.py"
        assert hunks[0].start_line == 10
        assert hunks[0].end_line == 14

    def test_multi_file(self):
        diff = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -5,1 +5,2 @@
+new
diff --git a/b.py b/b.py
--- a/b.py
+++ b/b.py
@@ -20,1 +20,3 @@
+new1
+new2
"""
        hunks = parse_unified_diff(diff)
        assert len(hunks) == 2
        assert hunks[0].file_path == "a.py"
        assert hunks[1].file_path == "b.py"

    def test_non_python_filtered(self):
        diff = """diff --git a/style.css b/style.css
--- a/style.css
+++ b/style.css
@@ -1,1 +1,2 @@
+.new {}
"""
        hunks = parse_unified_diff(diff)
        assert len(hunks) == 0

    def test_empty_diff(self):
        assert parse_unified_diff("") == []


class TestFunctionMapping:
    def test_overlap_found(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI
            app = FastAPI()
            @app.get("/test")
            def handler():
                x = compute()
                return x

            def compute():
                return 42
        """})
        b = FlowGraphBuilder(str(tmp_path))
        eps = b.get_entrypoints()
        analyzer = ImpactAnalyzer(b.call_graph, str(tmp_path), entrypoints=eps)
        funcs = analyzer._find_functions_at("app.py", 6, 6)
        names = [f.name for f in funcs]
        assert "handler" in names

    def test_no_overlap(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI
            app = FastAPI()
            @app.get("/test")
            def handler():
                return 1

            def unrelated():
                return 2
        """})
        b = FlowGraphBuilder(str(tmp_path))
        analyzer = ImpactAnalyzer(b.call_graph, str(tmp_path))
        funcs = analyzer._find_functions_at("app.py", 9, 10)
        names = [f.name for f in funcs]
        assert "handler" not in names


class TestReachability:
    def test_transitive_reachability(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI
            app = FastAPI()
            @app.get("/test")
            def handler():
                return service()

            def service():
                return repo()

            def repo():
                return 42
        """})
        b = FlowGraphBuilder(str(tmp_path))
        eps = b.get_entrypoints()
        analyzer = ImpactAnalyzer(b.call_graph, str(tmp_path), entrypoints=eps)
        handler_func = b.call_graph._find_function("handler", None, None)
        reachable = analyzer._find_reachable(handler_func.qualified_name)
        names = {qn.split(".")[-1] for qn in reachable}
        assert "handler" in names
        assert "service" in names
        assert "repo" in names

    def test_cycle_safe(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI
            app = FastAPI()
            @app.get("/test")
            def handler():
                return a()

            def a():
                return b()

            def b():
                return a()
        """})
        b = FlowGraphBuilder(str(tmp_path))
        eps = b.get_entrypoints()
        analyzer = ImpactAnalyzer(b.call_graph, str(tmp_path), entrypoints=eps)
        handler_func = b.call_graph._find_function("handler", None, None)
        reachable = analyzer._find_reachable(handler_func.qualified_name)
        # Should not hang — cycle is handled by visited set
        assert len(reachable) >= 3


class TestEndpointImpact:
    def test_affected_endpoint_detected(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI
            app = FastAPI()
            @app.get("/test")
            def handler():
                return helper()

            def helper():
                return 42
        """})
        b = FlowGraphBuilder(str(tmp_path))
        eps = b.get_entrypoints()
        b.call_graph.analyze_project()
        helper_func = next((f for f in b.call_graph._functions.values() if f.name == "helper"), None)
        assert helper_func, "helper function must be found"
        line = helper_func.line_start
        diff = f"""diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -{line},1 +{line},2 @@
+    audit()
"""
        analyzer = ImpactAnalyzer(b.call_graph, str(tmp_path), entrypoints=eps)
        result = analyzer.analyze_diff(diff)
        assert len(result.affected_endpoints) >= 1
        assert result.affected_endpoints[0].path == "/test"

    def test_unaffected_endpoint_not_included(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI
            app = FastAPI()
            @app.get("/a")
            def handler_a():
                return helper_a()
            @app.get("/b")
            def handler_b():
                return helper_b()

            def helper_a():
                return 1
            def helper_b():
                return 2
        """})
        b = FlowGraphBuilder(str(tmp_path))
        eps = b.get_entrypoints()
        b.call_graph.analyze_project()
        helper_a = next((f for f in b.call_graph._functions.values() if f.name == "helper_a"), None)
        assert helper_a, "helper_a must be found"
        line = helper_a.line_start
        diff = f"""diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -{line},1 +{line},2 @@
+    audit()
"""
        analyzer = ImpactAnalyzer(b.call_graph, str(tmp_path), entrypoints=eps)
        result = analyzer.analyze_diff(diff)
        paths = [e.path for e in result.affected_endpoints]
        assert "/a" in paths
        assert "/b" not in paths
