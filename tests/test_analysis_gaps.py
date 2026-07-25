"""Regression tests for four reported analysis-accuracy gaps.

1. ``verify_claim`` silently ignored condition qualifiers it cannot model
   (``dry-run ...``, ``without OPENAI_API_KEY ...``, ``wake=false ...``) and
   still answered ``true`` with ``safe_to_summarize: true``.
2. Calls made inside a ``lambda`` body never became call-graph edges.
3. ``find_symbols(search_mode="semantic")`` could not reach a concept whose
   wording differs from the query, and scored a one-token-of-many overlap as
   highly as a full match.
4. ``self.conn.execute("INSERT ...")`` was not recognised as a DB effect
   (risk 0), and direct vs transitive effects were not distinguished.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from codecanvas_mcp.mcp.session import get_builder
from codecanvas_mcp.mcp import queries


def _builder(tmp_path: Path, files: dict[str, str]):
    for rel, content in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")
    return get_builder(str(tmp_path))


GUARDED = {"app.py": """
    import os

    def run(dry_run: bool = False):
        if dry_run:
            return None
        return _call_api()

    def _call_api():
        return 1

    def run_analysis():
        if not os.getenv("OPENAI_API_KEY"):
            return None
        return run_ai_analysis()

    def run_ai_analysis():
        return 2

    def deliver_once(wake: bool = True):
        if not wake:
            return None
        return _send()

    def _send():
        return 3
