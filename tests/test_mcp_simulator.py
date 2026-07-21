import textwrap
import signal

import pytest

from codecanvas_mcp.mcp import queries
from codecanvas_mcp.mcp.session import get_builder


def _builder(tmp_path, source: str):
    (tmp_path / "agent.py").write_text(
        textwrap.dedent(source).strip() + "\n", encoding="utf-8"
    )
    return get_builder(str(tmp_path))


SCHEMA = {
    "type": "object",
    "properties": {
        "done": {"type": "boolean"},
        "messages": {"type": "array"},
        "remaining_steps": {"type": "integer", "minimum": 0},
    },
    "required": ["messages", "remaining_steps"],
}


def test_simulator_reproduces_missing_return_key(tmp_path):
    builder = _builder(tmp_path, """
        def next_step(state):
            if state.get("done"):
                return {"messages": []}
            return {
                "messages": [],
                "remaining_steps": state["remaining_steps"] - 1,
            }
    """)
    out = queries.simulate_state_transition(
        builder,
        "next_step",
        SCHEMA,
        cases=[{"done": True, "messages": [], "remaining_steps": 1}],
        invariants=["no_exception", "return_has_required_keys"],
    )

    assert out["failed"] == 1
    assert out["results"][0]["return_value"] == {"messages": []}
    assert out["results"][0]["violations"] == [{
        "invariant": "return_has_required_keys",
        "fields": ["remaining_steps"],
    }]


def test_simulator_supports_async_and_captures_mutation(tmp_path):
    builder = _builder(tmp_path, """
        async def next_step(state):
            state["remaining_steps"] -= 1
            return {
                "messages": state["messages"],
                "remaining_steps": state["remaining_steps"],
            }
    """)
    out = queries.simulate_state_transition(
        builder,
        "next_step",
        SCHEMA,
        cases=[{"messages": [], "remaining_steps": 2}],
        invariants=["no_exception", "return_has_required_keys"],
    )

    assert out["passed"] == 1
    assert out["results"][0]["mutated_state"]["remaining_steps"] == 1
    assert out["results"][0]["return_value"]["remaining_steps"] == 1


def test_simulator_captures_exception(tmp_path):
    builder = _builder(tmp_path, """
        def next_step(state):
            raise ValueError("bad state")
    """)
    out = queries.simulate_state_transition(
        builder, "next_step", SCHEMA, cases=[{"messages": [], "remaining_steps": 1}]
    )

    assert out["failed"] == 1
    assert out["results"][0]["exception"]["type"] == "ValueError"
    assert "bad state" in out["results"][0]["exception"]["message"]
    assert "agent.py" in out["results"][0]["exception"]["traceback"]
    assert "simulator.py" not in out["results"][0]["exception"]["traceback"]


def test_simulator_generates_schema_cases(tmp_path):
    builder = _builder(tmp_path, """
        def next_step(state):
            return state
    """)
    out = queries.simulate_state_transition(
        builder, "next_step", SCHEMA, max_cases=6
    )

    assert out["generated_cases"] is True
    assert 1 < out["case_count"] <= 6
    assert all("messages" in row["input_state"] for row in out["results"])
    assert all("remaining_steps" in row["input_state"] for row in out["results"])


def test_simulator_generated_cases_respect_common_schema_constraints(tmp_path):
    builder = _builder(tmp_path, """
        def next_step(state):
            return state
    """)
    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "minimum": 5, "maximum": 6},
            "label": {"type": "string", "minLength": 3, "maxLength": 4},
            "items": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        },
        "required": ["count", "label", "items"],
    }
    out = queries.simulate_state_transition(builder, "next_step", schema, max_cases=12)

    for row in out["results"]:
        state = row["input_state"]
        assert 5 <= state["count"] <= 6
        assert 3 <= len(state["label"]) <= 4
        assert len(state["items"]) >= 1
    assert out["generated_case_notes"]["strategy"] == (
        "required-field baseline plus one-property variations"
    )
    assert "minLength" in out["generated_case_notes"]["supported_keywords_used"]
    assert out["generated_case_notes"]["ignored_keywords"] == []
    assert out["summary"]["status"] == "passed"


