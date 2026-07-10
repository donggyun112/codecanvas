"""Tests for risk scoring: signal points, phase multipliers, aggregation."""
from __future__ import annotations
import sys, textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from codecanvas_mcp.graph.builder import FlowGraphBuilder
from codecanvas_mcp.graph.models import (
    FlowGraph, FlowNode, FlowEdge, EntryPoint, NodeType, EdgeType,
)


def _l3(node_id: str, name: str, phase: str, signals: list[str]) -> FlowNode:
    return FlowNode(
        id=node_id, node_type=NodeType.FUNCTION, name=name, level=3,
        metadata={"pipeline_phase": phase, "review_signals": list(signals)},
    )


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


class TestRiskDoubleCount:
    def test_propagated_db_write_scored_once_in_endpoint(self):
        """A db_write performed in one function must not be summed into the
        endpoint aggregate twice via 1-hop signal propagation to its caller."""
        ep = EntryPoint(kind="api", method="POST", path="/items")
        g = FlowGraph(entrypoint=ep)
        handler = _l3("h", "handler", "handler", [])       # no own risky work
        repo = _l3("r", "save", "repository", ["db_write"])  # the real db_write
        g.add_node(handler)
        g.add_node(repo)
        g.add_edge(FlowEdge(id="e1", source_id="h", target_id="r",
                            edge_type=EdgeType.CALLS))

        FlowGraphBuilder._propagate_review_signals(g)
        FlowGraphBuilder._compute_risk_scores(g)

        # Visibility preserved: handler still surfaces the transitive db_write.
        assert "db_write" in handler.metadata.get("review_signals", [])
        # But the propagated signal must NOT be scored on the handler —
        # the repo already owns those points.
        handler_factors = [f["factor"] for f in handler.metadata.get("risk_factors", [])]
        assert "db_write" not in handler_factors
        # Deliberate ranking consequence: a pure-delegator handler carries no
        # own-risk score, so it drops out of focusAreas (risk_score >= 3) and
        # risk is attributed to the repo where the write actually happens.
        assert handler.metadata.get("risk_score", 0) == 0
        assert repo.metadata.get("risk_score", 0) >= 3
        # Endpoint aggregate counts the physical db_write exactly once.
        assert ep.metadata.get("risk_score", 0) == repo.metadata.get("risk_score", 0)


class TestSignalWeightsSingleSource:
    def test_builder_and_impact_share_one_weight_table(self):
        """Both scorers must reference the same weight object so they cannot
        drift (impact.py previously duplicated the dict verbatim)."""
        from codecanvas_mcp.parser.call_graph import REVIEW_SIGNAL_POINTS
        from codecanvas_mcp.graph import builder as builder_mod
        from codecanvas_mcp.graph import impact as impact_mod
        assert builder_mod.REVIEW_SIGNAL_POINTS is REVIEW_SIGNAL_POINTS
        assert impact_mod.REVIEW_SIGNAL_POINTS is REVIEW_SIGNAL_POINTS

    def test_no_dead_io_weight(self):
        """'io' is never emitted as a node-level review signal, so a weight for
        it can never fire — it must not sit in the scoring table pretending to."""
        from codecanvas_mcp.parser.call_graph import REVIEW_SIGNAL_POINTS
        assert "io" not in REVIEW_SIGNAL_POINTS


class TestExecuteAccessClassification:
    def _signals_for(self, tmp_path, func_name: str) -> list[str]:
        from codecanvas_mcp.parser.call_graph import CallGraphBuilder
        _write_files(tmp_path, {"m.py": """
            from sqlalchemy import select, insert
            def read_items(session):
                return session.execute(select(Item)).scalars().all()
            def write_item(session):
                session.execute(insert(Item).values(name="x"))
        """})
        cg = CallGraphBuilder(str(tmp_path))
        cg.analyze_project()
        func = next(f for f in cg._functions.values() if f.name == func_name)
        return CallGraphBuilder._aggregate_review_signals(func)

    def test_execute_select_is_read_not_write(self, tmp_path):
        signals = self._signals_for(tmp_path, "read_items")
        assert "db_read" in signals
        # session.execute(select(...)) is a read — must NOT be tagged db_write.
        assert "db_write" not in signals

    def test_execute_insert_is_write(self, tmp_path):
        signals = self._signals_for(tmp_path, "write_item")
        assert "db_write" in signals


