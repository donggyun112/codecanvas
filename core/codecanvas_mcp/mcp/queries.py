"""Agent-facing query functions over the analysis engine.

Each function takes an analyzed FlowGraphBuilder and returns a compact,
JSON-serializable dict. No FlowGraph IR, no coordinates.
"""
from __future__ import annotations

import difflib
import os

from codecanvas_mcp.mcp.answers import capped


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


def _miss_suggestions(cg, funcs, ref: str) -> dict:
    """Error payload for an unresolved ref.

    Suggest qualified names whose own (tail) name matches best — exact tail
    hits first, else a fuzzy match on the tail — so the agent gets a
    copy-pasteable target instead of a bare simple name.
    """
    tail = ref.rsplit(".", 1)[-1]
    hits = [f for f in funcs if f.name == tail]
    if not hits:
        close = set(difflib.get_close_matches(tail, {f.name for f in funcs}, n=5))
        hits = [f for f in funcs if f.name in close]
    hits.sort(key=lambda f: _rank_key(cg, f), reverse=True)
    return {
        "error": f"No function matching '{ref}'.",
        "suggestions": [f.qualified_name for f in hits[:5]],
    }


def _gapped_suffix_match(qname: str, ref: str) -> bool:
    """True if ``ref``'s dotted segments occur in order within ``qname``, tail-anchored.

    Matches a scope-skipping reference like ``Class.nested`` against
    ``module.Class.method.nested`` (the enclosing ``method`` omitted). The final
    segment must coincide (the function's own name) and every ``ref`` segment
    must appear in order, so ordering and the tail are enforced — this keeps the
    looser match from firing on unrelated functions.
    """
    q = qname.split(".")
    r = ref.split(".")
    if len(r) < 2 or r[-1] != q[-1]:
        return False
    i = 0
    for seg in q:
        if i < len(r) and seg == r[i]:
            i += 1
    return i == len(r)