"""}


# --------------------------------------------------------------- gap 1
class TestClaimConditionQualifiers:
    def test_dry_run_qualifier_refutes_the_claim(self, tmp_path):
        b = _builder(tmp_path, GUARDED)
        out = queries.verify_claim(b, "dry-run run reaches _call_api")
        assert out["verdict"] == "false", out
        assert out["counterexample"], out
        assert out["safe_to_summarize"] is True

    def test_missing_env_var_qualifier_refutes_the_claim(self, tmp_path):
        b = _builder(tmp_path, GUARDED)
        out = queries.verify_claim(
            b, "without OPENAI_API_KEY run_analysis reaches run_ai_analysis",
        )
        assert out["verdict"] == "false", out
        assert out["counterexample"], out

    def test_false_flag_qualifier_refutes_the_claim(self, tmp_path):
        b = _builder(tmp_path, GUARDED)
        out = queries.verify_claim(b, "wake=false deliver_once reaches _send")
        assert out["verdict"] == "false", out
        assert out["counterexample"], out

    def test_satisfied_qualifier_keeps_the_claim_true(self, tmp_path):
        b = _builder(tmp_path, GUARDED)
        out = queries.verify_claim(b, "wake=true deliver_once reaches _send")
        assert out["verdict"] == "true", out
        assert out["applied_qualifiers"], out

    def test_unmodelled_qualifier_is_reported_not_ignored(self, tmp_path):
        b = _builder(tmp_path, GUARDED)
        out = queries.verify_claim(b, "shutdown=true run reaches _call_api")
        assert out["verdict"] == "uncertain", out
        assert out["safe_to_summarize"] is False, out
        assert any(
            q["subject"] == "shutdown" for q in out["unsupported_qualifiers"]
        ), out

    def test_non_boolean_assignment_prefix_is_an_input_error(self, tmp_path):
        b = _builder(tmp_path, GUARDED)
        out = queries.verify_claim(
            b, "priority=urgent run reaches _call_api",
        )
        assert "error" in out, out
        assert out["safe_to_summarize"] is False, out
        assert out["unsupported_prefix"] == "priority=urgent"

    def test_unknown_bare_prefix_is_an_input_error(self, tmp_path):
        b = _builder(tmp_path, GUARDED)
        out = queries.verify_claim(b, "banana run reaches _call_api")
        assert "error" in out, out
        assert out["safe_to_summarize"] is False, out
        assert out["unsupported_prefix"] == "banana"

    def test_unqualified_claim_is_unaffected(self, tmp_path):
        b = _builder(tmp_path, GUARDED)
        out = queries.verify_claim(b, "run reaches _call_api")
        assert out["verdict"] == "true", out
        assert out["unsupported_qualifiers"] == []
        assert out["safe_to_summarize"] is True


# --------------------------------------------------------------- gap 2
class TestLambdaCallEdges:
    LAMBDA_APP = {"app.py": """
        def _with_builder(fn):
            return fn(1)

        def target(b, arg):
            return arg

        def outer(arg):
            return _with_builder(lambda b: target(b, arg))
    """}

    def test_call_inside_lambda_is_an_edge(self, tmp_path):
        b = _builder(tmp_path, self.LAMBDA_APP)
        callers = [c["caller"] for c in queries.who_calls(b, "target")["callers"]]
        assert any(name.endswith("outer") for name in callers), callers

    def test_verify_claim_traverses_lambda_edge(self, tmp_path):
        b = _builder(tmp_path, self.LAMBDA_APP)
        assert queries.verify_claim(b, "outer reaches target")["verdict"] == "true"

    def test_call_tree_includes_lambda_callee(self, tmp_path):
        b = _builder(tmp_path, self.LAMBDA_APP)
        names = [n["function"] for n in queries.call_tree(b, "outer", depth=2)["nodes"]]
        assert any(name.endswith("target") for name in names), names

    def test_module_qualified_call_resolves_to_an_edge(self, tmp_path):
        """A lambda body is only reachable if `module.fn()` resolves at all."""
        b = _builder(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/mod_a.py": """
                def target(x):
                    return x
            """,
            "pkg/mod_b.py": """
                from pkg import mod_a
                import pkg.mod_a as aliased

                def direct_caller(x):
                    return mod_a.target(x)

                def alias_caller(x):
                    return aliased.target(x)

                def lambda_caller(x):
                    return _run(lambda: mod_a.target(x))

                def _run(fn):
                    return fn()
            """,
        })
        callers = {c["caller"] for c in queries.who_calls(b, "target")["callers"]}
        assert callers >= {
            "pkg.mod_b.direct_caller",
            "pkg.mod_b.alias_caller",
            "pkg.mod_b.lambda_caller",
        }, callers

    def test_unimported_receiver_does_not_resolve(self, tmp_path):
        b = _builder(tmp_path, {"svc.py": """
            def target(x):
                return x

            def caller(client):
                return client.target(1)
        """})
        callers = [c["caller"] for c in queries.who_calls(b, "target")["callers"]]
        assert "svc.caller" not in callers, callers

    def test_nested_def_body_is_still_not_flattened(self, tmp_path):
        b = _builder(tmp_path, {"nested.py": """
            def sink():
                return 1

            def holder():
                def inner():
                    return sink()
                return inner
        """})
        callers = [c["caller"] for c in queries.who_calls(b, "sink")["callers"]]
        assert any(name.endswith("inner") for name in callers), callers
        assert not any(name.endswith("holder") for name in callers), callers


# --------------------------------------------------------------- gap 3
SEMANTIC_APP = {"app.py": """
    def throttle_parallel_requests(limit):
        '''Limit how many requests run at the same time.'''
        return limit

    def store_user_token(token):
        '''Persist an access token for a signed-in user.'''
        return token

    def keep_user_token(token):
        '''Cache the access token of a signed-in user for later reuse.'''
        return token

    def format_report(rows):
        '''Render rows into a plain text report.'''
        return rows