def test_simulator_generated_case_notes_are_not_branch_coverage(tmp_path):
    builder = _builder(tmp_path, """
        def advance(state):
            if state["step"] >= 3:
                return {**state, "done": True}
            return {**state, "step": state["step"] + 1}
    """)
    schema = {
        "type": "object",
        "properties": {"step": {"type": "integer", "minimum": 0}},
        "required": ["step"],
    }
    out = queries.simulate_state_transition(builder, "advance", schema, max_cases=4)

    notes = out["generated_case_notes"]
    assert notes["coverage"] == "schema_shape"
    assert notes["branch_coverage"] is False
    assert "reaching_conditions" in notes["branch_coverage_note"]


def test_simulator_generated_case_notes_report_ignored_keywords(tmp_path):
    builder = _builder(tmp_path, """
        def next_step(state):
            return state
    """)
    schema = {
        "type": "object",
        "properties": {"email": {"type": "string", "format": "email"}},
        "required": ["email"],
    }
    out = queries.simulate_state_transition(builder, "next_step", schema)

    assert "format" in out["generated_case_notes"]["ignored_keywords"]


@pytest.mark.skipif(
    not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"),
    reason="requires POSIX timers",
)
def test_simulator_separates_import_and_execution_timeouts(tmp_path):
    builder = _builder(tmp_path, """
        import time

        def next_step(state):
            try:
                time.sleep(0.5)
            except Exception:
                return state
            return state
    """)

    out = queries.simulate_state_transition(
        builder,
        "next_step",
        SCHEMA,
        cases=[{"messages": [], "remaining_steps": 1}],
        timeout_seconds=0.1,
        import_timeout_seconds=2,
    )

    result = out["results"][0]
    assert result["passed"] is False
    assert result["exception"]["phase"] == "execution"
    assert result["violations"] == [{
        "invariant": "timeout",
        "phase": "execution",
        "detail": "execution exceeded 0.1 seconds.",
    }]
    assert out["summary"]["failure_kinds"] == ["timeout"]


@pytest.mark.skipif(
    not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"),
    reason="requires POSIX timers",
)
def test_simulator_reports_import_timeout_separately(tmp_path):
    builder = _builder(tmp_path, """
        import time

        time.sleep(0.5)

        def next_step(state):
            return state
    """)

    out = queries.simulate_state_transition(
        builder,
        "next_step",
        SCHEMA,
        cases=[{"messages": [], "remaining_steps": 1}],
        timeout_seconds=2,
        import_timeout_seconds=0.1,
    )

    result = out["results"][0]
    assert result["passed"] is False
    assert result["exception"]["phase"] == "import"
    assert result["violations"][0]["phase"] == "import"


def test_simulator_hydrates_allowlisted_langchain_fixture(tmp_path):
    package = tmp_path / "langchain_core"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "messages.py").write_text(textwrap.dedent("""
        class AIMessage:
            def __init__(self, content, tool_calls):
                self.content = content
                self.tool_calls = tool_calls
    """).strip() + "\n", encoding="utf-8")
    builder = _builder(tmp_path, """
        from langchain_core.messages import AIMessage

        def next_step(state):
            message = state["messages"][-1]
            assert isinstance(message, AIMessage)
            return {"route": "tools" if message.tool_calls else "end"}
    """)

    out = queries.simulate_state_transition(
        builder,
        "next_step",
        {"type": "object"},
        cases=[{
            "messages": [{
                "$type": "langchain.AIMessage",
                "content": "",
                "tool_calls": [{"name": "search", "args": {}, "id": "call-1"}],
            }],
        }],
        invariants=["no_exception", "return_is_mapping"],
    )

    result = out["results"][0]
    assert result["return_value"] == {"route": "tools"}
    assert result["mutated_state"]["messages"][0]["$type"] == (
        "langchain.AIMessage"
    )


