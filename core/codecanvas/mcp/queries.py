"""Agent-facing query functions over the analysis engine.

Each function takes an analyzed FlowGraphBuilder and returns a compact,
JSON-serializable dict. No FlowGraph IR, no coordinates.
"""
from __future__ import annotations

import difflib
import os

from codecanvas.mcp.answers import capped


def _location(func) -> str:
    return f"{func.file_path}:{func.line_start}"


def _is_test_path(fp: str) -> bool:
    """True if a file path looks like test code (dir segment or filename)."""
    parts = (fp or "").replace("\\", "/").split("/")
    if any(seg in ("tests", "test") for seg in parts):
        return True
    base = parts[-1] if parts else ""
    return base.startswith("test_") or base.endswith("_test.py")


def _rank_key(cg, f) -> tuple:
    """Ranking key, higher is better: (non_test, concrete, fan_in)."""
    non_test = not _is_test_path(f.file_path or "")
    concrete = not (f.is_protocol or f.is_abstract)
    fan_in = len(cg.get_callers(f.qualified_name))
    return (non_test, concrete, fan_in)


def _rank_and_select(cg, ref: str, cands: list):
    """Rank ambiguous candidates; auto-select a dominant one or return a list.

    Dominance: the top candidate wins outright on the categorical key
    (non_test, concrete); on a categorical tie it must also dominate
    fan-in by a clear margin (>= 2x and >= +2) to auto-select. Otherwise
    a ranked, best-first candidate list is returned for the agent.
    """
    keyed = sorted(
        ((_rank_key(cg, f), f) for f in cands),
        key=lambda kf: kf[0],
        reverse=True,
    )
    (top_key, top), (second_key, _second) = keyed[0], keyed[1]
    top_cat, second_cat = top_key[:2], second_key[:2]
    if top_cat > second_cat or (
        top_cat == second_cat
        and top_key[2] >= 2 * second_key[2]
        and top_key[2] - second_key[2] >= 2
    ):
        return top, None
    return None, {
        "error": f"Ambiguous '{ref}' ({len(cands)} matches); pick one by qualified_name.",
        "candidates": [
            {
                "qualified_name": f.qualified_name,
                "location": _location(f),
                "kind": "method" if f.class_name else "function",
                "is_interface": bool(f.is_protocol or f.is_abstract),
                "callers": key[2],
            }
            for key, f in keyed[:10]
        ],
    }


def resolve_function(builder, ref: str):
    """Resolve a function reference to a FunctionDef.

    Accepts a qualified name, a bare name (if unique), or ``file:line``.
    Returns (func, None) or (None, {"error", "suggestions"}).
    """
    cg = builder.call_graph
    funcs = cg.all_functions()

    # 1. Exact qualified name.
    exact = cg.get_function(ref)
    if exact is not None:
        return exact, None

    # 2. file:line form.
    if ":" in ref:
        path_part, _, line_part = ref.rpartition(":")
        if line_part.isdigit():
            line = int(line_part)
            matches = []
            for f in funcs:
                fp = f.file_path or ""
                same = fp.endswith(path_part) or path_part.endswith(os.path.basename(fp))
                end = f.line_end or f.line_start
                if same and f.line_start <= line <= end:
                    matches.append(f)
            if len(matches) == 1:
                return matches[0], None
            if len(matches) > 1:
                return _rank_and_select(cg, ref, matches)

    # 3. Bare name or dot-boundary suffix (Class.method / module.Class.method).
    cands = [f for f in funcs
             if f.qualified_name == ref or f.qualified_name.endswith("." + ref)]
    if len(cands) == 1:
        return cands[0], None
    if len(cands) > 1:
        return _rank_and_select(cg, ref, cands)

    # 4. Miss -> near-name suggestions.
    names = [f.name for f in funcs]
    close = difflib.get_close_matches(ref, names, n=5)
    return None, {
        "error": f"No function matching '{ref}'.",
        "suggestions": close,
    }


