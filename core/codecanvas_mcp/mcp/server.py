"""CodeCanvas MCP server (stdio).

Exposes precision static-analysis tools to coding agents. Tools return a
compact dict; engine errors become error dicts rather than raised exceptions
so the agent gets an actionable message. `project_path` may be passed once
and is remembered for later calls in the session (see session.resolve_project).
"""
from __future__ import annotations

from mcp.server import MCPServer

from codecanvas_mcp.mcp import queries
from codecanvas_mcp.mcp.session import (
    get_builder, project_status as inspect_project_status, resolve_project,
    AmbiguousProjectRootError, ProjectNotFoundError, NoDefaultProjectError,
)
from codecanvas_mcp.parser.call_graph import ProjectTooLargeError

mcp = MCPServer(
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
        "the blast radius of a diff or PR (analyze_impact). Use verify_claim to "
        "combine call paths and branch guards before stating a reachability "
        "claim as fact. When a suspected "
        "bug depends on state shape, validate fields statically with "
        "validate_state_schema, then run focused synthetic or custom cases "
        "with simulate_state_transition.\n\n"
        "Pass `project_path` (the repo root) once — it is remembered for later "
        "calls in the session, so subsequent calls may omit it. Answers are "
        "compact and capped on large projects; use each tool's "
        "`filter`/`depth`/`kind` args to narrow results. Python only."
    ),
)


def _evidence_metadata(payload: dict) -> dict:
    confidences: list[str] = []
    ambiguous: list[dict] = []
    truncated = bool(payload.get("truncated"))

    def walk(value):
        nonlocal truncated
        if isinstance(value, dict):
            confidence = value.get("confidence")
            if confidence in {"definite", "high", "inferred"}:
                confidences.append(confidence)
                if confidence == "inferred":
                    identity = {
                        key: value[key]
                        for key in ("function", "caller", "callee", "via", "location")
                        if key in value
                    }
                    if identity and identity not in ambiguous:
                        ambiguous.append(identity)
            note = value.get("note")
            if isinstance(note, str) and "truncated" in note.lower():
                truncated = True
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    # Claim responses select their strongest viable witness. Alternative paths
    # remain visible for audit, but must not downgrade the selected evidence.
    walk(payload.get("witness_path", payload))
    inferred_count = confidences.count("inferred")
    grade = (
        "inferred" if inferred_count
        else "high" if "high" in confidences
        else "definite"
    )
    safe = (
        not inferred_count
        and not ambiguous
        and not truncated
        and not payload.get("requires_root_selection", False)
    )
    metadata = {
        "evidence_grade": grade,
        "inferred_edge_count": inferred_count,
        "ambiguous_calls": ambiguous,
        "truncated": truncated,
        "safe_to_summarize": safe,
    }
    if inferred_count:
        metadata["response_guidance"] = (
            "Do not turn inferred call edges into unconditional claims. "
            "Name every ambiguous candidate or return an uncertain verdict."
        )
    elif truncated:
        metadata["response_guidance"] = (
            "Do not summarize this response as complete; narrow the query first."
        )
    elif payload.get("requires_root_selection"):
        metadata["response_guidance"] = (
            "Select one candidate analysis root before making code claims."
        )
    return metadata


def _decorate_response(payload: dict, analysis_root: str) -> dict:
    payload.setdefault("analysis_root", analysis_root)
    for key, value in _evidence_metadata(payload).items():
        payload.setdefault(key, value)
    return payload


def _with_builder(project_path, fn):
    try:
        root = resolve_project(project_path)
        builder = get_builder(root)
    except AmbiguousProjectRootError as e:
        return {
            "error": str(e),
            "analysis_root": e.requested,
            "candidate_roots": e.candidates,
            "evidence_grade": "inferred",
            "inferred_edge_count": 0,
            "ambiguous_calls": [],
            "truncated": False,
            "safe_to_summarize": False,
            "response_guidance": (
                "Select one candidate analysis root before making code claims."
            ),
        }
    except (ProjectNotFoundError, NoDefaultProjectError) as e:
        return {"error": str(e)}
    except ProjectTooLargeError as e:
        return {"error": f"Project too large: {e}"}
    return _decorate_response(fn(builder), root)


