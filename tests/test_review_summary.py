"""Tests for review summary generation."""
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


class TestReviewSummaryStructure:
    def test_summary_exists(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI
            app = FastAPI()
            @app.get("/test")
            def handler():
                return {"ok": True}
        """})
        d = _build(tmp_path, "GET", "/test")
        summary = d["entrypoint"]["metadata"].get("review_summary")
        assert summary is not None
        assert "concerns" in summary
        assert "focusAreas" in summary
        assert "totalFunctions" in summary
        assert "signalCoverage" in summary

    def test_empty_concerns_for_simple_handler(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI
            app = FastAPI()
            @app.get("/health")
            def handler():
                return {"status": "ok"}
        """})
        d = _build(tmp_path, "GET", "/health")
        summary = d["entrypoint"]["metadata"]["review_summary"]
        assert len(summary["concerns"]) == 0
        assert len(summary["focusAreas"]) == 0


class TestConcernDetection:
    def test_raises_concern(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI, HTTPException
            app = FastAPI()
            @app.get("/test")
            def handler():
                raise HTTPException(status_code=400)
        """})
        d = _build(tmp_path, "GET", "/test")
        summary = d["entrypoint"]["metadata"]["review_summary"]
        concern_signals = [c["signal"] for c in summary["concerns"]]
        assert "raises_4xx" in concern_signals

    def test_error_paths_concern(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI, HTTPException
            app = FastAPI()
            @app.get("/test")
            def handler(x):
                if not x:
                    raise HTTPException(status_code=404)
                return x
        """})
        d = _build(tmp_path, "GET", "/test")
        summary = d["entrypoint"]["metadata"]["review_summary"]
        concern_signals = [c["signal"] for c in summary["concerns"]]
        assert "error_paths" in concern_signals

    def test_concern_severity(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI, HTTPException
            app = FastAPI()
            @app.get("/test")
            def handler():
                raise HTTPException(status_code=500)
        """})
        d = _build(tmp_path, "GET", "/test")
        summary = d["entrypoint"]["metadata"]["review_summary"]
        for c in summary["concerns"]:
            if c["signal"] == "raises_5xx":
                assert c["severity"] == "high"


class TestFocusAreas:
    def test_focus_areas_sorted_by_risk(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI, HTTPException
            app = FastAPI()
            @app.get("/test")
            def handler():
                raise HTTPException(status_code=500)
                raise HTTPException(status_code=400)
        """})
        d = _build(tmp_path, "GET", "/test")
        summary = d["entrypoint"]["metadata"]["review_summary"]
        areas = summary["focusAreas"]
        if len(areas) >= 2:
            assert areas[0]["score"] >= areas[1]["score"]

    def test_focus_area_has_phase(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI, HTTPException
            app = FastAPI()
            @app.get("/test")
            def handler():
                raise HTTPException(status_code=500)
        """})
        d = _build(tmp_path, "GET", "/test")
        areas = d["entrypoint"]["metadata"]["review_summary"]["focusAreas"]
        for area in areas:
            assert "name" in area
            assert "score" in area
            assert "level" in area
            assert "phase" in area