def list_entrypoints(builder, filter=None, kind=None,
                     include_tests=False) -> dict:
    """List discovered entrypoints (APIs + scripts + functions).

    Optional narrowing, applied BEFORE the output cap so a target in a
    large project is not hidden by truncation:
    - ``kind``: keep only entrypoints of this kind (e.g. "api", "script").
    - ``filter``: case-insensitive substring matched against the method,
      path, handler, id, and tags.
    - ``include_tests``: by default entrypoints whose handler lives under a
      test path (``tests/`` dir, ``test_*.py`` / ``*_test.py``) are hidden,
      since test-app fixtures are not real service routes. Set True to keep
      them.
    """
    eps = builder.get_entrypoints()

    hidden_tests = 0
    if not include_tests:
        kept = [e for e in eps if not _is_test_path(e.handler_file or "")]
        hidden_tests = len(eps) - len(kept)
        eps = kept

    if kind:
        eps = [e for e in eps if e.kind == kind]
    if filter:
        needle = filter.lower()
        eps = [
            e for e in eps
            if needle in (
                f"{e.method} {e.path} {e.handler_name} {e.id} "
                f"{' '.join(e.tags or [])}"
            ).lower()
        ]

    rows = [
        {
            "id": e.id,
            "kind": e.kind,
            "method": e.method,
            "path": e.path,
            "handler": e.handler_name,
            "location": f"{e.handler_file}:{e.handler_line}",
            "tags": e.tags,
        }
        for e in eps
    ]
    rows, cap_note = capped(rows)
    out = {"count": len(eps), "entrypoints": rows}
    notes = []
    if hidden_tests:
        notes.append(
            f"{hidden_tests} test-fixture entrypoint(s) hidden; "
            f"pass include_tests=True to show them."
        )
    if cap_note:
        notes.append(cap_note)
    if notes:
        out["note"] = " ".join(notes)
    return out


def who_calls(builder, function: str, depth: int = 1, filter=None) -> dict:
    """Callers of a function (ground-truth reverse edges).

    ``depth`` controls how many hops of the reverse call tree to walk:
    - ``depth=1`` (default): direct callers only.
    - ``depth=N``: transitive callers up to N hops. Each row carries its
      ``depth`` (hops from the target) and ``callee`` (the function it calls
      on the traced path). The walk is breadth-first and dedups by qualified
      name, so cycles/recursion terminate and no caller is listed twice.

    ``filter`` is a case-insensitive substring matched against each row's
    caller, location, and callee. It is applied BEFORE the output cap, so a
    specific caller in a heavily-called function is not hidden by truncation.
    """
    func, err = resolve_function(builder, function)
    if err is not None:
        return err
    cg = builder.call_graph
    depth = max(1, int(depth))

    rows = []
    visited = {func.qualified_name}
    frontier = [func]  # functions whose callers we still need to expand
    for hop in range(1, depth + 1):
        next_frontier = []
        for callee in frontier:
            for caller, ref in cg.get_callers(callee.qualified_name):
                if caller.qualified_name in visited:
                    continue
                visited.add(caller.qualified_name)
                rows.append({
                    "caller": caller.qualified_name,
                    "location": _location(caller),
                    "relation": ref.relation,
                    "condition": ref.condition,
                    "depth": hop,
                    "callee": callee.qualified_name,
                })
                next_frontier.append(caller)
        if not next_frontier:
            break
        frontier = next_frontier

    if filter:
        needle = filter.lower()
        rows = [
            r for r in rows
            if needle in f"{r['caller']} {r['location']} {r['callee']}".lower()
        ]

    rows, note = capped(rows)
    out = {"function": func.qualified_name, "callers": rows}
    if note:
        out["note"] = note
    return out


def _summarize_calls(func) -> dict:
    db, http, raises, callees = [], [], [], []
    for c in func.calls:
        if c.is_raise:
            raises.append({"status": c.raise_status, "exception": c.func_name})
        elif c.is_db_call:
            db.append({"op": (c.db_detail or {}).get("operation"),
                       "model": (c.db_detail or {}).get("model"),
                       "call": c.func_name})
        elif c.is_http_call:
            http.append({"method": (c.http_detail or {}).get("method"),
                         "call": c.func_name})
        else:
            callees.append(c.func_name)
    # Dedup callees preserving order.
    seen, uniq = set(), []
    for name in callees:
        if name not in seen:
            seen.add(name)
            uniq.append(name)
    uniq, _ = capped(uniq)
    return {"db": db, "http": http, "raises": raises, "callees": uniq}