@mcp.tool()
def list_entrypoints(project_path: str | None = None, filter: str | None = None,
                     kind: str | None = None,
                     include_tests: bool = False) -> dict:
    """Map where a codebase starts — list its API/HTTP routes, CLI scripts,
    public library exports, and entry-point functions. Reach for this first to
    get the lay of an unfamiliar project: what endpoints exist, which handler
    serves each route, where execution begins.

    For a library, `kind="export"` is its public API surface, taken from each
    distributed package's `__all__` and `[project.scripts]` and resolved to
    where each name is defined. Every export is tagged with its distribution.

    On large projects the result is capped, so narrow it: `filter` is a
    case-insensitive substring matched over method/path/handler/id/tags
    (e.g. "login", "StateGraph", or a package name), and `kind` keeps one kind
    ("api", "script", "export", "function"). When the list is truncated the
    note names every package that has exports, so a symbol you cannot see is
    still one `filter` away. Test-fixture entrypoints (handlers under `tests/`,
    `test_*.py`) are hidden by default; set `include_tests=True` to keep them.
    """
    return _with_builder(
        project_path,
        lambda b: queries.list_entrypoints(
            b, filter=filter, kind=kind, include_tests=include_tests),
    )


@mcp.tool()
def find_symbols(query: str, project_path: str | None = None,
                 kind: str | None = None, path: str | None = None,
                 include_tests: bool = False, limit: int = 20,
                 cursor: str | None = None,
                 search_mode: str = "hybrid",
                 min_score: float = 0.68) -> dict:
    """Find project functions, methods, and classes by name, qualified name,
    scope, acronym, or docstring meaning. Results explain matched tokens,
    character spans, and likely symbol role. Continue with `next_cursor`;
    choose `search_mode` name, semantic, or hybrid.

    `semantic` is concept-expanded lexical matching over identifier words plus
    docstrings — related wordings reach each other (throttle/limiter,
    parallel/concurrency), but it is not an embedding search, so a query
    sharing no vocabulary with the code will not find it. Every row reports
    `match.coverage`: the fraction of the query's content words the symbol
    accounts for. Ranking multiplies by coverage, so a partial overlap can
    never outrank a full one — raise `min_score` to cut partial hits."""
    return _with_builder(
        project_path,
        lambda b: queries.find_symbols(
            b, query, kind=kind, path=path, include_tests=include_tests,
            limit=limit, cursor=cursor, search_mode=search_mode,
            min_score=min_score,
        ),
    )