def test_simulator_rejects_unapproved_fixture_type(tmp_path):
    builder = _builder(tmp_path, """
        def next_step(state):
            return state
    """)

    out = queries.simulate_state_transition(
        builder,
        "next_step",
        SCHEMA,
        cases=[{"$type": "os.PathLike"}],
    )

    result = out["results"][0]
    assert result["passed"] is False
    assert result["exception"]["type"] == "ValueError"
    assert "Unsupported fixture type" in result["exception"]["message"]


def test_simulator_redacts_sensitive_output(tmp_path):
    builder = _builder(tmp_path, """
        from pathlib import Path
        import sys

        def next_step(state):
            print("token=top-secret-value")
            print(f"{Path.home()}/private", file=sys.stderr)
            return state
    """)

    out = queries.simulate_state_transition(
        builder,
        "next_step",
        SCHEMA,
        cases=[{"messages": [], "remaining_steps": 1}],
    )

    result = out["results"][0]
    assert result["stdout"] == "token=<redacted>\n"
    assert "top-secret-value" not in result["stdout"]
    assert result["stderr"] == "<HOME>/private\n"


def test_simulator_loads_nested_non_package_module_with_sibling_import(tmp_path):
    workflow = tmp_path / "workflow"
    workflow.mkdir()
    (workflow / "dependency.py").write_text(
        "def value():\n    return 7\n", encoding="utf-8"
    )
    (workflow / "agent.py").write_text(textwrap.dedent("""
        from dependency import value

        def next_step(state):
            return {**state, "remaining_steps": value()}
    """).strip() + "\n", encoding="utf-8")
    builder = get_builder(str(tmp_path))

    out = queries.simulate_state_transition(
        builder,
        "next_step",
        SCHEMA,
        cases=[{"messages": [], "remaining_steps": 1}],
    )

    assert out["passed"] == 1
    assert out["results"][0]["return_value"]["remaining_steps"] == 7


def test_simulator_only_enforces_selected_exception_invariant(tmp_path):
    builder = _builder(tmp_path, """
        def next_step(state):
            raise ValueError("ignored by selected invariants")
    """)

    out = queries.simulate_state_transition(
        builder,
        "next_step",
        SCHEMA,
        cases=[{"messages": [], "remaining_steps": 1}],
        invariants=[],
    )

    assert out["passed"] == 1
    assert out["results"][0]["violations"] == []
    assert out["results"][0]["exception"]["type"] == "ValueError"


def test_simulator_rejects_non_list_invariants(tmp_path):
    builder = _builder(tmp_path, """
        def next_step(state):
            return state
    """)

    out = queries.simulate_state_transition(
        builder, "next_step", SCHEMA, invariants="no_exception"
    )

    assert out["error"] == "invariants must be a list of strings."
    assert out["function"] == "agent.next_step"
    assert out["location"].endswith("agent.py:1")


def test_simulator_rejects_additional_required_parameters(tmp_path):
    builder = _builder(tmp_path, """
        def next_step(state, client):
            return state
    """)
    out = queries.simulate_state_transition(
        builder, "next_step", SCHEMA, cases=[{"messages": [], "remaining_steps": 1}]
    )

    assert out["failed"] == 1
    assert out["results"][0]["exception"]["type"] == "TypeError"
    assert "client" in out["results"][0]["exception"]["message"]


def test_simulator_requires_state_var_to_match_parameter(tmp_path):
    builder = _builder(tmp_path, """
        def summarize_items(items):
            return {"count": len(items), "items": items}
    """)
    out = queries.simulate_state_transition(
        builder,
        "summarize_items",
        {"properties": {"items": {"type": "array"}}, "required": ["items"]},
    )

    assert out["error"].startswith("state_var 'state' must match")
    assert out["parameters"] == ["items"]
    assert "results" not in out


