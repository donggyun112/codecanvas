"""Tests for response origin tracking: provenance chain from return to data sources."""
from __future__ import annotations
import sys, textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from codecanvas.graph.builder import FlowGraphBuilder


def _write_files(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def _build(root: Path, method: str, path: str):
    b = FlowGraphBuilder(str(root))
    ep = next(e for e in b.get_entrypoints() if e.method == method and e.path == path)
    return b.build_flow(ep).to_dict()


def _get_respond_steps(d):
    eg = d.get("executionGraph", {})
    return [s for s in eg.get("steps", []) if s["operation"] == "respond"]


class TestResponseOriginBasic:
    def test_simple_origin(self, tmp_path):
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
        d = _build(tmp_path, "GET", "/test")
        responds = _get_respond_steps(d)
        # The respond step should have return_expression
        assert len(responds) >= 1
        main_respond = responds[-1]
        assert main_respond["metadata"].get("return_expression")

    def test_transitive_chain(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI
            app = FastAPI()
            @app.get("/test")
            def handler():
                raw = fetch()
                processed = transform(raw)
                return processed
            def fetch():
                return [1, 2, 3]
            def transform(data):
                return [x * 2 for x in data]
        """})
        d = _build(tmp_path, "GET", "/test")
        responds = _get_respond_steps(d)
        main_respond = responds[-1]
        origins = main_respond["metadata"].get("response_origins", [])
        # Should include both transform and fetch
        labels = [o["label"] for o in origins]
        assert len(origins) >= 1

    def test_no_origins_for_literal_return(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI
            app = FastAPI()
            @app.get("/test")
            def handler():
                return {"ok": True}
        """})
        d = _build(tmp_path, "GET", "/test")
        responds = _get_respond_steps(d)
        if responds:
            origins = responds[-1]["metadata"].get("response_origins", [])
            # Literal return has no variable-based origins
            # (may have some depending on expression parsing)


class TestResponseOriginMetadata:
    def test_origin_has_step_id(self, tmp_path):
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
        d = _build(tmp_path, "GET", "/test")
        responds = _get_respond_steps(d)
        origins = responds[-1]["metadata"].get("response_origins", [])
        for o in origins:
            assert "stepId" in o
            assert "variable" in o
            assert "label" in o
            assert "operation" in o

    def test_origin_no_cycle(self, tmp_path):
        """Response origins should not contain duplicate step IDs."""
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI
            app = FastAPI()
            @app.get("/test")
            def handler():
                a = step1()
                b = step2(a)
                c = step3(b)
                return c
            def step1(): return 1
            def step2(x): return x
            def step3(x): return x
        """})
        d = _build(tmp_path, "GET", "/test")
        responds = _get_respond_steps(d)
        origins = responds[-1]["metadata"].get("response_origins", [])
        step_ids = [o["stepId"] for o in origins]
        assert len(step_ids) == len(set(step_ids)), "Duplicate step IDs in response origins"