@mcp.tool()
def project_status(project_path: str | None = None) -> dict:
    """Inspect the active analysis root, Python file count, disk-cache state,
    and nested Python project roots. Use this when results look polluted or
    incomplete and you may need a narrower `project_path`."""
    try:
        root = resolve_project(project_path, allow_ambiguous=True)
        return _decorate_response(inspect_project_status(root), root)
    except (ProjectNotFoundError, NoDefaultProjectError) as e:
        return {"error": str(e)}


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
    file:line, or a scope-skipping suffix like `Class.nested`.

    `calls` and `effects.direct` cover this function's own call sites, and
    `risk` scores only those. `effects.transitive` names what it reaches
    through callees, each attributed to the callee that carries it via
    `effects.via` — so a thin wrapper scoring risk 0 still shows the database
    write underneath it. Use `call_tree` for the full downstream picture."""
    return _with_builder(project_path, lambda b: queries.what_does(b, function))


@mcp.tool()
def analyze_impact(project_path: str | None = None, diff_text: str | None = None,
                   git_ref: str | None = None, include_tests: bool = False) -> dict:
    """Assess the blast radius of a change — given a diff or git ref, list the
    changed functions and which entry points/public surfaces they affect
    (HTTP routes, scripts, or public function fallbacks). Reach for this when
    reviewing a PR or before merging, to see what a set of edits could break
    downstream.

    Pass `diff_text` for an inline diff, or `git_ref` to diff against a ref.
    Entry points whose handler is under a test path are hidden by default
    (consistent with `list_entrypoints`); set `include_tests=True` to keep
    them. Non-Python changed files are reported under `skipped_files`. Prefer
    `affected_entrypoints`; `affected_endpoints` is a legacy compatibility
    alias."""
    return _with_builder(
        project_path,
        lambda b: queries.analyze_impact(b, diff_text=diff_text, git_ref=git_ref,
                                         include_tests=include_tests),
    )


@mcp.tool()
def function_flow(function: str, project_path: str | None = None) -> dict:
    """Understand how a function works internally without reading the full
    source. `flow` is a structured branch tree with explicit `subject`, `scope`,
    `condition`, and `nested_subjects`; `outline` is a compatibility rendering.
    For exact return/raise guards, use `reaching_conditions`. `function` accepts
    a qualified name, bare name, file:line, or scope-skipping suffix."""
    return _with_builder(project_path, lambda b: queries.function_flow(b, function))


@mcp.tool()
def verify_claim(claim: str, project_path: str | None = None,
                 max_depth: int = 6) -> dict:
    """Verify a qualified reachability claim before summarizing it.

    Use `<source> reaches <target>`, optionally prefixed with context such as
    `root task-mode`. The result combines static call paths, structured
    `function_flow`, and `reaching_conditions`, returning `true`, `false`, or
    `uncertain` plus a counterexample/qualification. Inferred-only paths never
    receive a true verdict.

    Condition qualifiers on the prefix are evaluated, not ignored:
    `dry-run publish reaches _call_api`, `wake=false deliver reaches _send`,
    and `without OPENAI_API_KEY analyze reaches run_ai` each get checked
    against the guards on every path, and a guard requiring the opposite makes
    the verdict `false` with that guard as the counterexample.
    `applied_qualifiers` lists the conditions actually modelled. A condition no
    guard constrains lands in `unsupported_qualifiers`, caps the verdict at
    `uncertain`, and sets `safe_to_summarize: false` — report it as
    unevaluated rather than answering as if it held. Unknown prefix syntax,
    such as `priority=urgent` or a bare `banana`, is returned as an input error
    with `safe_to_summarize: false`.
    """
    return _with_builder(
        project_path,
        lambda b: queries.verify_claim(b, claim, max_depth=max_depth),
    )


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
    fields, and returns missing required fields. `state_var` must match the
    function parameter that receives the whole state mapping; these tools are
    designed for node-style functions such as `def node(state)`. This is a
    focused repro aid: it turns custom state assumptions into checkable evidence,
    but still does not conclusively prove runtime behavior.
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
                              import_timeout_seconds: float = 10.0,
                              max_cases: int = 12,
                              python_executable: str | None = None) -> dict:
    """Execute focused state-transition repro cases in isolated processes.

    Pass explicit `cases` for exact domain states, or omit them to generate a
    small schema-shaped set from `state_schema`; generated cases do not derive
    branch predicates, so use `function_flow`/`reaching_conditions` plus explicit
    cases when branch coverage matters. Built-in invariants include `no_exception`,
    `return_is_mapping`, `return_has_required_keys`,
    `no_unknown_return_keys`, and `state_preserves_required_keys`. Results
    include return values, state mutations, exceptions, captured output, and
    per-case violations. Exceptions are always reported; include `no_exception`
    when they should fail a case. `timeout_seconds` limits function execution;
    `import_timeout_seconds` separately limits fixture hydration and module setup.
    Cases may use allowlisted `$type` fixtures such as `langchain.AIMessage` and
    `langchain.ToolMessage`. Use `overrides` to replace a dependency at its runtime
    lookup path with one explicit `return_value`, `return_sequence`, or `raise`
    behavior. Override calls and unused overrides are reported per case. This
    executes trusted project code in a separate process, not a security sandbox:
    filesystem, network, subprocess, and import-time side effects remain possible
    and cannot be contained or overridden by this tool.
    `state_var` must match the parameter receiving the whole state mapping.
    Module-level sync and async node-style functions are supported in this MVP.

    The worker runs on the project's virtualenv interpreter when one is found
    (`<project>/.venv` or `venv`, then the same in the parent directory),
    otherwise on this server's interpreter — which under `uvx` cannot import the
    project's dependencies. Set `python_executable` to choose explicitly. Every
    result reports the interpreter used under `worker`; check it first when a
    case fails with an unexpected ImportError.
    """
    return _with_builder(
        project_path,
        lambda b: queries.simulate_state_transition(
            b, function, state_schema, cases=cases, invariants=invariants,
            overrides=overrides,
            state_var=state_var, timeout_seconds=timeout_seconds,
            import_timeout_seconds=import_timeout_seconds,
            max_cases=max_cases,
            python_executable=python_executable,
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
    flags (db/http/raises/stub) with `effect_scope: "direct"` — a node's flags
    are always its own, never its subtree's — and risk. The top-level
    `effects` splits the queried function's own effects (`direct`) from those
    it only reaches through callees (`transitive`, attributed by `via`). Only
    project-internal functions are nodes; library calls show up as the
    parent's effect tags. Cycle-safe
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
