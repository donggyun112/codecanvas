"""Tests for data flow step generation, step_call edges, and visibility."""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))

from codecanvas.graph.builder import FlowGraphBuilder


SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "sample-fastapi")


@pytest.fixture(scope="module")
def builder():
    b = FlowGraphBuilder(SAMPLE_DIR)
    return b


@pytest.fixture(scope="module")
def login_flow(builder):
    eps = builder.get_entrypoints()
    ep = next(e for e in eps if e.path and "login" in e.path)
    return builder.build_flow(ep).to_dict()


class TestDataFlowSteps:
    def test_handler_has_data_flow_steps(self, login_flow):
        handler = _find_handler(login_flow)
        dfs = handler["metadata"].get("data_flow_steps", [])
        assert len(dfs) >= 3, f"Expected >=3 data flow steps, got {len(dfs)}"

    def test_no_duplicate_source_ids(self, login_flow):
        """Each data flow step should reference unique L4 source nodes."""
        handler = _find_handler(login_flow)
        dfs = handler["metadata"]["data_flow_steps"]
        all_source_ids = []
        for step in dfs:
            all_source_ids.extend(step.get("sourceStepIds", []))
        assert len(all_source_ids) == len(set(all_source_ids)), (
            "Duplicate source step IDs found — dedup is collapsing legitimate calls"
        )

    def test_operations_are_valid(self, login_flow):
        valid_ops = {"query", "transform", "validate", "branch", "respond", "side_effect", "process"}
        handler = _find_handler(login_flow)
        for step in handler["metadata"]["data_flow_steps"]:
            assert step["operation"] in valid_ops, f"Invalid operation: {step['operation']}"

    def test_respond_step_exists(self, login_flow):
        handler = _find_handler(login_flow)
        dfs = handler["metadata"]["data_flow_steps"]
        responds = [s for s in dfs if s["operation"] == "respond"]
        assert len(responds) >= 1, "No respond step found"

    def test_login_flow_steps(self, login_flow):
        """Login flow should have: transform(verify) + branch + transform(issue) + respond."""
        handler = _find_handler(login_flow)
        dfs = handler["metadata"]["data_flow_steps"]
        ops = [s["operation"] for s in dfs]
        assert "branch" in ops, "Missing branch step"
        assert ops[-1] == "respond", "Last step should be respond"


class TestStepCallEdges:
    def test_step_call_edges_exist(self, login_flow):
        step_calls = [e for e in login_flow["edges"] if e.get("metadata", {}).get("step_call")]
        assert len(step_calls) >= 2, f"Expected >=2 step_call edges, got {len(step_calls)}"

    def test_step_call_source_is_l4(self, login_flow):
        for e in login_flow["edges"]:
            if not e.get("metadata", {}).get("step_call"):
                continue
            src = login_flow["nodes"].get(e["sourceId"])
            assert src is not None, f"step_call source {e['sourceId']} not found"
            assert src["level"] == 4, f"step_call source should be L4, got L{src['level']}"

    def test_step_call_target_is_l3(self, login_flow):
        for e in login_flow["edges"]:
            if not e.get("metadata", {}).get("step_call"):
                continue
            tgt = login_flow["nodes"].get(e["targetId"])
            assert tgt is not None, f"step_call target {e['targetId']} not found"
            assert tgt["level"] == 3, f"step_call target should be L3, got L{tgt['level']}"

    def test_no_step_return_edges(self, login_flow):
        """step_return edges should NOT exist (return shown via annotation)."""
        step_returns = [e for e in login_flow["edges"] if e.get("metadata", {}).get("step_return")]
        assert len(step_returns) == 0, "step_return edges should not exist"

    def test_step_call_is_display_only(self, login_flow):
        for e in login_flow["edges"]:
            if not e.get("metadata", {}).get("step_call"):
                continue
            assert e["metadata"].get("display_only") is True, "step_call should be display_only"


class TestReturnTypeAnnotation:
    def test_step_call_targets_have_return_type(self, login_flow):
        """Callee nodes reached by step_call should have return_type when
        the source function has a return annotation."""
        step_call_targets = set()
        for e in login_flow["edges"]:
            if e.get("metadata", {}).get("step_call"):
                step_call_targets.add(e["targetId"])
        callee_nodes = [login_flow["nodes"][tid] for tid in step_call_targets if tid in login_flow["nodes"]]
        # At least _check_password (-> bool) and _create_jwt (-> str) have annotations
        with_return = [n for n in callee_nodes if n.get("metadata", {}).get("return_type")]
        assert len(with_return) >= 1, (
            f"No step_call target nodes have return_type. Targets: "
            f"{[n.get('name') for n in callee_nodes]}"
        )


class TestEdgeCoexistence:
    def test_l3_and_step_call_both_exist(self, login_flow):
        """Both L3→L3 and step_call edges should coexist in the model.
        Deduplication happens in the webview transform, not in the backend."""
        step_call_count = sum(
            1 for e in login_flow["edges"]
            if e.get("metadata", {}).get("step_call")
        )
        l3_call_count = sum(
            1 for e in login_flow["edges"]
            if not e.get("metadata", {}).get("step_call")
            and not e.get("metadata", {}).get("display_only")
            and login_flow["nodes"].get(e["sourceId"], {}).get("level") == 3
            and login_flow["nodes"].get(e["targetId"], {}).get("level") == 3
            and e["type"] == "calls"
        )
        assert step_call_count >= 2, "Expected step_call edges"
        assert l3_call_count >= 2, "Expected L3→L3 edges (call graph contract)"


