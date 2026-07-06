"""Render a function's control-flow skeleton as a compact, de-noised outline.

Turns a function's AST into an indented outline that preserves branch / loop /
try nesting, early returns, raises, and the *meaningful* calls — while dropping
docstrings, logging, and literal-only assignments. The goal is source-level
comprehension of a function's logic without reading its full body.
"""
from __future__ import annotations

import ast

# Call receivers whose calls are pure noise for logic comprehension.
_NOISE_ROOTS = {"logger", "log", "logging", "print", "warnings"}

_EXPR_MAX = 68  # max chars for an inline expression


def _try_types() -> tuple:
    t = (ast.Try,)
    if hasattr(ast, "TryStar"):
        t = t + (ast.TryStar,)
    return t


def function_flow_lines(node: ast.AST, max_lines: int = 400) -> tuple[list[str], bool]:
    """Return (lines, truncated) for a function-def AST node.

    Each line is ``"<lineno>  <indent><text>"``. ``truncated`` is True when the
    outline was clipped at ``max_lines``.
    """
    out: list[tuple[int, int, str]] = []
    body = getattr(node, "body", [])
    _walk(list(body), 0, out)
    truncated = len(out) > max_lines
    out = out[:max_lines]
    lines = [f"{ln:>5}  {'    ' * ind}{txt}" for (ln, ind, txt) in out]
    return lines, truncated


def _walk(stmts: list, indent: int, out: list) -> None:
    for s in stmts:
        _emit(s, indent, out)


def _emit(s: ast.stmt, indent: int, out: list) -> None:
    if isinstance(s, ast.If):
        out.append((s.lineno, indent, f"if {_expr(s.test)}:"))
        _walk(s.body, indent + 1, out)
        orelse = s.orelse
        while len(orelse) == 1 and isinstance(orelse[0], ast.If):
            e = orelse[0]
            out.append((e.lineno, indent, f"elif {_expr(e.test)}:"))
            _walk(e.body, indent + 1, out)
            orelse = e.orelse
        if orelse:
            out.append((orelse[0].lineno, indent, "else:"))
            _walk(orelse, indent + 1, out)
        return

    if isinstance(s, _try_types()):
        out.append((s.lineno, indent, "try:"))
        _walk(s.body, indent + 1, out)
        for h in s.handlers:
            typ = _expr(h.type) if h.type else ""
            name = f" as {h.name}" if h.name else ""
            out.append((h.lineno, indent, f"except {typ}{name}:"))
            _walk(h.body, indent + 1, out)
        if s.orelse:
            out.append((s.orelse[0].lineno, indent, "else:"))
            _walk(s.orelse, indent + 1, out)
        if s.finalbody:
            out.append((s.finalbody[0].lineno, indent, "finally:"))
            _walk(s.finalbody, indent + 1, out)
        return

    if isinstance(s, (ast.For, ast.AsyncFor)):
        pre = "async for" if isinstance(s, ast.AsyncFor) else "for"
        out.append((s.lineno, indent, f"{pre} {_expr(s.target)} in {_expr(s.iter)}:"))
        _walk(s.body, indent + 1, out)
        return

    if isinstance(s, ast.While):
        out.append((s.lineno, indent, f"while {_expr(s.test)}:"))
        _walk(s.body, indent + 1, out)
        return

    if isinstance(s, (ast.With, ast.AsyncWith)):
        pre = "async with" if isinstance(s, ast.AsyncWith) else "with"
        items = ", ".join(_expr(i.context_expr) for i in s.items)
        out.append((s.lineno, indent, f"{pre} {items}:"))
        _walk(s.body, indent + 1, out)
        return

    if hasattr(ast, "Match") and isinstance(s, ast.Match):
        out.append((s.lineno, indent, f"match {_expr(s.subject)}:"))
        for c in s.cases:
            ln = getattr(c.pattern, "lineno", s.lineno)
            out.append((ln, indent + 1, f"case {_expr(c.pattern)}:"))
            _walk(c.body, indent + 2, out)
        return

    if isinstance(s, ast.Return):
        out.append((s.lineno, indent, f"→ return {_return_val(s.value)}"))
        return

    if isinstance(s, ast.Raise):
        out.append((s.lineno, indent, f"✗ raise {_raise_txt(s.exc)}"))
        return

    if isinstance(s, ast.Break):
        out.append((s.lineno, indent, "break"))
        return
    if isinstance(s, ast.Continue):
        out.append((s.lineno, indent, "continue"))
        return

    if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
        out.append((s.lineno, indent, f"def {s.name}(…)  # nested"))
        return  # don't descend into nested defs
    if isinstance(s, ast.ClassDef):
        out.append((s.lineno, indent, f"class {s.name}  # nested"))
        return

    line = _significant(s)
    if line is not None:
        out.append((s.lineno, indent, line))


