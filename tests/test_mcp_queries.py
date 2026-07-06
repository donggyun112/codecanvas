from pathlib import Path

from codecanvas.mcp.session import get_builder
from codecanvas.mcp import queries

SAMPLE = Path(__file__).parent.parent / "sample-fastapi"


def _b():
    return get_builder(str(SAMPLE))


def test_list_entrypoints_finds_login_route():
    out = queries.list_entrypoints(_b())
    assert out["count"] >= 1
    paths = [(e["method"], e["path"]) for e in out["entrypoints"]]
    assert any(m == "POST" and p.endswith("/login") for m, p in paths), paths


def test_resolve_by_bare_name():
    func, err = queries.resolve_function(_b(), "verify_user")
    assert err is None
    assert func is not None and func.name == "verify_user"


def test_resolve_unknown_returns_suggestions():
    func, err = queries.resolve_function(_b(), "verifyuser")
    assert func is None
    assert "error" in err and isinstance(err["suggestions"], list)


def test_who_calls_verify_user_lists_login():
    out = queries.who_calls(_b(), "verify_user")
    assert "callers" in out, out
    caller_names = [c["caller"] for c in out["callers"]]
    assert any(name.endswith(".login") or name == "login" for name in caller_names), caller_names


def test_who_calls_unknown_returns_error():
    out = queries.who_calls(_b(), "nope_nope")
    assert "error" in out
