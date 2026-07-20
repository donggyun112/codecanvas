"""CodeCanvas MCP server (stdio).

Exposes precision static-analysis tools to coding agents. Tools return a
compact dict; engine errors become error dicts rather than raised exceptions
so the agent gets an actionable message. `project_path` may be passed once
and is remembered for later calls in the session (see session.resolve_project).
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from codecanvas_mcp.mcp import queries
from codecanvas_mcp.mcp.session import (
    get_builder, resolve_project, ProjectNotFoundError, NoDefaultProjectError,
)
from codecanvas_mcp.parser.call_graph import ProjectTooLargeError

mcp = FastMCP(
    "codecanvas",
    instructions=(
        "CodeCanvas answers precise questions about a Python codebase from a "
        "real call graph and control-flow graph — not text search or "
        "guesswork. Turn to it instead of grepping or reading whole files "
        "when you need to know: who calls a function and what breaks if you "
        "change it (who_calls); everything a function reaches downstream and "
        "the side effects it triggers (call_tree); what a function does at a "
        "glance (what_does); how its logic branches (function_flow); the exact "
        "conditions guarding each return/raise (reaching_conditions); where a "
        "codebase's entry points and HTTP routes live (list_entrypoints); and "
        "the blast radius of a diff or PR (analyze_impact). When a suspected "
        "bug depends on state shape, validate fields statically with "
        "validate_state_schema, then run focused synthetic or custom cases "
        "with simulate_state_transition.\n\n"
        "Pass `project_path` (the repo root) once — it is remembered for later "
        "calls in the session, so subsequent calls may omit it. Answers are "
        "compact and capped on large projects; use each tool's "
        "`filter`/`depth`/`kind` args to narrow results. Python only."
    ),
)


def _with_builder(project_path, fn):
    try:
        builder = get_builder(resolve_project(project_path))
    except (ProjectNotFoundError, NoDefaultProjectError) as e:
        return {"error": str(e)}
    except ProjectTooLargeError as e:
        return {"error": f"Project too large: {e}"}
    return fn(builder)


@mcp.tool()
def list_entrypoints(project_path: str | None = None, filter: str | None = None,
                     kind: str | None = None,
                     include_tests: bool = False) -> dict:
    """Map where a codebase starts — list its API/HTTP routes, CLI scripts,
    and entry-point functions. Reach for this first to get the lay of an
    unfamiliar project: what endpoints exist, which handler serves each
    route, where execution begins.

    On large projects the result is capped, so narrow it: `filter` is a
    case-insensitive substring matched over method/path/handler/id/tags
    (e.g. "login"), and `kind` keeps one kind ("api", "script", "function").
    Test-fixture entrypoints (handlers under `tests/`, `test_*.py`) are
    hidden by default; set `include_tests=True` to keep them.
    """
    return _with_builder(
        project_path,
        lambda b: queries.list_entrypoints(
            b, filter=filter, kind=kind, include_tests=include_tests),
    )


@mcp.tool()
def who_calls(function: str, project_path: str | None = None, depth: int = 1,
              filter: str | None = None) -> dict:
    """Find the callers of a function — who calls it, its upstream usages and
    references, the reverse call graph, and what would break if you change its
    signature. Complements `call_tree`, which walks the opposite direction
    (downstream, what the function reaches).

    `function` accepts a qualified name, bare name, file:line, or a
    scope-skipping suffix like `Class.nested` (an enclosing scope omitted). `depth=1`
    (default) returns direct callers; `depth=N` walks up to N hops of
    transitive callers, tagging each with its `depth` and the `callee` it
    calls on the traced path. Cycles/recursion terminate safely. On heavily
    called functions the result is capped, so `filter` (case-insensitive
    substring over caller/location/callee) narrows it before truncation."""
    return _with_builder(
        project_path,
        lambda b: queries.who_calls(b, function, depth=depth, filter=filter))


@mcp.tool()
def what_does(function: str, project_path: str | None = None) -> dict:
    """Get a quick summary of what a function does without reading its source
    — its signature, docstring, side effects (whether it touches the database
    or makes HTTP calls), the exceptions it can raise, and a risk rating. Use
    it to triage an unfamiliar function before deciding whether to dig into
    `function_flow` or the full source. `function` = qualified name, bare name,
    file:line, or a scope-skipping suffix like `Class.nested`."""
    return _with_builder(project_path, lambda b: queries.what_does(b, function))


@mcp.tool()
def analyze_impact(project_path: str | None = None, diff_text: str | None = None,
                   git_ref: str | None = None, include_tests: bool = False) -> dict:
    """Assess the blast radius of a change — given a diff or git ref, list the
    changed functions and which API endpoints they affect. Reach for this when
    reviewing a PR or before merging, to see what a set of edits could break
    downstream.

    Pass `diff_text` for an inline diff, or `git_ref` to diff against a ref.
    Endpoints whose handler is under a test path are hidden by default
    (consistent with `list_entrypoints`); set `include_tests=True` to keep
    them. Non-Python changed files are reported under `skipped_files`."""
    return _with_builder(
        project_path,
        lambda b: queries.analyze_impact(b, diff_text=diff_text, git_ref=git_ref,
                                         include_tests=include_tests),
    )


@mcp.tool()
def function_flow(function: str, project_path: str | None = None) -> dict:
    """Understand how a function works internally without reading the full
    source — a de-noised control-flow outline showing branch/loop/try nesting,
    early returns (with their dict-key shape), raises, and the meaningful calls
    (logging and docstrings stripped out). Reach for this to grasp complex or
    deeply-nested logic at a glance. For the exact conditions guarding each
    return/raise, use `reaching_conditions` instead. `function` = qualified
    name, bare name, file:line, or a scope-skipping suffix like `Class.nested`."""
    return _with_builder(project_path, lambda b: queries.function_flow(b, function))


@mcp.tool()
def reaching_conditions(function: str, project_path: str | None = None,
                        target: str | None = None) -> dict:
    """Find out under what conditions a function reaches each of its returns
    and raises — the guard/path conditions (the enclosing if/elif/else,
    except, and loop tests) leading to each outcome. Reach for this when
    hunting a bug in branching logic or asking "why does this hit the error
    path?": it surfaces error-path vs success-path asymmetries (e.g. a success
    response returned from an except handler), plus cyclomatic complexity and
    any unreachable/dead code. `target`: omit for all return/raise; or
    "return" / "raise" / "line:N" to focus. `function` = qualified name,
    bare name, file:line, or a scope-skipping suffix like `Class.nested`."""
    return _with_builder(
        project_path, lambda b: queries.reaching_conditions(b, function, target))


@mcp.tool()
def validate_state_schema(function: str, state_schema: dict,
                          project_path: str | None = None,
                          state_var: str = "state") -> dict:
    """Check a function's state dict/object usage against expected fields.

    Use this when a bug depends on domain state shape rather than call graph
    reachability alone. `state_schema` may be JSON-schema-like
    (`{"properties": {...}, "required": [...]}`) or a simple field mapping.
    The tool reports state reads/writes, dict-shaped returns, schema-extra
    fields, and returns missing required fields. This is a focused repro aid:
    it turns custom state assumptions into checkable evidence, but still does
    not conclusively prove runtime behavior.
    """
    return _with_builder(
        project_path,
        lambda b: queries.validate_state_schema(
            b, function, state_schema, state_var=state_var),
    )


@mcp.tool()
def simulate_state_transition(function: str, state_schema: dict,
                              cases: list[dict] | None = None,
                              invariants: list[str] | None = None,
                              overrides: list[dict] | None = None,
                              project_path: str | None = None,
                              state_var: str = "state",
                              timeout_seconds: float = 3.0,
                              max_cases: int = 12) -> dict:
    """Execute focused state-transition repro cases in isolated processes.

    Pass explicit `cases` for exact domain states, or omit them to generate a
    small set from `state_schema`. Built-in invariants include `no_exception`,
    `return_is_mapping`, `return_has_required_keys`,
    `no_unknown_return_keys`, and `state_preserves_required_keys`. Results
    include return values, state mutations, exceptions, captured output, and
    per-case violations. Use `overrides` to replace a dependency at its runtime
    lookup path with one explicit `return_value`, `return_sequence`, or `raise`
    behavior. Override calls and unused overrides are reported per case. This
    executes project code; import-time side effects cannot be overridden.
    Module-level sync and async functions are supported in this MVP.
    """
    return _with_builder(
        project_path,
        lambda b: queries.simulate_state_transition(
            b, function, state_schema, cases=cases, invariants=invariants,
            overrides=overrides,
            state_var=state_var, timeout_seconds=timeout_seconds,
            max_cases=max_cases,
        ),
    )


@mcp.tool()
def call_tree(function: str, project_path: str | None = None, depth: int = 2,
              filter: str | None = None, include_tests: bool = False) -> dict:
    """Trace everything a function reaches downstream — the forward transitive
    call tree, N hops deep, in one call instead of hopping node-by-node. Reach
    for this to see what a function ends up doing and which side effects it
    triggers transitively. The complement of `who_calls`, which walks the
    opposite direction (upstream, who calls it).

    Each node carries its `depth`, the `via` caller on the traced path, effect
    flags (db/http/raises), and risk. Only project-internal functions are
    nodes; library calls show up as the parent's effect tags. Cycle-safe
    (dedup by name). Callees resolving into a test path are dropped by default
    (usually a misresolution); set `include_tests=True` to keep them. `filter`
    narrows by substring before the cap. `function` = qualified name, bare
    name, file:line, or a scope-skipping suffix like `Class.nested`."""
    return _with_builder(
        project_path,
        lambda b: queries.call_tree(b, function, depth=depth, filter=filter,
                                    include_tests=include_tests))


def main() -> None:
    """Console entry point: run the stdio MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
