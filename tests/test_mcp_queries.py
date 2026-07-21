import textwrap
from pathlib import Path

from codecanvas_mcp.mcp.session import get_builder
from codecanvas_mcp.mcp import queries

SAMPLE = Path(__file__).parent.parent / "sample-fastapi"


def _b():
    return get_builder(str(SAMPLE))


def _tmp_builder(tmp_path, files: dict[str, str]):
    for rel, content in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")
    return get_builder(str(tmp_path))


def test_list_entrypoints_finds_login_route():
    out = queries.list_entrypoints(_b())
    assert out["count"] >= 1
    paths = [(e["method"], e["path"]) for e in out["entrypoints"]]
    assert any(m == "POST" and p.endswith("/login") for m, p in paths), paths


def test_list_entrypoints_filter_narrows_to_login():
    out = queries.list_entrypoints(_b(), filter="login")
    assert out["count"] >= 1
    for e in out["entrypoints"]:
        hay = f"{e['method']} {e['path']} {e['handler']} {e['id']} {' '.join(e['tags'])}".lower()
        assert "login" in hay, e
    assert any(e["path"].endswith("/login") for e in out["entrypoints"])


def test_list_entrypoints_kind_filters_to_api():
    out = queries.list_entrypoints(_b(), kind="api")
    assert out["count"] >= 1
    assert all(e["kind"] == "api" for e in out["entrypoints"])


def test_list_entrypoints_filter_no_match_is_empty():
    out = queries.list_entrypoints(_b(), filter="zzz_no_such_route_zzz")
    assert out["count"] == 0
    assert out["entrypoints"] == []



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


def test_analyze_impact_rejects_option_injection_ref(tmp_path):
    sentinel = tmp_path / "pwned.txt"
    out = queries.analyze_impact(_b(), git_ref=f"--output={sentinel}")
    assert "error" in out, out
    assert not sentinel.exists(), "git must not have written the sentinel file"


def test_is_safe_git_ref():
    from codecanvas_mcp.graph.impact import _is_safe_git_ref
    assert _is_safe_git_ref("HEAD~1..HEAD")
    assert _is_safe_git_ref("main...feature/x")
    assert _is_safe_git_ref("abc123")
    assert not _is_safe_git_ref("--output=/tmp/x")
    assert not _is_safe_git_ref("HEAD..--output=/tmp/x")
    assert not _is_safe_git_ref("")


FIXTURE_APP = {
    "app.py": """
        from fastapi import FastAPI
        app = FastAPI()

        @app.get("/real")
        def real_route():
            return {"ok": True}
    """,
    "tests/test_routes.py": """
        from fastapi import FastAPI
        app = FastAPI()

        @app.post("/fixture")
        def fixture_route():
            return {"ok": True}
    """,
}


def test_list_entrypoints_excludes_test_fixtures_by_default(tmp_path):
    out = queries.list_entrypoints(_tmp_builder(tmp_path, FIXTURE_APP))
    paths = [e["path"] for e in out["entrypoints"]]
    assert "/real" in paths, paths
    assert "/fixture" not in paths, paths
    assert "test" in out.get("note", "").lower(), out.get("note")


def test_list_entrypoints_include_tests_keeps_fixtures(tmp_path):
    out = queries.list_entrypoints(
        _tmp_builder(tmp_path, FIXTURE_APP), include_tests=True)
    paths = [e["path"] for e in out["entrypoints"]]
    assert "/real" in paths, paths
    assert "/fixture" in paths, paths


CALL_CHAIN = {
    "chain.py": """
        def leaf():
            return 1

        def mid():
            return leaf()

        def top():
            return mid()
    """,
}


def test_who_calls_default_depth_is_direct_only(tmp_path):
    out = queries.who_calls(_tmp_builder(tmp_path, CALL_CHAIN), "leaf")
    names = [c["caller"] for c in out["callers"]]
    assert any(n.endswith("mid") for n in names), names
    assert not any(n.endswith("top") for n in names), names


def test_who_calls_depth_2_traces_transitive_callers(tmp_path):
    out = queries.who_calls(_tmp_builder(tmp_path, CALL_CHAIN), "leaf", depth=2)
    by_name = {c["caller"].rsplit(".", 1)[-1]: c for c in out["callers"]}
    assert "mid" in by_name and "top" in by_name, out["callers"]
    assert by_name["mid"]["depth"] == 1
    assert by_name["top"]["depth"] == 2
    assert by_name["mid"]["callee"].endswith("leaf")
    assert by_name["top"]["callee"].endswith("mid")