def what_does(builder, function: str) -> dict:
    """Summarize what a function does (signature + effects), no source read."""
    from codecanvas.graph.impact import ImpactAnalyzer

    func, err = resolve_function(builder, function)
    if err is not None:
        return err

    kw = "async def" if func.is_async else "def"
    ret = f" -> {func.return_annotation}" if func.return_annotation else ""
    signature = f"{kw} {func.name}({', '.join(func.params)}){ret}"

    return {
        "function": func.qualified_name,
        "async": func.is_async,
        "signature": signature,
        "docstring": (func.docstring or "").strip(),
        "calls": _summarize_calls(func),
        "risk": ImpactAnalyzer._compute_function_risk(func),
    }


def _diff_non_python_files(diff_text: str) -> list[str]:
    """Changed non-Python file paths from a unified diff (sorted, unique).

    Mirrors ``parse_unified_diff``'s ``+++ b/<path>`` header scan, but keeps
    only the paths it drops (non ``.py``) so the agent still learns which
    files changed even when no Python function was touched.
    """
    files = set()
    for line in (diff_text or "").splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            if path and path != "/dev/null" and not path.endswith(".py"):
                files.add(path)
    return sorted(files)


def analyze_impact(builder, diff_text: str | None = None,
                   git_ref: str | None = None) -> dict:
    """Impact of a change: changed functions -> affected endpoints.

    Uses flow_builder=None so no FlowGraph is ever built (risk comes from
    the standalone signal-based score).
    """
    from codecanvas.graph.impact import ImpactAnalyzer

    if not diff_text and git_ref is not None:
        from codecanvas.graph.impact import _is_safe_git_ref
        if not _is_safe_git_ref(git_ref):
            return {"error": f"Invalid git_ref: {git_ref!r}. "
                             f"Expected a git revision or range like 'HEAD~1..HEAD'."}

    analyzer = ImpactAnalyzer(
        builder.call_graph, builder.project_root,
        entrypoints=builder.get_entrypoints(), flow_builder=None,
    )
    if diff_text:
        result = analyzer.analyze_diff(diff_text)
    else:
        result = analyzer.analyze_git_ref(git_ref or "HEAD~1..HEAD")

    changed = [
        {"function": f.qualified_name, "location": f"{f.file_path}:{f.line_start}",
         "risk": f.risk_score, "change_type": f.change_type}
        for f in result.affected_functions
    ]
    endpoints = [
        {"method": e.method, "path": e.path, "via": e.affected_functions,
         "call_depth": e.max_depth, "risk": e.aggregate_risk}
        for e in result.affected_endpoints
    ]
    changed, cnote = capped(changed)
    endpoints, enote = capped(endpoints)

    skipped = _diff_non_python_files(diff_text) if diff_text else []
    summary = result.summary
    if skipped and not changed:
        # No Python function changed, but the diff did touch other files —
        # say so instead of the bare "No Python changes detected."
        summary = (f"No Python changes detected; "
                   f"{len(skipped)} non-Python file(s) changed.")

    out = {"summary": summary,
           "changed_functions": changed,
           "affected_endpoints": endpoints}
    if skipped:
        out["skipped_files"] = skipped
    note = "; ".join(n for n in (cnote, enote) if n)
    if note:
        out["note"] = note
    return out


def function_flow(builder, function: str) -> dict:
    """Return a de-noised control-flow outline of a function.

    Preserves branch/loop/try nesting, early returns (with dict-key shape),
    raises, and meaningful calls — dropping logging, docstrings, and
    literal-only assignments — so the logic can be grasped without reading
    the full body.
    """
    from codecanvas.mcp import outline

    func, err = resolve_function(builder, function)
    if err is not None:
        return err
    ast_node = builder.call_graph.get_ast_node(func.qualified_name)
    import ast as _ast
    if not isinstance(ast_node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
        return {
            "error": f"No function body available for '{func.qualified_name}' "
                     f"(it may be a class or an unparsed definition).",
        }
    lines, truncated = outline.function_flow_lines(ast_node)
    out = {
        "function": func.qualified_name,
        "location": f"{func.file_path}:{func.line_start}",
        "flow": lines,
    }
    if truncated:
        out["note"] = f"outline truncated at {len(lines)} lines"
    return out