"""}


class TestSemanticSearchQuality:
    def test_concept_query_reaches_reworded_docstring(self, tmp_path):
        b = _builder(tmp_path, SEMANTIC_APP)
        out = queries.find_symbols(b, "concurrency limiter", search_mode="semantic")
        names = [row["name"] for row in out["symbols"]]
        assert "throttle_parallel_requests" in names, out

    def test_partial_token_overlap_scores_below_full_coverage(self, tmp_path):
        b = _builder(tmp_path, SEMANTIC_APP)
        out = queries.find_symbols(
            b, "cache user token", search_mode="semantic", min_score=0.0,
        )
        by_name = {row["name"]: row for row in out["symbols"]}
        assert "keep_user_token" in by_name, out
        assert "store_user_token" in by_name, out
        full = by_name["keep_user_token"]
        partial = by_name["store_user_token"]
        assert partial["score"] < full["score"], (partial, full)
        assert partial["match"]["coverage"] < 1.0
        assert full["match"]["coverage"] == 1.0

    def test_unrelated_symbol_is_not_returned(self, tmp_path):
        b = _builder(tmp_path, SEMANTIC_APP)
        out = queries.find_symbols(b, "concurrency limiter", search_mode="semantic")
        assert "format_report" not in [row["name"] for row in out["symbols"]], out

    def test_hybrid_does_not_promote_one_token_overlap(self, tmp_path):
        b = _builder(tmp_path, {
            "app.py": """
                def analyze(): pass
                def chat_handler(): pass
                def data_link(): pass
            """,
        })
        out = queries.find_symbols(b, "analyze chat data", search_mode="hybrid")
        assert out["count"] == 0, out
        assert all(row["score"] < out["min_score"] for row in out["suggestions"])


# --------------------------------------------------------------- gap 4
STORE_APP = {"store.py": """
    import sqlite3

    class Store:
        def __init__(self, path):
            self.conn = sqlite3.connect(path)

        def send_message(self, body):
            with self.conn:
                self.conn.execute(
                    "INSERT INTO messages (body) VALUES (?)", (body,)
                )
            return True

        def list_agents(self):
            return self.conn.execute("SELECT * FROM agents").fetchall()

    def caller(store, body):
        return store.send_message(body)
"""}


class TestDatabaseEffectDetection:
    def test_attribute_cursor_write_is_a_db_effect(self, tmp_path):
        b = _builder(tmp_path, STORE_APP)
        out = queries.what_does(b, "Store.send_message")
        assert out["calls"]["db"], out
        assert out["risk"] > 0, out
        assert any(
            (row.get("sql") or {}).get("table") == "messages"
            for row in out["calls"]["db"]
        ), out["calls"]["db"]

    def test_attribute_cursor_read_scores_below_write(self, tmp_path):
        b = _builder(tmp_path, STORE_APP)
        write = queries.what_does(b, "Store.send_message")["risk"]
        read = queries.what_does(b, "Store.list_agents")["risk"]
        assert 0 < read < write, (read, write)

    def test_non_db_attribute_receiver_is_not_a_db_effect(self, tmp_path):
        b = _builder(tmp_path, {"svc.py": """
            class Svc:
                def __init__(self, cache):
                    self.cache = cache

                def read(self, key):
                    return self.cache.get(key)
        """})
        assert queries.what_does(b, "Svc.read")["calls"]["db"] == []


class TestDirectVersusTransitiveEffects:
    def test_what_does_separates_direct_from_transitive(self, tmp_path):
        b = _builder(tmp_path, STORE_APP)
        leaf = queries.what_does(b, "Store.send_message")
        assert "db" in leaf["effects"]["direct"]
        assert leaf["effects"]["transitive"] == []

        wrapper = queries.what_does(b, "caller")
        assert wrapper["effects"]["direct"] == []
        assert "db" in wrapper["effects"]["transitive"], wrapper["effects"]
        assert any(
            item["effect"] == "db" and item["through"].endswith("Store.send_message")
            for item in wrapper["effects"]["via"]
        ), wrapper["effects"]

    def test_call_tree_labels_node_effects_as_direct(self, tmp_path):
        b = _builder(tmp_path, STORE_APP)
        out = queries.call_tree(b, "caller", depth=2)
        assert out["effects"]["direct"] == []
        assert "db" in out["effects"]["transitive"], out["effects"]
        node = next(
            n for n in out["nodes"] if n["function"].endswith("Store.send_message")
        )
        assert node["effect_scope"] == "direct"
        assert "db" in node["effects"]