def test_who_calls_depth_handles_recursion(tmp_path):
    out = queries.who_calls(_tmp_builder(tmp_path, {
        "rec.py": """
            def a():
                return b()

            def b():
                return a()
        """,
    }), "a", depth=5)
    names = [c["caller"] for c in out["callers"]]
    assert len(names) == len(set(names)), names


FANOUT = {
    "fan.py": """
        def target():
            return 1

        def alpha_caller():
            return target()

        def beta_caller():
            return target()
    """,
}


def test_who_calls_filter_narrows_callers(tmp_path):
    out = queries.who_calls(_tmp_builder(tmp_path, FANOUT), "target", filter="alpha")
    names = [c["caller"] for c in out["callers"]]
    assert any(n.endswith("alpha_caller") for n in names), names
    assert not any(n.endswith("beta_caller") for n in names), names


def test_who_calls_filter_no_match_is_empty(tmp_path):
    out = queries.who_calls(
        _tmp_builder(tmp_path, FANOUT), "target", filter="zzz_no_such")
    assert out["callers"] == []


NON_PY_DIFF = """\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,1 +1,2 @@
+docs change
"""


def test_analyze_impact_reports_skipped_non_python_files():
    out = queries.analyze_impact(_b(), diff_text=NON_PY_DIFF)
    assert out["changed_functions"] == []
    assert out["skipped_files"] == ["README.md"]
    assert "non-python" in out["summary"].lower(), out["summary"]


def test_analyze_impact_no_diff_has_no_skipped_files_key():
    out = queries.analyze_impact(_b(), diff_text="not a diff")
    assert "skipped_files" not in out
    assert out["summary"] == "No Python changes detected."


STATE_RETURNS = {
    "agent.py": """
        def next_step(state):
            if state.get("done"):
                return {"messages": []}
            return {
                "messages": [],
                "remaining_steps": state["remaining_steps"] - 1,
            }
    """,
}


def test_validate_state_schema_flags_missing_required_return_key(tmp_path):
    schema = {
        "properties": {"messages": {}, "remaining_steps": {}},
        "required": ["messages", "remaining_steps"],
    }
    out = queries.validate_state_schema(
        _tmp_builder(tmp_path, STATE_RETURNS), "next_step", schema)

    assert [r["field"] for r in out["reads"]] == ["done", "remaining_steps"]
    assert any(
        d["type"] == "missing_required_return_keys"
        and d["fields"] == ["remaining_steps"]
        for d in out["diagnostics"]
    ), out["diagnostics"]


STATE_EXTRA_FIELD = {
    "agent.py": """
        def next_step(state):
            update = {"messages": []}
            update["unexpected"] = True
            return update
    """,
}


def test_validate_state_schema_flags_return_field_outside_schema(tmp_path):
    schema = {
        "properties": {"messages": {}, "remaining_steps": {}},
        "required": ["messages"],
    }
    out = queries.validate_state_schema(
        _tmp_builder(tmp_path, STATE_EXTRA_FIELD), "next_step", schema)

    assert out["returns"][0]["keys"] == ["messages", "unexpected"]
    assert any(
        d["type"] == "field_not_in_schema" and d["field"] == "unexpected"
        for d in out["diagnostics"]
    ), out["diagnostics"]


def test_validate_state_schema_requires_matching_state_var(tmp_path):
    builder = _tmp_builder(tmp_path, {
        "agent.py": """
            def summarize_items(items):
                return {"count": len(items), "items": items}
        """,
    })
    out = queries.validate_state_schema(
        builder,
        "summarize_items",
        {"properties": {"count": {}, "items": {}}, "required": ["count"]},
    )

    assert out["error"].startswith("state_var 'state' must match")
    assert out["parameters"] == ["items"]
    assert out["state_var"] == "state"


IMPACT_EP_APP = {
    "svc.py": "def helper():\n    return 1\n",
    "app.py": """
        from fastapi import FastAPI
        from svc import helper
        app = FastAPI()

        @app.get("/real")
        def real_route():
            return helper()
    """,
    "tests/test_routes.py": """
        from fastapi import FastAPI
        from svc import helper
        app = FastAPI()

        @app.get("/fixture")
        def fixture_route():
            return helper()
    """,
}

HELPER_DIFF = """\
--- a/svc.py
+++ b/svc.py
@@ -1,2 +1,3 @@
 def helper():
+    # changed
     return 1
"""


def test_analyze_impact_excludes_test_endpoints_by_default(tmp_path):
    out = queries.analyze_impact(_tmp_builder(tmp_path, IMPACT_EP_APP),
                                 diff_text=HELPER_DIFF)
    paths = [e["path"] for e in out["affected_endpoints"]]
    assert "/real" in paths, paths
    assert "/fixture" not in paths, paths


