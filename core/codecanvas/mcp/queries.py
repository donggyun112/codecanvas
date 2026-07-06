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
            for f in funcs:
                fp = f.file_path or ""
                same = fp.endswith(path_part) or path_part.endswith(os.path.basename(fp))
                end = f.line_end or f.line_start
                if same and f.line_start <= line <= end:
                    return f, None

    # 3. Bare name (unique last segment).
    by_name = [f for f in funcs if f.name == ref]
    if len(by_name) == 1:
        return by_name[0], None
    if len(by_name) > 1:
        return None, {
            "error": f"Ambiguous function name '{ref}' ({len(by_name)} matches). "
                     f"Use a qualified name.",
            "suggestions": [f.qualified_name for f in by_name][:10],
        }

    # 4. Miss -> near-name suggestions.
    names = [f.name for f in funcs]
    close = difflib.get_close_matches(ref, names, n=5)
    return None, {
        "error": f"No function matching '{ref}'.",
        "suggestions": close,
    }


def list_entrypoints(builder) -> dict:
    """List discovered entrypoints (APIs + scripts + functions)."""
    eps = builder.get_entrypoints()
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
    rows, note = capped(rows)
    out = {"count": len(eps), "entrypoints": rows}
    if note:
        out["note"] = note
    return out


def who_calls(builder, function: str) -> dict:
    """Direct callers of a function (ground-truth reverse edges)."""
    func, err = resolve_function(builder, function)
    if err is not None:
        return err
    cg = builder.call_graph
    callers = cg.get_callers(func.qualified_name)
    rows = [
        {
            "caller": caller.qualified_name,
            "location": _location(caller),
            "relation": ref.relation,
            "condition": ref.condition,
        }
        for caller, ref in callers
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
    out = {"summary": result.summary,
           "changed_functions": changed,
           "affected_endpoints": endpoints}
    note = "; ".join(n for n in (cnote, enote) if n)
    if note:
        out["note"] = note
    return out