class TestDefinitionOnlyClass:
    def test_definition_only_classes_have_no_l4_children(self, login_flow):
        """Classes like AuthService, UserRepository should have no L4 children
        (the condition used by visibility to hide them)."""
        nodes = login_flow["nodes"]
        class_nodes = [n for n in nodes.values() if n["level"] == 3 and n["type"] == "class"]
        assert len(class_nodes) >= 1, "Expected at least one class node in login flow"
        for cn in class_nodes:
            children = [
                n for n in nodes.values()
                if n["level"] == 4 and n.get("metadata", {}).get("function_id") == cn["id"]
            ]
            assert len(children) == 0, (
                f"Class {cn.get('name')} has L4 children — not definition-only: "
                f"{[c.get('name') for c in children]}"
            )


class TestExecutionGraph:
    def test_execution_graph_exists(self, login_flow):
        assert "executionGraph" in login_flow, "executionGraph missing from to_dict()"

    def test_has_pipeline_and_handler_steps(self, login_flow):
        eg = login_flow["executionGraph"]
        phases = {s["phase"] for s in eg["steps"]}
        assert "trigger" in phases or "api" in phases, "No pipeline steps"
        assert "handler" in phases, "No handler steps"

    def test_has_links(self, login_flow):
        eg = login_flow["executionGraph"]
        assert len(eg["links"]) >= len(eg["steps"]) - 1, "Too few links"

    def test_no_dangling_links(self, login_flow):
        eg = login_flow["executionGraph"]
        step_ids = {s["id"] for s in eg["steps"]}
        for link in eg["links"]:
            assert link["sourceStepId"] in step_ids, f"Dangling source: {link['sourceStepId']}"
            assert link["targetStepId"] in step_ids, f"Dangling target: {link['targetStepId']}"

    def test_handler_steps_present(self, login_flow):
        """ExecutionGraph should have handler steps."""
        eg = login_flow["executionGraph"]
        handler_steps = [s for s in eg["steps"] if s["phase"] == "handler"]
        assert len(handler_steps) >= 3, f"Expected >=3 handler steps, got {len(handler_steps)}"

    def test_callee_depth_recursive(self, login_flow):
        """Login: handler(0) → verify_user(1) → find_by_email(2)."""
        eg = login_flow["executionGraph"]
        depths = {s["depth"] for s in eg["steps"]}
        assert 0 in depths, "No depth-0 steps"
        assert 1 in depths, "No depth-1 callee steps"
        assert 2 in depths, f"No depth-2 callee steps — recursive expansion missing. Depths: {depths}"


class TestBranchPathSeparation:
    def test_branch_with_body_has_branch_id(self):
        """Branches with flattened body steps (if/else) should tag steps with branchId.
        Uses send_message which has if body.stream / else."""
        import os
        ai_lib = os.path.join(os.path.dirname(__file__), "..", "..", "ai-librarian", "poc")
        if not os.path.isdir(ai_lib):
            pytest.skip("ai-librarian project not found")
        b = FlowGraphBuilder(ai_lib)
        eps = b.get_entrypoints()
        ep = next((e for e in eps if e.path and "messages" in e.path and e.method == "POST"), None)
        if not ep:
            pytest.skip("send_message endpoint not found")
        data = b.build_flow(ep).to_dict()
        hid = next(n["id"] for n in data["nodes"].values()
                   if n.get("metadata", {}).get("pipeline_phase") == "handler" and n["level"] == 3)
        dfs = data["nodes"][hid]["metadata"]["data_flow_steps"]
        branched = [s for s in dfs if s.get("branchId")]
        assert len(branched) >= 2, f"Expected >=2 branch-path steps, got {len(branched)}"
        paths = {s["branchId"].split(":")[-1] for s in branched if ":" in (s.get("branchId") or "")}
        assert "if" in paths, f"Missing 'if' branch path. Paths: {paths}"
        assert "else" in paths, f"Missing 'else' branch path. Paths: {paths}"

    def test_execution_graph_branch_fork(self, login_flow):
        """Branch node should have at least 1 outgoing link."""
        eg = login_flow["executionGraph"]
        link_by_src: dict[str, list] = {}
        for l in eg["links"]:
            link_by_src.setdefault(l["sourceStepId"], []).append(l)
        for s in eg["steps"]:
            if s["operation"] == "branch" and s["phase"] == "handler":
                out = link_by_src.get(s["id"], [])
                assert len(out) >= 1, f"Branch '{s['label']}' has no outgoing links"


class TestBranchMergeProvenance:
    """Verify that after branch merge, data links come from correct producers."""

    def test_merge_links_not_empty(self, login_flow):
        """Post-merge step should have incoming links from branch tail(s)."""
        eg = login_flow["executionGraph"]
        link_by_tgt: dict[str, list] = {}
        for l in eg["links"]:
            link_by_tgt.setdefault(l["targetStepId"], []).append(l)
        # Login: branch "user is None" → [raise] vs [issue tokens]
        # After merge: "issue tokens" or "LoginResponse" should have incoming links
        handler_steps = [s for s in eg["steps"] if s["phase"] == "handler"]
        branch_ids = {s.get("branchId") for s in handler_steps if s.get("branchId")}
        main_after_branch = [
            s for s in handler_steps
            if not s.get("branchId") and s["operation"] != "branch"
        ]
        # Steps after a branch should have at least one incoming link
        for s in main_after_branch:
            incoming = link_by_tgt.get(s["id"], [])
            assert len(incoming) >= 1, f"Post-merge step '{s['label']}' has no incoming links"


def _find_handler(flow_data):
    for n in flow_data["nodes"].values():
        if n.get("metadata", {}).get("pipeline_phase") == "handler" and n["level"] == 3:
            return n
    raise AssertionError("No handler node found")
