from pathlib import Path

import anyio

from codecanvas_mcp.mcp import server

SAMPLE = Path(__file__).parent.parent / "sample-fastapi"


def test_all_tools_registered():
    tools = anyio.run(server.mcp.list_tools)
    names = {t.name for t in tools}
    assert names == {"list_entrypoints", "who_calls", "what_does",
                     "analyze_impact", "function_flow", "reaching_conditions",
                     "validate_state_schema", "simulate_state_transition",
                     "call_tree", "find_symbols", "project_status",
                     "verify_claim"}


def test_tool_function_returns_dict():
    # The decorated tool functions remain directly callable.
    out = server.list_entrypoints(str(SAMPLE))
    assert isinstance(out, dict) and "entrypoints" in out
    assert out["analysis_root"] == str(SAMPLE.resolve())
    assert out["evidence_grade"] == "definite"
    assert out["safe_to_summarize"] is True


def test_tool_missing_project_returns_error_dict():
    out = server.list_entrypoints("/no/such/dir")
    assert "error" in out


def test_project_status_recommends_nested_python_root(tmp_path):
    nested = tmp_path / "core"
    nested.mkdir()
    (nested / "pyproject.toml").write_text("[project]\nname='demo'\n")
    (nested / "app.py").write_text("def main():\n    pass\n")

    out = server.project_status(str(tmp_path))

    assert out["python_files"] == 1
    assert out["recommended_root"] == str(nested)
    assert out["cache"]["exists"] is False


def test_inferred_edges_force_uncertain_response_guidance(tmp_path):
    (tmp_path / "caller.py").write_text(
        "def start():\n    shared()\n",
        encoding="utf-8",
    )
    (tmp_path / "one.py").write_text(
        "def shared():\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "two.py").write_text(
        "def shared():\n    pass\n",
        encoding="utf-8",
    )

    out = server.call_tree("start", str(tmp_path), depth=1)

    assert out["evidence_grade"] == "inferred"
    assert out["inferred_edge_count"] == 2
    assert len(out["ambiguous_calls"]) == 2
    assert out["safe_to_summarize"] is False
    assert "unconditional claims" in out["response_guidance"]


def test_verify_claim_tool_returns_decorated_verdict(tmp_path):
    (tmp_path / "agent.py").write_text(
        """
class Runner:
    async def run(self):
        if self.agent.mode == "chat":
            return await self._run_node_async()
        raise ValueError("chat only")

    async def _run_node_async(self):
        return None
""".lstrip(),
        encoding="utf-8",
    )

    out = server.verify_claim(
        "root task-mode Runner reaches _run_node_async",
        str(tmp_path),
    )

    assert out["verdict"] == "false"
    assert out["analysis_root"] == str(tmp_path.resolve())
    assert out["evidence_grade"] in {"definite", "high"}
    assert out["safe_to_summarize"] is True


def test_verify_claim_tool_rejects_unknown_prefix_as_unsafe(tmp_path):
    (tmp_path / "app.py").write_text(
        "def run():\n    return target()\n\ndef target():\n    return None\n",
        encoding="utf-8",
    )

    out = server.verify_claim(
        "priority=urgent run reaches target",
        str(tmp_path),
    )

    assert "error" in out
    assert out["unsupported_prefix"] == "priority=urgent"
    assert out["safe_to_summarize"] is False


def test_verify_claim_metadata_uses_best_witness_path(tmp_path):
    (tmp_path / "caller.py").write_text(
        "def start():\n    target()\n    shared()\n",
        encoding="utf-8",
    )
    (tmp_path / "target.py").write_text(
        "def target():\n    return None\n",
        encoding="utf-8",
    )
    (tmp_path / "one.py").write_text(
        "def shared():\n    target()\n",
        encoding="utf-8",
    )
    (tmp_path / "two.py").write_text(
        "def shared():\n    return None\n",
        encoding="utf-8",
    )

    out = server.verify_claim(
        "start reaches target",
        str(tmp_path),
    )

    assert out["verdict"] == "true"
    assert out["witness_path"]["edges"][0]["confidence"] == "definite"
    assert any(
        edge["confidence"] == "inferred"
        for path in out["alternative_paths"]
        for edge in path["edges"]
    )
    assert out["evidence_grade"] == "definite"
    assert out["inferred_edge_count"] == 0
    assert out["ambiguous_calls"] == []
    assert out["safe_to_summarize"] is True
    assert "response_guidance" not in out