def test_analyze_impact_include_tests_keeps_test_endpoints(tmp_path):
    out = queries.analyze_impact(_tmp_builder(tmp_path, IMPACT_EP_APP),
                                 diff_text=HELPER_DIFF, include_tests=True)
    paths = [e["path"] for e in out["affected_endpoints"]]
    assert "/fixture" in paths, paths


def test_analyze_impact_mixed_diff_reports_both():
    mixed = NON_PY_DIFF + VERIFY_USER_DIFF
    out = queries.analyze_impact(_b(), diff_text=mixed)
    changed = [c["function"] for c in out["changed_functions"]]
    assert any(name.endswith(".verify_user") for name in changed), changed
    assert out["skipped_files"] == ["README.md"]


LIBRARY_IMPACT_APP = {
    "simulator.py": """
        class FakeDB:
            def execute(self, sql):
                pass

        def _invoke(db):
            db.execute("INSERT INTO events VALUES (1)")

        def run():
            return _invoke(FakeDB())
    """,
}

LIBRARY_INVOKE_DIFF = """\
--- a/simulator.py
+++ b/simulator.py
@@ -5,2 +5,3 @@
 def _invoke(db):
+    # changed
     db.execute("INSERT INTO events VALUES (1)")
"""


def test_analyze_impact_reports_library_public_surface(tmp_path):
    out = queries.analyze_impact(
        _tmp_builder(tmp_path, LIBRARY_IMPACT_APP),
        diff_text=LIBRARY_INVOKE_DIFF,
    )

    changed = next(
        c for c in out["changed_functions"]
        if c["function"].endswith("._invoke")
    )
    assert changed["risk"] >= 3
    assert changed["risk_level"] == "medium"
    assert any(f["factor"] == "db_write" for f in changed["risk_factors"])
    assert out["risk_scale"]["weights"]["db_write"] == 3

    surface = out["affected_entrypoints"][0]
    assert surface["kind"] == "function"
    assert surface["surface"] == "run()"
    assert surface["module"] == "simulator.py"
    assert "method" not in surface

    legacy = out["affected_endpoints"][0]
    assert legacy["method"] == ""
    assert legacy["path"] == "simulator.py"


REACH_APP = {
    "svc.py": """
        def save():
            pass

        def charge(ok, force):
            if not ok:
                raise ValueError("bad")
            try:
                save()
            except Exception:
                return {"saved": False}
            if force:
                return {"forced": True}
            return {"ok": True}
    """,
}


YIELD_APP = {
    "g.py": """
        def gen(flag):
            if flag:
                yield 1
            yield 2

        async def stream(agen):
            async for x in agen:
                yield x
    """,
}


def test_reaching_conditions_captures_yield(tmp_path):
    b = _tmp_builder(tmp_path, YIELD_APP)
    out = queries.reaching_conditions(b, "gen")
    kinds = {(o["kind"], tuple(o["guards"])) for o in out["outcomes"]}
    assert ("yield", ("flag",)) in kinds, out["outcomes"]
    assert ("yield", ()) in kinds, out["outcomes"]


def test_reaching_conditions_yield_in_async_for(tmp_path):
    b = _tmp_builder(tmp_path, YIELD_APP)
    out = queries.reaching_conditions(b, "stream")
    ylds = [o for o in out["outcomes"] if o["kind"] == "yield"]
    assert ylds and "loop" in ylds[0]["guards"], out["outcomes"]


def test_reaching_conditions_target_yield_filters(tmp_path):
    b = _tmp_builder(tmp_path, YIELD_APP)
    out = queries.reaching_conditions(b, "gen", target="yield")
    assert out["outcomes"] and all(o["kind"] == "yield" for o in out["outcomes"])


def test_reaching_conditions_returns_guarded_outcomes(tmp_path):
    out = queries.reaching_conditions(_tmp_builder(tmp_path, REACH_APP), "charge")
    kinds = {(o["kind"], tuple(o["guards"])) for o in out["outcomes"]}
    # raise is guarded by `not ok`
    assert any(k == "raise" and any("ok" in g for g in guards)
               for k, guards in kinds), out["outcomes"]
    # error-path return is under the except handler
    assert any(k == "return" and any("except" in g for g in guards)
               for k, guards in kinds), out["outcomes"]


