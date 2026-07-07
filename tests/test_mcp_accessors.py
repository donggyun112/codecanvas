from pathlib import Path

from codecanvas_mcp.parser.call_graph import CallGraphBuilder

SAMPLE = Path(__file__).parent.parent / "sample-fastapi"


def _cg():
    cg = CallGraphBuilder(str(SAMPLE))
    cg.analyze_project()
    return cg


def test_all_functions_includes_known_symbols():
    cg = _cg()
    names = {f.name for f in cg.all_functions()}
    assert "login" in names
    assert "verify_user" in names


def test_get_callers_of_verify_user_includes_login():
    cg = _cg()
    # Resolve verify_user's qualified name.
    verify = next(f for f in cg.all_functions() if f.name == "verify_user")
    callers = cg.get_callers(verify.qualified_name)
    caller_names = {caller.name for caller, _ref in callers}
    assert "login" in caller_names


def test_get_callers_unknown_returns_empty():
    cg = _cg()
    assert cg.get_callers("does.not.Exist") == []