# -- statement rendering --

def _significant(s: ast.stmt) -> str | None:
    """Render a simple statement if it carries logic signal, else None."""
    if isinstance(s, ast.Expr):
        v = s.value
        if isinstance(v, ast.Constant):
            return None  # docstring / bare literal
        if isinstance(v, (ast.Call, ast.Await)):
            if _is_noise_call(v):
                return None
            return _call_text(v)
        return None

    if isinstance(s, (ast.Assign, ast.AnnAssign)):
        value = s.value
        if value is None:
            return None
        targets = _targets(s)
        if isinstance(value, ast.Dict):
            return f"{targets} = {{{_dict_keys(value)}}}"
        if _contains_call(value):
            if _is_noise_call(value):
                return None
            return f"{targets} = {_call_text(value)}"
        if isinstance(value, (ast.Compare, ast.BoolOp)):
            return f"{targets} = {_expr(value)}"
        return None  # literal / plain name — no signal

    return None


def _targets(s) -> str:
    if isinstance(s, ast.AnnAssign):
        return _expr(s.target)
    return ", ".join(_expr(t) for t in s.targets)


def _call_text(value: ast.expr) -> str:
    """Render a call/await expression as ``[await ] receiver.method(…)``."""
    prefix = ""
    node = value
    if isinstance(node, ast.Await):
        prefix = "await "
        node = node.value
    if isinstance(node, ast.Call):
        name = _callee_name(node)
        args = "…" if (node.args or node.keywords) else ""
        return f"{prefix}{name}({args})"
    return prefix + _expr(node)


def _callee_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        base = _expr(f.value)
        return f"{base}.{f.attr}"
    return _expr(f)


def _is_noise_call(value: ast.expr) -> bool:
    node = value.value if isinstance(value, ast.Await) else value
    if not isinstance(node, ast.Call):
        return False
    root = _root_name(node.func)
    return root in _NOISE_ROOTS


def _root_name(node: ast.expr) -> str:
    while isinstance(node, ast.Attribute):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        return _root_name(node.func)
    return ""


def _contains_call(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, (ast.Call, ast.Await)):
            return True
    return False


def _dict_keys(node: ast.Dict) -> str:
    keys = []
    for k in node.keys:
        if isinstance(k, ast.Constant):
            keys.append(str(k.value))
        elif k is None:
            keys.append("**")
        else:
            keys.append(_expr(k))
    joined = ", ".join(keys)
    return joined if len(joined) <= _EXPR_MAX else joined[:_EXPR_MAX] + "…"


def _return_val(v: ast.expr | None) -> str:
    if v is None:
        return ""
    if isinstance(v, ast.Dict):
        return f"{{{_dict_keys(v)}}}"
    return _expr(v)


def _raise_txt(exc: ast.expr | None) -> str:
    if exc is None:
        return ""
    if isinstance(exc, ast.Call):
        typ = _callee_name(exc)
        if exc.args and isinstance(exc.args[0], ast.Constant):
            msg = str(exc.args[0].value)
            msg = msg if len(msg) <= 40 else msg[:40] + "…"
            return f"{typ}(\"{msg}\")"
        return f"{typ}(…)" if (exc.args or exc.keywords) else f"{typ}()"
    return _expr(exc)


def _expr(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        s = ast.unparse(node)
    except Exception:
        return "<expr>"
    s = " ".join(s.split())
    return s if len(s) <= _EXPR_MAX else s[:_EXPR_MAX] + "…"