def test_simulator_overrides_dependency_return_and_records_calls(tmp_path):
    builder = _builder(tmp_path, """
        def load_steps(user_id):
            return 99

        def unused_dependency():
            return "real"

        def next_step(state):
            return {
                "messages": [],
                "remaining_steps": load_steps(state["user_id"]),
            }
    """)
    out = queries.simulate_state_transition(
        builder,
        "next_step",
        SCHEMA,
        cases=[{"user_id": 7, "messages": [], "remaining_steps": 1}],
        overrides=[
            {"target": "agent.load_steps", "return_value": 3},
            {"target": "agent.unused_dependency", "return_value": "fake"},
        ],
    )

    result = out["results"][0]
    assert result["return_value"]["remaining_steps"] == 3
    assert result["overrides"][0]["called"] == 1
    assert result["overrides"][0]["calls"] == [{"args": [7], "kwargs": {}}]
    assert result["unused_overrides"] == ["agent.unused_dependency"]


def test_simulator_overrides_async_dependency(tmp_path):
    builder = _builder(tmp_path, """
        async def load_steps(user_id):
            return 99

        async def next_step(state):
            steps = await load_steps(state["user_id"])
            return {"messages": [], "remaining_steps": steps}
    """)
    out = queries.simulate_state_transition(
        builder,
        "next_step",
        SCHEMA,
        cases=[{"user_id": 7, "messages": [], "remaining_steps": 1}],
        overrides=[{"target": "agent.load_steps", "return_value": 4}],
    )

    result = out["results"][0]
    assert result["passed"] is True
    assert result["return_value"]["remaining_steps"] == 4
    assert result["overrides"][0]["called"] == 1


def test_simulator_override_return_sequence(tmp_path):
    builder = _builder(tmp_path, """
        def choose():
            return 99

        def next_step(state):
            return {
                "messages": [choose(), choose()],
                "remaining_steps": state["remaining_steps"],
            }
    """)
    out = queries.simulate_state_transition(
        builder,
        "next_step",
        SCHEMA,
        cases=[{"messages": [], "remaining_steps": 1}],
        overrides=[{"target": "agent.choose", "return_sequence": ["a", "b"]}],
    )

    result = out["results"][0]
    assert result["return_value"]["messages"] == ["a", "b"]
    assert result["overrides"][0]["called"] == 2


def test_simulator_override_can_raise(tmp_path):
    builder = _builder(tmp_path, """
        def load_steps():
            return 99

        def next_step(state):
            load_steps()
            return state
    """)
    out = queries.simulate_state_transition(
        builder,
        "next_step",
        SCHEMA,
        cases=[{"messages": [], "remaining_steps": 1}],
        overrides=[{
            "target": "agent.load_steps",
            "raise": {"type": "TimeoutError", "message": "dependency timed out"},
        }],
    )

    result = out["results"][0]
    assert result["passed"] is False
    assert result["exception"]["type"] == "TimeoutError"
    assert result["overrides"][0]["called"] == 1


def test_simulator_patches_import_alias_at_lookup_location(tmp_path):
    (tmp_path / "dependency.py").write_text(
        "def load_steps():\n    return 99\n", encoding="utf-8"
    )
    builder = _builder(tmp_path, """
        from dependency import load_steps as fetch_steps

        def next_step(state):
            return {
                "messages": [],
                "remaining_steps": fetch_steps(),
            }
    """)
    out = queries.simulate_state_transition(
        builder,
        "next_step",
        SCHEMA,
        cases=[{"messages": [], "remaining_steps": 1}],
        overrides=[{"target": "agent.fetch_steps", "return_value": 5}],
    )

    result = out["results"][0]
    assert result["return_value"]["remaining_steps"] == 5
    assert result["overrides"][0]["resolved_target"] == "agent.fetch_steps"
