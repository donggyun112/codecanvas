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
                     "call_tree", "find_symbols", "project_status"}


def test_tool_function_returns_dict():
    # The decorated tool functions remain directly callable.
    out = server.list_entrypoints(str(SAMPLE))
    assert isinstance(out, dict) and "entrypoints" in out


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
