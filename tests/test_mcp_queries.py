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


def test_what_does_verify_user():
    out = queries.what_does(_b(), "verify_user")
    assert out["async"] is True
    assert "email" in out["signature"] and "password" in out["signature"]
    assert out["docstring"].startswith("Verify")
    assert "calls" in out and "callees" in out["calls"]


def test_what_does_login_reports_raise():
    out = queries.what_does(_b(), "login")
    statuses = [r.get("status") for r in out["calls"]["raises"]]
    assert 401 in statuses, out["calls"]["raises"]


VERIFY_USER_DIFF = """\
--- a/app/services/auth_service.py
+++ b/app/services/auth_service.py
@@ -12,4 +12,5 @@
     async def verify_user(self, email: str, password: str):
         user = await self.user_repo.find_by_email(email)
         if user is None:
             return None
+        # changed
"""


def test_analyze_impact_maps_verify_user_to_login_endpoint():
    out = queries.analyze_impact(_b(), diff_text=VERIFY_USER_DIFF)
    changed = [c["function"] for c in out["changed_functions"]]
    assert any(name.endswith(".verify_user") for name in changed), changed
    ep_paths = [e["path"] for e in out["affected_endpoints"]]
    assert any(p.endswith("/login") for p in ep_paths), ep_paths


def test_analyze_impact_no_changes_message():
    out = queries.analyze_impact(_b(), diff_text="not a diff")
    assert "summary" in out
    assert out["changed_functions"] == []
