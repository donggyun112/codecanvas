"""Integration smoke tests: validate full pipeline on sample-fastapi."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from codecanvas.graph.builder import FlowGraphBuilder
from codecanvas.graph.cfg import CFGBuilder, ControlFlowGraph
from codecanvas.graph.impact import ImpactAnalyzer

SAMPLE = ROOT / "sample-fastapi"


def _validate_cfg(cfg: ControlFlowGraph) -> list[str]:
    errors = []
    block_ids = {b.id for b in cfg.blocks}
    edge_targets = {e.target_block_id for e in cfg.edges}
    for b in cfg.blocks:
        if b.kind == "entry" and b.id in edge_targets:
            errors.append(f"Entry {b.id} is edge target")
        elif b.kind not in ("entry", "error_exit") and b.id not in edge_targets:
            errors.append(f"Orphan block {b.id} [{b.kind}]")
    for e in cfg.edges:
        if e.source_block_id not in block_ids:
            errors.append(f"Dangling source {e.source_block_id}")
        if e.target_block_id not in block_ids:
            errors.append(f"Dangling target {e.target_block_id}")
    seen = set()
    for e in cfg.edges:
        key = (e.source_block_id, e.target_block_id, e.kind)
        if key in seen:
            errors.append(f"Duplicate edge {key}")
        seen.add(key)
    return errors


class TestAllEndpoints:
    """Run pipeline on every endpoint in sample-fastapi."""

    @classmethod
    def setup_class(cls):
        cls.builder = FlowGraphBuilder(str(SAMPLE))
        cls.endpoints = cls.builder.get_entrypoints()
        cls.flows = {}
        for ep in cls.endpoints:
            b = FlowGraphBuilder(str(SAMPLE))
            cls.flows[ep.id] = b.build_flow(ep).to_dict()

    def test_all_flow_graphs_build(self):
        assert len(self.flows) == len(self.endpoints)
        assert len(self.endpoints) >= 3

    def test_cfg_structure_valid(self):
        errors = []
        for ep in self.endpoints:
            d = self.flows[ep.id]
            cfg_data = d.get("cfg", {})
            if not cfg_data.get("blocks"):
                continue
            # Rebuild CFG for validation
            b = FlowGraphBuilder(str(SAMPLE))
            cg = b.call_graph
            cg.analyze_project()
            cfg_builder = CFGBuilder(cg)
            cfg = cfg_builder.build(ep.handler_name, ep.handler_file, ep.handler_line)
            errs = _validate_cfg(cfg)
            if errs:
                errors.append(f"{ep.method} {ep.path}: {errs}")
        assert errors == [], f"CFG errors: {errors}"

    def test_risk_scores_non_negative(self):
        for ep in self.endpoints:
            d = self.flows[ep.id]
            for nid, n in d["nodes"].items():
                score = n.get("metadata", {}).get("risk_score", 0)
                assert score >= 0, f"Negative risk: {n.get('name')} = {score}"

    def test_review_summary_exists(self):
        for ep in self.endpoints:
            if ep.kind != "api":
                continue
            d = self.flows[ep.id]
            summary = d["entrypoint"]["metadata"].get("review_summary")
            assert summary is not None, f"No review_summary for {ep.method} {ep.path}"
            assert "concerns" in summary
            assert "focusAreas" in summary

    def test_no_dangling_edges(self):
        for ep in self.endpoints:
            d = self.flows[ep.id]
            node_ids = set(d["nodes"].keys())
            for e in d["edges"]:
                assert e["sourceId"] in node_ids, f"Dangling source {e['sourceId']} in {ep.path}"
                assert e["targetId"] in node_ids, f"Dangling target {e['targetId']} in {ep.path}"

    def test_execution_graph_reasonable(self):
        for ep in self.endpoints:
            d = self.flows[ep.id]
            eg = d.get("executionGraph", {})
            steps = eg.get("steps", [])
            assert len(steps) <= 200, f"Too many steps ({len(steps)}) in {ep.path}"
            if ep.kind == "api":
                assert len(steps) >= 1, f"No steps for {ep.path}"

    def test_response_origin_no_cycles(self):
        for ep in self.endpoints:
            d = self.flows[ep.id]
            eg = d.get("executionGraph", {})
            for s in eg.get("steps", []):
                origins = s.get("metadata", {}).get("response_origins", [])
                ids = [o["stepId"] for o in origins]
                assert len(ids) == len(set(ids)), (
                    f"Cycle in response origins for {s['label']} in {ep.path}"
                )

    def test_impact_analysis_end_to_end(self):
        b = FlowGraphBuilder(str(SAMPLE))
        eps = b.get_entrypoints()
        # Construct diff touching login handler
        login_ep = next((e for e in eps if "login" in (e.path or "")), None)
        if not login_ep:
            return
        b.call_graph.analyze_project()
        handler = next(
            (f for f in b.call_graph._functions.values()
             if f.name == login_ep.handler_name and "auth" in f.file_path),
            None,
        )
        if not handler:
            return
        line = handler.line_start + 1
        diff = f"""diff --git a/{handler.file_path} b/{handler.file_path}
--- a/{handler.file_path}
+++ b/{handler.file_path}
@@ -{line},1 +{line},2 @@
+    audit()
"""
        analyzer = ImpactAnalyzer(b.call_graph, str(SAMPLE), entrypoints=eps, flow_builder=b)
        result = analyzer.analyze_diff(diff)
        assert len(result.affected_functions) >= 1
        assert len(result.affected_endpoints) >= 1
