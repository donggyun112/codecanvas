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

mcp = FastMCP("codecanvas")


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
def who_calls(function: str, project_path: str | None = None, depth: int = 1,
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
def what_does(function: str, project_path: str | None = None) -> dict:
    """Summarize a function: signature, docstring, db/http/raise effects, risk."""
    return _with_builder(project_path, lambda b: queries.what_does(b, function))


@mcp.tool()
def analyze_impact(project_path: str | None = None, diff_text: str | None = None,
                   git_ref: str | None = None, include_tests: bool = False) -> dict:
    """Given a diff or git ref, list changed functions and affected endpoints.

    Endpoints whose handler is under a test path are hidden by default
    (consistent with list_entrypoints); set `include_tests=True` to keep them.
    Non-Python changed files are reported under `skipped_files`."""
    return _with_builder(
        project_path,
        lambda b: queries.analyze_impact(b, diff_text=diff_text, git_ref=git_ref,
                                         include_tests=include_tests),
    )


@mcp.tool()
def function_flow(function: str, project_path: str | None = None) -> dict:
    """Control-flow outline of a function: branch/loop/try nesting, early
    returns (with dict-key shape), raises, and meaningful calls, de-noised
    (no logging/docstrings). Use to grasp complex logic without reading the
    full source. `function` = qualified name, bare name, or file:line."""
    return _with_builder(project_path, lambda b: queries.function_flow(b, function))


@mcp.tool()
def reaching_conditions(function: str, project_path: str | None = None,
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


@mcp.tool()
def call_tree(function: str, project_path: str | None = None, depth: int = 2,
              filter: str | None = None, include_tests: bool = False) -> dict:
    """Forward transitive call tree — what a function reaches, N hops down.

    The complement of `who_calls` (reverse): get the whole downstream tree in
    one call instead of hopping node-by-node. Each node carries its `depth`,
    the `via` caller on the traced path, effect flags (db/http/raises), and
    risk. Only project-internal functions are nodes; library calls show up as
    the parent's effect tags. Cycle-safe (dedup by name). Callees resolving
    into a test path are dropped by default (usually a misresolution); set
    `include_tests=True` to keep them. `filter` narrows by substring before
    the cap. `function` = qualified name, bare name, or file:line."""
    return _with_builder(
        project_path,
        lambda b: queries.call_tree(b, function, depth=depth, filter=filter,
                                    include_tests=include_tests))


def main() -> None:
    """Console entry point: run the stdio MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
