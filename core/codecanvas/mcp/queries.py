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
