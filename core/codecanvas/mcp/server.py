"""CodeCanvas MCP server (stdio).

Exposes precision static-analysis tools to coding agents. Every tool takes
a project_path and returns a compact dict; engine errors become error dicts
rather than raised exceptions so the agent gets an actionable message.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from codecanvas.mcp import queries
from codecanvas.mcp.session import get_builder, ProjectNotFoundError
from codecanvas.parser.call_graph import ProjectTooLargeError

mcp = FastMCP("codecanvas")


def _with_builder(project_path: str, fn):
    try:
        builder = get_builder(project_path)
    except ProjectNotFoundError as e:
        return {"error": str(e)}
    except ProjectTooLargeError as e:
        return {"error": f"Project too large: {e}"}
    return fn(builder)


@mcp.tool()
def list_entrypoints(project_path: str, filter: str | None = None,
                     kind: str | None = None,
                     include_tests: bool = False) -> dict:
    """List API/script/function entrypoints discovered in the project.

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
def who_calls(project_path: str, function: str, depth: int = 1,
              filter: str | None = None) -> dict:
    """Find callers of a function (qualified name, bare name, or file:line).

    `depth=1` (default) returns direct callers; `depth=N` walks up to N hops
    of transitive callers, tagging each with its `depth` and the `callee` it
    calls on the traced path. Cycles/recursion terminate safely. On heavily
    called functions the result is capped, so `filter` (case-insensitive
    substring over caller/location/callee) narrows it before truncation."""
    return _with_builder(
        project_path,
        lambda b: queries.who_calls(b, function, depth=depth, filter=filter))


@mcp.tool()
def what_does(project_path: str, function: str) -> dict:
    """Summarize a function: signature, docstring, db/http/raise effects, risk."""
    return _with_builder(project_path, lambda b: queries.what_does(b, function))


@mcp.tool()
def analyze_impact(project_path: str, diff_text: str | None = None,
                   git_ref: str | None = None) -> dict:
    """Given a diff or git ref, list changed functions and affected endpoints."""
    return _with_builder(
        project_path,
        lambda b: queries.analyze_impact(b, diff_text=diff_text, git_ref=git_ref),
    )


@mcp.tool()
def function_flow(project_path: str, function: str) -> dict:
    """Control-flow outline of a function: branch/loop/try nesting, early
    returns (with dict-key shape), raises, and meaningful calls, de-noised
    (no logging/docstrings). Use to grasp complex logic without reading the
    full source. `function` = qualified name, bare name, or file:line."""
    return _with_builder(project_path, lambda b: queries.function_flow(b, function))


@mcp.tool()
def reaching_conditions(project_path: str, function: str,
                        target: str | None = None) -> dict:
    """Guards under which each return/raise in a function is reached.

    Re-expresses control-flow reasoning as facts: for each outcome, the
    lexically enclosing branch conditions (if/elif/else, except, loop).
    Surfaces error-path vs success-path asymmetries (e.g. a success response
    returned from an except handler), plus cyclomatic complexity and any
    unreachable statements. `target`: omit for all return/raise; or
    "return" / "raise" / "line:N" to focus. `function` = qualified name,
    bare name, or file:line."""
    return _with_builder(
        project_path, lambda b: queries.reaching_conditions(b, function, target))


def main() -> None:
    """Console entry point: run the stdio MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