def resolve_function(builder, ref: str):
    """Resolve a function reference to a FunctionDef.

    Accepts a qualified name, a bare name (if unique), a ``file:line``, or a
    scope-skipping suffix such as ``Class.nested`` (enclosing method omitted).
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

    # 3b. Gapped dot-boundary subsequence — fallback for a reference that skips
    #     an enclosing scope, e.g. `Class.nested` omitting the method between.
    #     Only for dotted refs; a bare miss gains nothing here.
    if len(ref.split(".")) >= 2:
        gapped = [f for f in funcs if _gapped_suffix_match(f.qualified_name, ref)]
        if len(gapped) == 1:
            return gapped[0], None
        if len(gapped) > 1:
            return _rank_and_select(cg, ref, gapped)

    # 4. Miss -> suggest qualified names whose own (tail) name matches best.
    return None, _miss_suggestions(cg, funcs, ref)


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


def _summarize_calls(cg, func) -> dict:
    db, http, raises, callees, resolved_callees = [], [], [], [], []
    for c in func.calls:
        resolved_callees.extend(
            target.qualified_name for target in cg._resolve_call_targets(c, func)
        )
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
    resolved_uniq = list(dict.fromkeys(resolved_callees))
    resolved_uniq, _ = capped(resolved_uniq)
    return {
        "db": db,
        "http": http,
        "raises": raises,
        "callees": uniq,
        "resolved_callees": resolved_uniq,
    }


def what_does(builder, function: str) -> dict:
    """Summarize what a function does (signature + effects), no source read."""
    from codecanvas_mcp.graph.impact import ImpactAnalyzer

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
        "calls": _summarize_calls(builder.call_graph, func),
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
                   git_ref: str | None = None, include_tests=False) -> dict:
    """Impact of a change: changed functions -> affected endpoints.

    Uses flow_builder=None so no FlowGraph is ever built (risk comes from
    the standalone signal-based score).

    ``include_tests``: endpoints whose handler lives under a test path are
    excluded by default (consistent with ``list_entrypoints``), since a
    change reaching a test fixture is rarely the impact the agent cares
    about. Set True to keep them.
    """
    from codecanvas_mcp.graph.impact import ImpactAnalyzer

    if not diff_text and git_ref is not None:
        from codecanvas_mcp.graph.impact import _is_safe_git_ref
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
    affected_eps = result.affected_endpoints
    hidden_test_eps = 0
    if not include_tests:
        kept = [e for e in affected_eps
                if not _is_test_path(getattr(e, "handler_file", "") or "")]
        hidden_test_eps = len(affected_eps) - len(kept)
        affected_eps = kept
    endpoints = [
        {"method": e.method, "path": e.path, "via": e.affected_functions,
         "call_depth": e.max_depth, "risk": e.aggregate_risk}
        for e in affected_eps
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
    tnote = (f"{hidden_test_eps} test-fixture endpoint(s) hidden; "
             f"pass include_tests=True to show them." if hidden_test_eps else "")
    note = "; ".join(n for n in (cnote, enote, tnote) if n)
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
    from codecanvas_mcp.mcp import outline

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


def _cyclomatic(node) -> int:
    """Approximate McCabe complexity: 1 + count of decision points."""
    import ast
    count = 1
    for n in ast.walk(node):
        if isinstance(n, (ast.If, ast.For, ast.AsyncFor, ast.While,
                          ast.ExceptHandler, ast.IfExp)):
            count += 1
        elif isinstance(n, ast.BoolOp):
            count += len(n.values) - 1
        elif isinstance(n, ast.comprehension):
            count += 1 + len(n.ifs)
        elif hasattr(ast, "match_case") and isinstance(n, ast.match_case):
            count += 1
    return count


def _yield_value(stmt):
    """If a statement is a bare ``yield``/``yield from`` expression, return its
    rendered value (for the outcome detail); else None if it holds no yield."""
    import ast
    from codecanvas_mcp.mcp import outline
    for node in ast.walk(stmt):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # don't descend into nested scopes — their yields aren't ours
            continue
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return outline._expr(node.value) if node.value is not None else ""
    return None


def _stmt_has_yield(stmt) -> bool:
    """True if a statement contains a yield not inside a nested function."""
    import ast
    for child in ast.iter_child_nodes(stmt):
        if isinstance(child, (ast.Yield, ast.YieldFrom)):
            return True
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if _stmt_has_yield(child):
            return True
    return False


def reaching_conditions(builder, function: str, target=None) -> dict:
    """Guard conditions under which each outcome (return/raise) is reached.

    This re-expresses control-flow-graph reasoning as *facts* an agent can
    act on, instead of a node/edge graph. For each outcome it reports the
    *lexically enclosing* branch guards (if/elif/else, except, loop) — enough
    to spot asymmetries like an error-path ``return`` that skips a guard the
    success path enforces (e.g. "payment saved" returned from an except).

    ``target``:
    - ``None`` (default): every return/raise/yield with its guards.
    - ``"return"`` / ``"raise"`` / ``"yield"``: only that kind.
    - ``"line:N"``: the guards enclosing the statement at line N.

    ``yield`` outcomes make this work for generators/async generators (a
    yield is an output point like a return, but does not terminate the block).

    Also returns approximate cyclomatic complexity and any statements that
    are unreachable (follow an unconditional return/raise/break in the same
    block). Guards are lexical, not full path conditions.
    """
    import ast
    from codecanvas_mcp.mcp import outline

    func, err = resolve_function(builder, function)
    if err is not None:
        return err
    node = builder.call_graph.get_ast_node(func.qualified_name)
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return {
            "error": f"No function body available for '{func.qualified_name}' "
                     f"(it may be a class or an unparsed definition).",
        }

    outcomes: list[dict] = []
    line_guards: dict[int, list[str]] = {}
    dead: list[int] = []
    try_types = (ast.Try, ast.TryStar) if hasattr(ast, "TryStar") else (ast.Try,)

    def walk(stmts, guards):
        terminated = False
        for s in stmts:
            if terminated:
                dead.append(s.lineno)
            line_guards.setdefault(s.lineno, list(guards))
            if isinstance(s, ast.Return):
                outcomes.append({"at": s.lineno, "kind": "return",
                                 "detail": outline._return_val(s.value),
                                 "guards": list(guards)})
                terminated = True
            elif isinstance(s, ast.Raise):
                outcomes.append({"at": s.lineno, "kind": "raise",
                                 "detail": outline._raise_txt(s.exc),
                                 "guards": list(guards)})
                terminated = True
            elif isinstance(s, (ast.Break, ast.Continue)):
                terminated = True
            elif isinstance(s, ast.If):
                cond = outline._expr(s.test)
                walk(s.body, guards + [cond])
                if s.orelse:
                    walk(s.orelse, guards + [f"not ({cond})"])
            elif isinstance(s, try_types):
                walk(s.body, guards)
                for h in s.handlers:
                    typ = outline._expr(h.type) if h.type else ""
                    walk(h.body, guards + [f"except {typ}".strip()])
                walk(s.orelse, guards)
                walk(s.finalbody, guards)
            elif isinstance(s, (ast.For, ast.AsyncFor, ast.While)):
                walk(s.body, guards + ["loop"])
                walk(s.orelse, guards)
            elif isinstance(s, (ast.With, ast.AsyncWith)):
                walk(s.body, guards)
            elif _stmt_has_yield(s):
                # A yield is a generator's output point — an outcome like a
                # return, but it does not terminate the block.
                outcomes.append({"at": s.lineno, "kind": "yield",
                                 "detail": _yield_value(s) or "",
                                 "guards": list(guards)})

    walk(node.body, [])

    if target is None:
        selected = outcomes
    elif target in ("return", "raise", "yield"):
        selected = [o for o in outcomes if o["kind"] == target]
    elif target.startswith("line:") and target[5:].isdigit():
        ln = int(target[5:])
        g = line_guards.get(ln)
        selected = [{"at": ln, "kind": "line", "guards": g}] if g is not None else []
    else:
        return {"error": f"Invalid target {target!r}. "
                         f"Use 'return', 'raise', 'yield', or 'line:N'."}

    out = {
        "function": func.qualified_name,
        "location": _location(func),
        "outcomes": selected,
        "cyclomatic": _cyclomatic(node),
    }
    if dead:
        out["dead_code"] = sorted(set(dead))
    return out


def _schema_fields(state_schema) -> tuple[list[str], list[str], str | None]:
    """Return (schema_keys, required_keys, error) from a small schema shape."""
    if isinstance(state_schema, (list, tuple, set)):
        keys = sorted({str(k) for k in state_schema if isinstance(k, str)})
        return keys, keys, None

    if not isinstance(state_schema, dict):
        return [], [], "state_schema must be a dict or a list of field names."

    props = state_schema.get("properties")
    required = state_schema.get("required")
    if isinstance(props, dict):
        keys = {str(k) for k in props.keys()}
    else:
        reserved = {"properties", "required", "type", "title", "description"}
        keys = {str(k) for k in state_schema.keys() if k not in reserved}

    if isinstance(required, list):
        req = {str(k) for k in required if isinstance(k, str)}
    else:
        req = set(keys)
    keys |= req
    return sorted(keys), sorted(req), None


def _literal_key(node) -> str | None:
    import ast
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _dict_keys_from_literal(node) -> tuple[list[str], bool]:
    import ast
    if not isinstance(node, ast.Dict):
        return [], True
    keys: list[str] = []
    unknown = False
    for key_node in node.keys:
        if key_node is None:
            unknown = True
            continue
        key = _literal_key(key_node)
        if key is None:
            unknown = True
        else:
            keys.append(key)
    return keys, unknown


def _state_field_from_node(node, state_var: str) -> tuple[str | None, str | None]:
    """Return (field, source_kind) for state['x'] or state.x."""
    import ast
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        if node.value.id == state_var:
            return _literal_key(node.slice), "subscript"
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id == state_var:
            return node.attr, "attribute"
    return None, None


def _ast_state_param_names(node) -> list[str]:
    names = []
    names.extend(arg.arg for arg in getattr(node.args, "posonlyargs", []))
    names.extend(arg.arg for arg in node.args.args)
    names.extend(arg.arg for arg in node.args.kwonlyargs)
    return names


def _ast_state_param_annotation(node, state_var: str) -> str | None:
    args = []
    args.extend(getattr(node.args, "posonlyargs", []))
    args.extend(node.args.args)
    args.extend(node.args.kwonlyargs)
    for arg in args:
        if arg.arg == state_var and arg.annotation is not None:
            return _expr_text(arg.annotation)
    return None


def _state_var_param_error(func, state_var: str, params: list[str]) -> dict | None:
    if state_var in params:
        return None
    return {
        "error": (
            f"state_var {state_var!r} must match the function parameter that "
            "receives the state mapping."
        ),
        "function": func.qualified_name,
        "location": _location(func),
        "state_var": state_var,
        "parameters": params,
        "hint": (
            "These state tools expect node-style functions such as "
            "def node(state). For ordinary functions, add a small wrapper or "
            "set state_var to the parameter that receives the whole state mapping."
        ),
    }


def _state_var_annotation_error(func, state_var: str, annotation: str | None) -> dict | None:
    from codecanvas_mcp.mcp.simulator import _state_mapping_annotation_error

    message = _state_mapping_annotation_error(state_var, annotation)
    if message is None:
        return None
    return {
        "error": message,
        "function": func.qualified_name,
        "location": _location(func),
        "state_var": state_var,
        "annotation": annotation,
        "hint": (
            "These state tools expect node-style functions such as "
            "def node(state: dict). Use dict, Mapping, MutableMapping, "
            "or a TypedDict-like state annotation; for ordinary scalar "
            "functions, add a small wrapper."
        ),
    }


def _target_name(node) -> str | None:
    import ast
    return node.id if isinstance(node, ast.Name) else None


def _expr_text(node) -> str:
    import ast
    try:
        text = ast.unparse(node)
    except Exception:
        text = "<expr>"
    return " ".join(text.split())


class _StateSchemaVisitor:
    """Collect local state-field reads, writes, and dict-shaped returns."""

    def __init__(self, state_var: str):
        self.state_var = state_var
        self.reads: list[dict] = []
        self.writes: list[dict] = []
        self.returns: list[dict] = []
        self._dict_vars: dict[str, dict] = {}

    def visit(self, node) -> None:
        import ast
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        method = getattr(self, f"visit_{node.__class__.__name__}", None)
        if method is not None:
            method(node)
            return
        self.generic_visit(node)

    def generic_visit(self, node) -> None:
        import ast
        for child in ast.iter_child_nodes(node):
            self.visit(child)

    def visit_Assign(self, node) -> None:
        for target in node.targets:
            self._record_target(target, node.value, node.lineno)
        self.visit(node.value)

    def visit_AnnAssign(self, node) -> None:
        self._record_target(node.target, node.value, node.lineno)
        if node.value is not None:
            self.visit(node.value)

    def visit_AugAssign(self, node) -> None:
        field, source = _state_field_from_node(node.target, self.state_var)
        if field is not None:
            self._write(field, node.lineno, source or "state")
            self._read(field, node.lineno, source or "state")
        self.visit(node.value)

    def visit_Subscript(self, node) -> None:
        import ast
        field, source = _state_field_from_node(node, self.state_var)
        if field is not None and isinstance(node.ctx, ast.Load):
            self._read(field, node.lineno, source or "state")
        self.generic_visit(node)

    def visit_Attribute(self, node) -> None:
        import ast
        field, source = _state_field_from_node(node, self.state_var)
        if field is not None and isinstance(node.ctx, ast.Load):
            self._read(field, node.lineno, source or "state")
        self.generic_visit(node)

    def visit_Call(self, node) -> None:
        import ast
        if isinstance(node.func, ast.Attribute):
            owner = node.func.value
            method = node.func.attr
            if isinstance(owner, ast.Name) and owner.id == self.state_var:
                self._handle_state_call(method, node)
                return
            if isinstance(owner, ast.Name) and owner.id in self._dict_vars:
                self._handle_dict_var_call(owner.id, method, node)
                return
        self.generic_visit(node)

    def visit_Return(self, node) -> None:
        keys, unknown = self._return_keys(node.value)
        self.returns.append({
            "at": node.lineno,
            "keys": sorted(set(keys)),
            "unknown_keys": unknown,
            "detail": _expr_text(node.value) if node.value is not None else "",
        })
        if node.value is not None:
            self.visit(node.value)

    def _record_target(self, target, value, line: int) -> None:
        import ast
        field, source = _state_field_from_node(target, self.state_var)
        if field is not None:
            self._write(field, line, source or "state")
            return

        name = _target_name(target)
        if name is not None and isinstance(value, ast.Dict):
            keys, unknown = _dict_keys_from_literal(value)
            self._dict_vars[name] = {"keys": set(keys), "unknown": unknown}
            return

        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
            var = target.value.id
            key = _literal_key(target.slice)
            if key is not None and var in self._dict_vars:
                self._dict_vars[var]["keys"].add(key)

    def _handle_state_call(self, method: str, node) -> None:
        if method in {"get", "setdefault", "pop"} and node.args:
            key = _literal_key(node.args[0])
            if key is not None:
                self._read(key, node.lineno, f"{self.state_var}.{method}")
                if method in {"setdefault", "pop"}:
                    self._write(key, node.lineno, f"{self.state_var}.{method}")
        elif method == "update" and node.args:
            keys, _unknown = _dict_keys_from_literal(node.args[0])
            for key in keys:
                self._write(key, node.lineno, f"{self.state_var}.update")

        for arg in node.args:
            self.visit(arg)
        for kw in node.keywords:
            self.visit(kw.value)

    def _handle_dict_var_call(self, name: str, method: str, node) -> None:
        if method == "update" and node.args:
            keys, unknown = _dict_keys_from_literal(node.args[0])
            self._dict_vars[name]["keys"].update(keys)
            self._dict_vars[name]["unknown"] = self._dict_vars[name]["unknown"] or unknown
        for arg in node.args:
            self.visit(arg)
        for kw in node.keywords:
            self.visit(kw.value)

    def _return_keys(self, value) -> tuple[list[str], bool]:
        import ast
        if isinstance(value, ast.Dict):
            return _dict_keys_from_literal(value)
        if isinstance(value, ast.Name):
            if value.id == self.state_var:
                return [], True
            known = self._dict_vars.get(value.id)
            if known is not None:
                return sorted(known["keys"]), bool(known["unknown"])
        return [], True

    def _read(self, field: str, line: int, source: str) -> None:
        self.reads.append({"field": field, "at": line, "source": source})

    def _write(self, field: str, line: int, source: str) -> None:
        self.writes.append({"field": field, "at": line, "source": source})


def _dedup_records(records: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for row in records:
        key = tuple(sorted(row.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def validate_state_schema(builder, function: str, state_schema,
                          state_var: str = "state") -> dict:
    """Check function state-field usage against a caller-provided schema.

    ``state_schema`` may be a JSON-schema-like object with ``properties`` and
    ``required`` or a simple mapping/list of field names. This is a focused
    static repro helper: it does not prove a bug, but it flags branch returns
    missing required state keys and state fields that are outside the schema.
    """
    import ast

    schema_keys, required_keys, schema_err = _schema_fields(state_schema)
    if schema_err is not None:
        return {"error": schema_err}
    if not state_var:
        return {"error": "state_var must be a non-empty string."}

    func, err = resolve_function(builder, function)
    if err is not None:
        return err

    node = builder.call_graph.get_ast_node(func.qualified_name)
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return {
            "error": f"No function body available for '{func.qualified_name}' "
                     f"(it may be a class or an unparsed definition).",
        }
    param_err = _state_var_param_error(func, state_var, _ast_state_param_names(node))
    if param_err is not None:
        return param_err

    visitor = _StateSchemaVisitor(state_var)
    for stmt in node.body:
        visitor.visit(stmt)

    reads = _dedup_records(visitor.reads)
    writes = _dedup_records(visitor.writes)
    returns = visitor.returns
    schema_set = set(schema_keys)
    required_set = set(required_keys)
    diagnostics = []

    seen_unknown: set[str] = set()
    if schema_set:
        for row in reads + writes:
            field = row["field"]
            if field not in schema_set and field not in seen_unknown:
                seen_unknown.add(field)
                diagnostics.append({
                    "type": "field_not_in_schema",
                    "field": field,
                    "at": row["at"],
                    "source": row["source"],
                })

    explicit_return_fields: set[str] = set()
    has_unknown_return = False
    for row in returns:
        keys = set(row["keys"])
        explicit_return_fields.update(keys)
        has_unknown_return = has_unknown_return or bool(row["unknown_keys"])
        if schema_set:
            extras = sorted(keys - schema_set)
            for field in extras:
                diagnostics.append({
                    "type": "field_not_in_schema",
                    "field": field,
                    "at": row["at"],
                    "source": "return",
                })
        if required_set and not row["unknown_keys"]:
            missing = sorted(required_set - keys)
            if missing:
                diagnostics.append({
                    "type": "missing_required_return_keys",
                    "at": row["at"],
                    "fields": missing,
                })

    observed = ({r["field"] for r in reads} |
                {w["field"] for w in writes} |
                explicit_return_fields)
    if required_set and not has_unknown_return:
        missing_observed = sorted(required_set - observed)
        if missing_observed:
            diagnostics.append({
                "type": "required_fields_not_observed",
                "fields": missing_observed,
            })

    reads, rnote = capped(reads)
    writes, wnote = capped(writes)
    returns, retnote = capped(returns)
    diagnostics, dnote = capped(diagnostics)
    note = "; ".join(n for n in (rnote, wnote, retnote, dnote) if n)

    out = {
        "function": func.qualified_name,
        "location": _location(func),
        "state_var": state_var,
        "schema_keys": schema_keys,
        "required_keys": required_keys,
        "reads": reads,
        "writes": writes,
        "returns": returns,
        "diagnostics": diagnostics,
    }
    if note:
        out["note"] = note
    return out


def simulate_state_transition(builder, function: str, state_schema: dict,
                              cases: list[dict] | None = None,
                              invariants: list[str] | None = None,
                              overrides: list[dict] | None = None,
                              state_var: str = "state",
                              timeout_seconds: float = 3.0,
                              import_timeout_seconds: float = 10.0,
                              max_cases: int = 12) -> dict:
    """Run focused state cases against a module-level function in isolation."""
    import ast
    from codecanvas_mcp.mcp.simulator import simulate

    if not state_var:
        return {"error": "state_var must be a non-empty string."}
    func, err = resolve_function(builder, function)
    if err is not None:
        return err
    if func.class_name:
        return {
            "error": "Instance and class methods are not supported by the simulator MVP.",
            "function": func.qualified_name,
            "location": _location(func),
        }
    node = builder.call_graph.get_ast_node(func.qualified_name)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        params = _ast_state_param_names(node)
        param_err = _state_var_param_error(func, state_var, params)
        if param_err is None:
            param_err = _state_var_annotation_error(
                func, state_var, _ast_state_param_annotation(node, state_var)
            )
    else:
        param_err = _state_var_param_error(
            func, state_var, [p for p in func.params if not p.startswith("*")])
    if param_err is not None:
        return param_err

    out = simulate(
        project_root=builder.project_root,
        file_path=func.file_path,
        target_name=func.name,
        state_schema=state_schema,
        cases=cases,
        invariants=invariants,
        overrides=overrides,
        state_var=state_var,
        timeout_seconds=timeout_seconds,
        import_timeout_seconds=import_timeout_seconds,
        max_cases=max_cases,
    )
    out.setdefault("function", func.qualified_name)
    out.setdefault("location", _location(func))
    return out


def _effect_tags(func) -> list[str]:
    """Compact per-node effect flags: db / http / raises."""
    tags = []
    if any(c.is_db_call for c in func.calls):
        tags.append("db")
    if any(c.is_http_call for c in func.calls):
        tags.append("http")
    if any(c.is_raise for c in func.calls):
        tags.append("raises")
    return tags


def call_tree(builder, function: str, depth: int = 2, filter=None,
              include_tests=False) -> dict:
    """Forward transitive call tree: what this function reaches, N hops down.

    Complements ``who_calls`` (reverse). Instead of hopping node-by-node,
    get the whole downstream tree in one call, each node tagged with its
    ``depth``, the ``via`` caller on the traced path, effect flags
    (db/http/raises), and risk. Only project-internal functions are nodes;
    library/builtin calls are surfaced as the parent's effect tags, not
    walked. Breadth-first with dedup by qualified name, so recursion/cycles
    terminate and no function appears twice.

    ``include_tests``: callees resolving into a test path (``tests/`` dir,
    ``test_*.py``) are dropped by default — a production function reaching
    test code is almost always a name-collision misresolution. Set True to
    keep them (e.g. when tracing test code itself).

    ``filter`` is a case-insensitive substring over function/location/via,
    applied before the output cap.
    """
    from codecanvas_mcp.graph.impact import ImpactAnalyzer

    func, err = resolve_function(builder, function)
    if err is not None:
        return err
    cg = builder.call_graph
    depth = max(1, int(depth))

    nodes = []
    visited = {func.qualified_name}
    frontier = [func]
    for hop in range(1, depth + 1):
        next_frontier = []
        for caller in frontier:
            for call in caller.calls:
                for callee in cg._resolve_call_targets(call, caller):
                    if callee.qualified_name in visited:
                        continue
                    if not include_tests and _is_test_path(callee.file_path or ""):
                        continue
                    visited.add(callee.qualified_name)
                    nodes.append({
                        "function": callee.qualified_name,
                        "location": _location(callee),
                        "depth": hop,
                        "via": caller.qualified_name,
                        "effects": _effect_tags(callee),
                        "risk": ImpactAnalyzer._compute_function_risk(callee),
                    })
                    next_frontier.append(callee)
        if not next_frontier:
            break
        frontier = next_frontier

    if filter:
        needle = filter.lower()
        nodes = [
            n for n in nodes
            if needle in f"{n['function']} {n['location']} {n['via']}".lower()
        ]

    nodes, note = capped(nodes)
    out = {"function": func.qualified_name, "location": _location(func),
           "nodes": nodes}
    if note:
        out["note"] = note
    return out