def test_reaching_conditions_success_path_is_unguarded(tmp_path):
    out = queries.reaching_conditions(_tmp_builder(tmp_path, REACH_APP), "charge")
    # the final `return {"ok": True}` is the fallthrough: no enclosing guard
    ok_returns = [o for o in out["outcomes"]
                  if o["kind"] == "return" and "ok" in o["detail"]]
    assert ok_returns and ok_returns[0]["guards"] == [], ok_returns


def test_reaching_conditions_target_raise_filters(tmp_path):
    out = queries.reaching_conditions(
        _tmp_builder(tmp_path, REACH_APP), "charge", target="raise")
    assert out["outcomes"]
    assert all(o["kind"] == "raise" for o in out["outcomes"])


def test_reaching_conditions_reports_cyclomatic(tmp_path):
    out = queries.reaching_conditions(_tmp_builder(tmp_path, REACH_APP), "charge")
    # if + try/except + if  → complexity clearly above 1
    assert out["cyclomatic"] >= 3, out["cyclomatic"]


CALLTREE_APP = {
    "svc.py": """
        import httpx

        def leaf():
            httpx.get("http://x")
            return 1

        def mid():
            return leaf()

        def top():
            return mid()
    """,
}


def test_call_tree_traces_forward_callees(tmp_path):
    out = queries.call_tree(_tmp_builder(tmp_path, CALLTREE_APP), "top", depth=2)
    by_name = {n["function"].rsplit(".", 1)[-1]: n for n in out["nodes"]}
    assert "mid" in by_name and "leaf" in by_name, out["nodes"]
    assert by_name["mid"]["depth"] == 1
    assert by_name["leaf"]["depth"] == 2
    assert by_name["mid"]["via"].endswith("top")
    assert by_name["leaf"]["via"].endswith("mid")


def test_call_tree_default_depth_limits(tmp_path):
    out = queries.call_tree(_tmp_builder(tmp_path, CALLTREE_APP), "top", depth=1)
    names = [n["function"].rsplit(".", 1)[-1] for n in out["nodes"]]
    assert "mid" in names
    assert "leaf" not in names, names


def test_call_tree_tags_effects(tmp_path):
    out = queries.call_tree(_tmp_builder(tmp_path, CALLTREE_APP), "top", depth=2)
    leaf = next(n for n in out["nodes"] if n["function"].endswith("leaf"))
    assert "http" in leaf["effects"], leaf


STUB_CALLTREE_APP = {
    "svc.py": """
        class FakeDB:
            def write(self):
                pass

        def run():
            db = FakeDB()
            return db.write()
    """,
}


def test_call_tree_marks_stub_effects(tmp_path):
    out = queries.call_tree(_tmp_builder(tmp_path, STUB_CALLTREE_APP), "run", depth=1)
    write = next(n for n in out["nodes"] if n["function"].endswith("FakeDB.write"))
    assert write["effects"] == ["stub"]
    assert "stub" in out["effect_legend"]


DI_CALLTREE_APP = {
    "app.py": """
        from typing import Protocol
        from fastapi import Depends, FastAPI

        app = FastAPI()

        class SandboxBackend(Protocol):
            def create_session(self): ...
            def exec(self): ...
            def read_file(self): ...
            def write_file(self): ...
            def stat(self): ...
            def readdir(self): ...
            def persist(self): ...
            def hydrate(self): ...

        class FakeBackend:
            def create_session(self): return "session"
            def exec(self): return "exec"
            def read_file(self): return "read"
            def write_file(self): return "write"
            def stat(self): return "stat"
            def readdir(self): return "readdir"
            def persist(self): return "persist"
            def hydrate(self): return "hydrate"

        def get_backend() -> SandboxBackend:
            return FakeBackend()

        @app.post("/session")
        def create_session(backend=Depends(get_backend)):
            return backend.create_session()

        @app.post("/exec")
        def exec_cmd(backend=Depends(get_backend)):
            return backend.exec()

        @app.get("/fs")
        def fs_get(backend=Depends(get_backend)):
            return backend.read_file()

        @app.put("/fs")
        def fs_put(backend=Depends(get_backend)):
            return backend.write_file()

        @app.get("/stat")
        def stat_path(backend=Depends(get_backend)):
            return backend.stat()

        @app.get("/readdir")
        def readdir_path(backend=Depends(get_backend)):
            return backend.readdir()

        @app.post("/persist")
        def persist(backend=Depends(get_backend)):
            return backend.persist()

        @app.post("/hydrate")
        def hydrate(backend=Depends(get_backend)):
            return backend.hydrate()
    """,
}


