"""Tests for risk scoring: signal points, phase multipliers, aggregation."""
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


class TestRiskSignalPoints:
    def test_raises_4xx_scores(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI, HTTPException
            app = FastAPI()
            @app.get("/test")
            def handler():
                raise HTTPException(status_code=400)
        """})
        d = _build(tmp_path, "GET", "/test")
        handler = next(n for n in d["nodes"].values()
                       if n.get("metadata", {}).get("pipeline_phase") == "handler" and n["level"] == 3)
        assert handler["metadata"].get("risk_score", 0) > 0
        assert "raises_4xx" in handler["metadata"].get("review_signals", [])

    def test_auth_signal_from_security_dep(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI, Depends, Security
            app = FastAPI()
            def get_token(token: str = Security(lambda: "t", scopes=["read"])):
                return token
            @app.get("/test")
            def handler(token=Depends(get_token)):
                return {"ok": True}
        """})
        d = _build(tmp_path, "GET", "/test")
        # Auth signal should propagate to handler
        handler = next(n for n in d["nodes"].values()
                       if n.get("metadata", {}).get("pipeline_phase") == "handler" and n["level"] == 3)
        assert "auth" in handler["metadata"].get("review_signals", [])

    def test_no_risk_for_simple_handler(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI
            app = FastAPI()
            @app.get("/health")
            def handler():
                return {"status": "ok"}
        """})
        d = _build(tmp_path, "GET", "/health")
        handler = next(n for n in d["nodes"].values()
                       if n.get("metadata", {}).get("pipeline_phase") == "handler" and n["level"] == 3)
        assert handler["metadata"].get("risk_score", 0) == 0


class TestRiskLevels:
    def test_risk_level_thresholds(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI, HTTPException
            app = FastAPI()
            @app.get("/test")
            def handler():
                raise HTTPException(status_code=400)
                raise HTTPException(status_code=500)
        """})
        d = _build(tmp_path, "GET", "/test")
        handler = next(n for n in d["nodes"].values()
                       if n.get("metadata", {}).get("pipeline_phase") == "handler" and n["level"] == 3)
        level = handler["metadata"].get("risk_level", "")
        assert level in ("low", "medium", "high", "critical")

    def test_phase_multiplier_handler(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI, HTTPException
            app = FastAPI()
            @app.get("/test")
            def handler():
                raise HTTPException(status_code=400)
        """})
        d = _build(tmp_path, "GET", "/test")
        handler = next(n for n in d["nodes"].values()
                       if n.get("metadata", {}).get("pipeline_phase") == "handler" and n["level"] == 3)
        # Handler phase = 1.5x multiplier. raises_4xx=2 raw → 3.0 with multiplier
        score = handler["metadata"].get("risk_score", 0)
        assert score >= 3.0  # 2 * 1.5

    def test_endpoint_aggregate_risk(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI, HTTPException
            app = FastAPI()
            @app.get("/test")
            def handler():
                raise HTTPException(status_code=400)
        """})
        d = _build(tmp_path, "GET", "/test")
        ep_risk = d["entrypoint"]["metadata"].get("risk_score", 0)
        assert ep_risk > 0


class TestRiskDeduplication:
    def test_raises_generic_skipped_when_specific_exists(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            from fastapi import FastAPI, HTTPException
            app = FastAPI()
            @app.get("/test")
            def handler():
                raise HTTPException(status_code=401)
        """})
        d = _build(tmp_path, "GET", "/test")
        handler = next(n for n in d["nodes"].values()
                       if n.get("metadata", {}).get("pipeline_phase") == "handler" and n["level"] == 3)
        factors = handler["metadata"].get("risk_factors", [])
        factor_names = [f["factor"] for f in factors]
        # Should have raises_4xx but not generic raises
        assert "raises_4xx" in factor_names
        assert "raises" not in factor_names
