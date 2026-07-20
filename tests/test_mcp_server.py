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
                     "call_tree"}


def test_tool_function_returns_dict():
    # The decorated tool functions remain directly callable.
    out = server.list_entrypoints(str(SAMPLE))
    assert isinstance(out, dict) and "entrypoints" in out


def test_tool_missing_project_returns_error_dict():
    out = server.list_entrypoints("/no/such/dir")
    assert "error" in out