def test_call_tree_includes_di_runtime_targets_for_handlers(tmp_path):
    builder = _tmp_builder(tmp_path, DI_CALLTREE_APP)
    expected = {
        "create_session": "FakeBackend.create_session",
        "exec_cmd": "FakeBackend.exec",
        "fs_get": "FakeBackend.read_file",
        "fs_put": "FakeBackend.write_file",
        "stat_path": "FakeBackend.stat",
        "readdir_path": "FakeBackend.readdir",
        "persist": "FakeBackend.persist",
        "hydrate": "FakeBackend.hydrate",
    }

    for handler, implementation in expected.items():
        out = queries.call_tree(builder, f"app.{handler}", depth=1)
        functions = {node["function"] for node in out["nodes"]}
        assert any(name.endswith(implementation) for name in functions), out


def test_build_flow_includes_di_runtime_target(tmp_path):
    builder = _tmp_builder(tmp_path, DI_CALLTREE_APP)
    nodes, _edges = builder.call_graph.build_flow_from(
        "exec_cmd", str(tmp_path / "app.py"), max_depth=1,
    )
    assert any(name.endswith("FakeBackend.exec") for name in nodes), nodes


FACTORY_DI_APP = {
    "app.py": """
        from typing import Protocol
        from fastapi import FastAPI

        class SandboxBackend(Protocol):
            def exec(self): ...
            def read_file(self): ...
            def write_file(self): ...
            def persist(self): ...
            def hydrate(self): ...

        class FakeBackend:
            def exec(self): return "exec"
            def read_file(self): return "read"
            def write_file(self): return "write"
            def persist(self): return "persist"
            def hydrate(self): return "hydrate"

        def create_app(backend: SandboxBackend):
            app = FastAPI()

            @app.post("/exec")
            def exec_cmd(): return backend.exec()

            @app.get("/fs")
            def fs_get(): return backend.read_file()

            @app.put("/fs")
            def fs_put(): return backend.write_file()

            @app.post("/persist")
            def persist(): return backend.persist()

            @app.post("/hydrate")
            def hydrate(): return backend.hydrate()

            return app

        def build_backend():
            return FakeBackend()

        app = create_app(build_backend())
    """,
}


def test_app_factory_closure_resolves_contract_and_runtime_targets(tmp_path):
    builder = _tmp_builder(tmp_path, FACTORY_DI_APP)
    expected = {
        "exec_cmd": "exec",
        "fs_get": "read_file",
        "fs_put": "write_file",
        "persist": "persist",
        "hydrate": "hydrate",
    }

    for handler, method in expected.items():
        out = queries.call_tree(builder, f"app.create_app.{handler}", depth=1)
        functions = {node["function"] for node in out["nodes"]}
        assert any(name.endswith(f"FakeBackend.{method}") for name in functions), out
        assert any(name.endswith(f"SandboxBackend.{method}") for name in functions), out

    for target in ("app.FakeBackend.exec", "app.SandboxBackend.exec"):
        out = queries.who_calls(builder, target)
        callers = {node["caller"] for node in out["callers"]}
        assert "app.create_app.exec_cmd" in callers, out

    summary = queries.what_does(builder, "app.create_app.exec_cmd")
    resolved = set(summary["calls"]["resolved_callees"])
    assert "app.FakeBackend.exec" in resolved, summary
    assert "app.SandboxBackend.exec" in resolved, summary


TESTNODE_APP = {
    "svc.py": """
        def handler():
            return helper()
    """,
    "tests/helpers.py": """
        def helper():
            return 1
    """,
}


def test_call_tree_excludes_test_path_nodes_by_default(tmp_path):
    out = queries.call_tree(_tmp_builder(tmp_path, TESTNODE_APP), "handler")
    locs = [n["location"] for n in out["nodes"]]
    assert not any("/tests/" in loc for loc in locs), out["nodes"]


def test_call_tree_include_tests_keeps_test_nodes(tmp_path):
    out = queries.call_tree(
        _tmp_builder(tmp_path, TESTNODE_APP), "handler", include_tests=True)
    names = [n["function"].rsplit(".", 1)[-1] for n in out["nodes"]]
    assert "helper" in names, out["nodes"]


def test_call_tree_handles_recursion(tmp_path):
    out = queries.call_tree(_tmp_builder(tmp_path, {
        "rec.py": """
            def a():
                return b()

            def b():
                return a()
        """,
    }), "a", depth=5)
    names = [n["function"] for n in out["nodes"]]
    assert len(names) == len(set(names)), names


def test_reaching_conditions_detects_dead_code(tmp_path):
    out = queries.reaching_conditions(_tmp_builder(tmp_path, {
        "d.py": """
            def f(x):
                return x
                y = x + 1
                return y
        """,
    }), "f")
    assert out.get("dead_code"), out
