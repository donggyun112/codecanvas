# CodeCanvas MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose CodeCanvas's static-analysis engine to coding agents as a stdio MCP server with 4 precision tools (`list_entrypoints`, `who_calls`, `what_does`, `analyze_impact`).

**Architecture:** A thin agent-facing layer (`core/codecanvas/mcp/`) sits directly on the reused Layer-1 analysis (`CallGraphBuilder`, `ImpactAnalyzer`, entrypoint extractors) via the existing `FlowGraphBuilder` as composition/cache root. It never calls the viz `build_flow()` and never emits the Layer-2 `FlowGraph` IR. Output is token-bounded structured dicts, not node graphs.

**Tech Stack:** Python 3.10+, official `mcp` SDK (FastMCP, stdio), pytest. Reuses existing `libcst`/`ast` engine.

## Global Constraints

- Python `>=3.10` (matches `core/pyproject.toml`).
- Reuse Layer 1 (`parser/call_graph.py`, `graph/impact.py`, `parser/*_extractor.py`); do **not** modify or import the Layer-2 viz builder path for output (`FlowGraph.to_dict`, `build_flow`).
- The VS Code extension and webview must remain untouched and working.
- All tool outputs are compact JSON-serializable dicts with a short `summary`/`note`; hard-cap lists and emit an explicit `"… N more (truncated)"` note. No ELK coordinates, no `level`, no full node dumps.
- New dependency: `mcp>=1.0.0` (only `server.py` imports it).
- Tests use the in-repo fixtures `sample-fastapi/` and `sample-script/`.
- No AI attribution in commit messages.

---

### Task 1: Public query accessors on `CallGraphBuilder`

Add clean public accessors so the MCP layer doesn't reach into private state.

**Files:**
- Modify: `core/codecanvas/parser/call_graph.py` (add two methods to `CallGraphBuilder`, near the existing `get_function` at ~line 608)
- Test: `core/../tests/test_mcp_accessors.py` (repo-root `tests/`)

**Interfaces:**
- Consumes: existing `CallGraphBuilder._functions`, `CallGraphBuilder._get_callers`, `CallGraphBuilder.analyze_project`.
- Produces:
  - `CallGraphBuilder.get_callers(self, qualified_name: str) -> list[tuple[FunctionDef, CallerReference]]`
  - `CallGraphBuilder.all_functions(self) -> list[FunctionDef]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_accessors.py`:

```python
from pathlib import Path

from codecanvas.parser.call_graph import CallGraphBuilder

SAMPLE = Path(__file__).parent.parent / "sample-fastapi"


def _cg():
    cg = CallGraphBuilder(str(SAMPLE))
    cg.analyze_project()
    return cg


def test_all_functions_includes_known_symbols():
    cg = _cg()
    names = {f.name for f in cg.all_functions()}
    assert "login" in names
    assert "verify_user" in names


def test_get_callers_of_verify_user_includes_login():
    cg = _cg()
    # Resolve verify_user's qualified name.
    verify = next(f for f in cg.all_functions() if f.name == "verify_user")
    callers = cg.get_callers(verify.qualified_name)
    caller_names = {caller.name for caller, _ref in callers}
    assert "login" in caller_names


def test_get_callers_unknown_returns_empty():
    cg = _cg()
    assert cg.get_callers("does.not.Exist") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_mcp_accessors.py -v`
Expected: FAIL with `AttributeError: 'CallGraphBuilder' object has no attribute 'all_functions'`

- [ ] **Step 3: Add the accessors**

In `core/codecanvas/parser/call_graph.py`, immediately after `get_function` (~line 610), add:

```python
    def all_functions(self) -> list["FunctionDef"]:
        """Public snapshot of all discovered function definitions."""
        return list(self._functions.values())

    def get_callers(self, qualified_name: str) -> list[tuple["FunctionDef", "CallerReference"]]:
        """Public reverse-call lookup: who calls the given function.

        Returns one representative call per distinct caller. Empty list if
        the target is unknown.
        """
        target = self._functions.get(qualified_name)
        if target is None:
            return []
        return self._get_callers(target)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_mcp_accessors.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add core/codecanvas/parser/call_graph.py tests/test_mcp_accessors.py
git commit -m "Add public get_callers/all_functions accessors to CallGraphBuilder"
```

---

### Task 2: Decouple impact risk from the viz builder

`ImpactAnalyzer` only computes risk when a `flow_builder` (viz) is present. Add a standalone fallback so the MCP path (`flow_builder=None`) still produces risk scores.

**Files:**
- Modify: `core/codecanvas/graph/impact.py` (`_analyze_hunks`, insert after the hunk→function mapping loop that ends ~line 177, before `# 2. Trace upstream`)
- Test: `tests/test_mcp_impact_decouple.py`

**Interfaces:**
- Consumes: `ImpactAnalyzer._compute_function_risk` (static, ~line 240), `CallGraphBuilder.get_function`.
- Produces: no new signature — `ImpactAnalyzer(..., flow_builder=None).analyze_diff(...)` now yields non-zero `risk_score` on affected functions that have risk signals.

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_impact_decouple.py`:

```python
from pathlib import Path

from codecanvas.parser.call_graph import CallGraphBuilder
from codecanvas.graph.impact import ImpactAnalyzer
from codecanvas.parser.entrypoint_extractor import EntryPointExtractor
from codecanvas.parser.fastapi_extractor import FastAPIExtractor

SAMPLE = Path(__file__).parent.parent / "sample-fastapi"

# Diff touching the `login` handler (raises HTTPException(401) -> risk signal).
LOGIN_DIFF = """\
--- a/app/routers/auth.py
+++ b/app/routers/auth.py
@@ -14,3 +14,4 @@
 async def login(
     body: LoginRequest,
     db=Depends(get_db),
+    extra=None,
"""


def _entrypoints():
    extractor = FastAPIExtractor(str(SAMPLE))
    return EntryPointExtractor(str(SAMPLE), extractor).analyze()


def test_risk_populated_without_flow_builder():
    cg = CallGraphBuilder(str(SAMPLE))
    analyzer = ImpactAnalyzer(
        cg, str(SAMPLE), entrypoints=_entrypoints(), flow_builder=None
    )
    result = analyzer.analyze_diff(LOGIN_DIFF)
    login_funcs = [f for f in result.affected_functions if f.name == "login"]
    assert login_funcs, "login should be detected as changed"
    assert login_funcs[0].risk_score > 0, "risk must be computed without the viz builder"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_mcp_impact_decouple.py -v`
Expected: FAIL — `assert 0 > 0` (risk stays 0 because no flow_builder).

- [ ] **Step 3: Add the fallback in `_analyze_hunks`**

In `core/codecanvas/graph/impact.py`, after the loop that maps hunks to `result.affected_functions` (ends ~line 177) and before the `# 1b. Compute risk scores` / `# 2. Trace upstream` section, insert:

```python
        # Risk source: the viz FlowGraph builder gives per-node risk when
        # available (extension path). The MCP path passes flow_builder=None,
        # so fall back to the standalone signal-based score here. This keeps
        # aggregate endpoint risk meaningful without building any FlowGraph.
        if self._flow_builder is None:
            for af in result.affected_functions:
                func = self.cg.get_function(af.qualified_name)
                if func is not None:
                    af.risk_score = self._compute_function_risk(func)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_mcp_impact_decouple.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Run the existing impact test to confirm no regression**

Run: `python3 -m pytest tests/test_impact_analysis.py -v`
Expected: PASS (all existing impact tests still green)

- [ ] **Step 6: Commit**

```bash
git add core/codecanvas/graph/impact.py tests/test_mcp_impact_decouple.py
git commit -m "Decouple impact risk scoring from the viz flow builder"
```

---

### Task 3: `session.py` — cached analyzed-builder provider

Resolve a `project_path` to an analyzed, in-process-cached `FlowGraphBuilder` (reuses its call-graph + entrypoint disk caches). The MCP layer uses this as its single entry to the engine.

**Files:**
- Create: `core/codecanvas/mcp/__init__.py`
- Create: `core/codecanvas/mcp/session.py`
- Test: `tests/test_mcp_session.py`

**Interfaces:**
- Consumes: `codecanvas.graph.builder.FlowGraphBuilder` (composition root — `.call_graph`, `.get_entrypoints()`, `.extractor`).
- Produces:
  - `get_builder(project_path: str) -> FlowGraphBuilder` — analyzed (`call_graph.analyze_project()` already run), LRU-cached (max 8).
  - `ProjectNotFoundError(Exception)` — raised when `project_path` is not a directory.

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_session.py`:

```python
from pathlib import Path

import pytest

from codecanvas.mcp.session import get_builder, ProjectNotFoundError

SAMPLE = Path(__file__).parent.parent / "sample-fastapi"


def test_get_builder_returns_analyzed_builder():
    builder = get_builder(str(SAMPLE))
    # Analyzed: functions are populated.
    assert builder.call_graph.all_functions(), "call graph should be analyzed"


def test_get_builder_is_cached():
    b1 = get_builder(str(SAMPLE))
    b2 = get_builder(str(SAMPLE))
    assert b1 is b2, "same project path returns the cached builder"


def test_get_builder_missing_dir_raises():
    with pytest.raises(ProjectNotFoundError):
        get_builder("/no/such/dir/xyz")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_mcp_session.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codecanvas.mcp'`

- [ ] **Step 3: Create the package init**

Create `core/codecanvas/mcp/__init__.py`:

```python
"""Agent-facing MCP layer over the CodeCanvas analysis engine."""
```

- [ ] **Step 4: Implement `session.py`**

Create `core/codecanvas/mcp/session.py`:

```python
"""Resolve a project path to an analyzed, cached FlowGraphBuilder.

The MCP layer reuses FlowGraphBuilder purely as a composition + cache root
(call graph, entrypoint discovery, disk caches). It never calls build_flow().
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from codecanvas.graph.builder import FlowGraphBuilder

_MAX_BUILDERS = 8
_builders: "OrderedDict[str, FlowGraphBuilder]" = OrderedDict()


class ProjectNotFoundError(Exception):
    """Raised when the requested project path is not a directory."""


def get_builder(project_path: str) -> FlowGraphBuilder:
    """Return an analyzed, LRU-cached FlowGraphBuilder for ``project_path``."""
    if not Path(project_path).is_dir():
        raise ProjectNotFoundError(f"Directory not found: {project_path}")

    if project_path in _builders:
        _builders.move_to_end(project_path)
        return _builders[project_path]

    builder = FlowGraphBuilder(project_path)
    builder.call_graph.analyze_project()  # idempotent; warm via disk cache
    _builders[project_path] = builder
    while len(_builders) > _MAX_BUILDERS:
        _builders.popitem(last=False)
    return builder
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_mcp_session.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Commit**

```bash
git add core/codecanvas/mcp/__init__.py core/codecanvas/mcp/session.py tests/test_mcp_session.py
git commit -m "Add MCP session module: cached analyzed-builder provider"
```

---

### Task 4: `answers.py` — output caps + truncation helper

Shared helper enforcing the token-bounded output constraint.

**Files:**
- Create: `core/codecanvas/mcp/answers.py`
- Test: `tests/test_mcp_answers.py`

**Interfaces:**
- Produces:
  - `DEFAULT_CAP: int = 50`
  - `capped(items: list, cap: int = DEFAULT_CAP) -> tuple[list, str | None]` — returns `(items[:cap], note_or_None)` where note is `"… N more (truncated)"` when clipped.

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_answers.py`:

```python
from codecanvas.mcp.answers import capped, DEFAULT_CAP


def test_capped_under_limit_no_note():
    items, note = capped([1, 2, 3], cap=10)
    assert items == [1, 2, 3]
    assert note is None


def test_capped_over_limit_truncates_with_note():
    items, note = capped(list(range(10)), cap=4)
    assert items == [0, 1, 2, 3]
    assert note == "… 6 more (truncated)"


def test_capped_default_cap():
    items, note = capped(list(range(DEFAULT_CAP + 5)))
    assert len(items) == DEFAULT_CAP
    assert note == "… 5 more (truncated)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_mcp_answers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codecanvas.mcp.answers'`

- [ ] **Step 3: Implement `answers.py`**

Create `core/codecanvas/mcp/answers.py`:

```python
"""Output shaping helpers: keep tool payloads token-bounded."""
from __future__ import annotations

DEFAULT_CAP = 50


def capped(items: list, cap: int = DEFAULT_CAP) -> tuple[list, str | None]:
    """Clip a list to ``cap`` items, returning a truncation note if clipped."""
    if len(items) <= cap:
        return items, None
    extra = len(items) - cap
    return items[:cap], f"… {extra} more (truncated)"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_mcp_answers.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add core/codecanvas/mcp/answers.py tests/test_mcp_answers.py
git commit -m "Add MCP answers helper: list capping + truncation note"
```

---

### Task 5: `queries.py` — function resolver + `list_entrypoints`

Foundational resolver (shared by later tools) plus the first, simplest tool.

**Files:**
- Create: `core/codecanvas/mcp/queries.py`
- Test: `tests/test_mcp_queries.py`

**Interfaces:**
- Consumes: `session.get_builder`, `FlowGraphBuilder.get_entrypoints()`, `CallGraphBuilder.all_functions/get_function`, `answers.capped`.
- Produces:
  - `resolve_function(builder, ref: str) -> tuple[FunctionDef | None, dict | None]` — returns `(func, None)` on success or `(None, error_dict)` where `error_dict = {"error": str, "suggestions": [str]}`. Accepts qualified name, bare name (unique), or `file:line`.
  - `list_entrypoints(builder) -> dict` — `{"count": int, "entrypoints": [{"id","kind","method","path","handler","location","tags"}], "note"?: str}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_queries.py`:

```python
from pathlib import Path

from codecanvas.mcp.session import get_builder
from codecanvas.mcp import queries

SAMPLE = Path(__file__).parent.parent / "sample-fastapi"


def _b():
    return get_builder(str(SAMPLE))


def test_list_entrypoints_finds_login_route():
    out = queries.list_entrypoints(_b())
    assert out["count"] >= 1
    paths = [(e["method"], e["path"]) for e in out["entrypoints"]]
    assert any(m == "POST" and p.endswith("/login") for m, p in paths), paths


def test_resolve_by_bare_name():
    func, err = queries.resolve_function(_b(), "verify_user")
    assert err is None
    assert func is not None and func.name == "verify_user"


def test_resolve_unknown_returns_suggestions():
    func, err = queries.resolve_function(_b(), "verifyuser")
    assert func is None
    assert "error" in err and isinstance(err["suggestions"], list)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_mcp_queries.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codecanvas.mcp.queries'`

- [ ] **Step 3: Implement resolver + `list_entrypoints`**

Create `core/codecanvas/mcp/queries.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_mcp_queries.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add core/codecanvas/mcp/queries.py tests/test_mcp_queries.py
git commit -m "Add MCP queries: function resolver + list_entrypoints"
```

---

### Task 6: `queries.py` — `who_calls`

**Files:**
- Modify: `core/codecanvas/mcp/queries.py` (append `who_calls`)
- Test: `tests/test_mcp_queries.py` (append test)

**Interfaces:**
- Consumes: `resolve_function`, `CallGraphBuilder.get_callers`, `answers.capped`.
- Produces: `who_calls(builder, function: str) -> dict` — success `{"function": qname, "callers": [{"caller","location","relation","condition"}], "note"?}`; miss returns the resolver `error_dict`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_queries.py`:

```python
def test_who_calls_verify_user_lists_login():
    out = queries.who_calls(_b(), "verify_user")
    assert "callers" in out, out
    caller_names = [c["caller"] for c in out["callers"]]
    assert any(name.endswith(".login") or name == "login" for name in caller_names), caller_names


def test_who_calls_unknown_returns_error():
    out = queries.who_calls(_b(), "nope_nope")
    assert "error" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_mcp_queries.py -k who_calls -v`
Expected: FAIL with `AttributeError: module 'codecanvas.mcp.queries' has no attribute 'who_calls'`

- [ ] **Step 3: Implement `who_calls`**

Append to `core/codecanvas/mcp/queries.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_mcp_queries.py -k who_calls -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add core/codecanvas/mcp/queries.py tests/test_mcp_queries.py
git commit -m "Add MCP query: who_calls"
```

---

### Task 7: `queries.py` — `what_does`

Summarize a function without the agent reading it: signature, docstring, outgoing db/http/raise calls, callees, risk.

**Files:**
- Modify: `core/codecanvas/mcp/queries.py` (append `what_does` + `_summarize_calls` helper)
- Test: `tests/test_mcp_queries.py` (append test)

**Interfaces:**
- Consumes: `resolve_function`, `FunctionDef` fields (`params`, `return_annotation`, `is_async`, `docstring`, `calls`), `CallSite` fields (`is_db_call`, `db_detail`, `is_http_call`, `http_detail`, `is_raise`, `raise_status`, `func_name`), `ImpactAnalyzer._compute_function_risk`, `answers.capped`.
- Produces: `what_does(builder, function: str) -> dict` — `{"function","async","signature","docstring","calls":{"db":[],"http":[],"raises":[],"callees":[]},"risk":float}` or resolver error.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_queries.py`:

```python
def test_what_does_verify_user():
    out = queries.what_does(_b(), "verify_user")
    assert out["async"] is True
    assert "email" in out["signature"] and "password" in out["signature"]
    assert out["docstring"].startswith("Verify")
    assert "calls" in out and "callees" in out["calls"]


def test_what_does_login_reports_raise():
    out = queries.what_does(_b(), "login")
    statuses = [r.get("status") for r in out["calls"]["raises"]]
    assert 401 in statuses, out["calls"]["raises"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_mcp_queries.py -k what_does -v`
Expected: FAIL with `AttributeError: ... has no attribute 'what_does'`

- [ ] **Step 3: Implement `what_does`**

Append to `core/codecanvas/mcp/queries.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_mcp_queries.py -k what_does -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add core/codecanvas/mcp/queries.py tests/test_mcp_queries.py
git commit -m "Add MCP query: what_does"
```

---

### Task 8: `queries.py` — `analyze_impact`

The killer tool: diff (or git ref) → changed functions → affected endpoints, using `flow_builder=None`.

**Files:**
- Modify: `core/codecanvas/mcp/queries.py` (append `analyze_impact`)
- Test: `tests/test_mcp_queries.py` (append test)

**Interfaces:**
- Consumes: `session` builder (`.call_graph`, `.get_entrypoints()`), `ImpactAnalyzer` (with `flow_builder=None`), `answers.capped`.
- Produces: `analyze_impact(builder, diff_text: str | None = None, git_ref: str | None = None) -> dict` — `{"summary", "changed_functions":[{"function","location","risk","change_type"}], "affected_endpoints":[{"method","path","via","call_depth","risk"}], "note"?}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_queries.py`:

```python
VERIFY_USER_DIFF = """\
--- a/app/services/auth_service.py
+++ b/app/services/auth_service.py
@@ -12,4 +12,5 @@
     async def verify_user(self, email: str, password: str):
         user = await self.user_repo.find_by_email(email)
         if user is None:
             return None
+        # changed
"""


def test_analyze_impact_maps_verify_user_to_login_endpoint():
    out = queries.analyze_impact(_b(), diff_text=VERIFY_USER_DIFF)
    changed = [c["function"] for c in out["changed_functions"]]
    assert any(name.endswith(".verify_user") for name in changed), changed
    ep_paths = [e["path"] for e in out["affected_endpoints"]]
    assert any(p.endswith("/login") for p in ep_paths), ep_paths


def test_analyze_impact_no_changes_message():
    out = queries.analyze_impact(_b(), diff_text="not a diff")
    assert "summary" in out
    assert out["changed_functions"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_mcp_queries.py -k analyze_impact -v`
Expected: FAIL with `AttributeError: ... has no attribute 'analyze_impact'`

- [ ] **Step 3: Implement `analyze_impact`**

Append to `core/codecanvas/mcp/queries.py`:

```python
def analyze_impact(builder, diff_text: str | None = None,
                   git_ref: str | None = None) -> dict:
    """Impact of a change: changed functions -> affected endpoints.

    Uses flow_builder=None so no FlowGraph is ever built (risk comes from
    the standalone signal-based score).
    """
    from codecanvas.graph.impact import ImpactAnalyzer

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_mcp_queries.py -k analyze_impact -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Run the full queries suite**

Run: `python3 -m pytest tests/test_mcp_queries.py -v`
Expected: PASS (all query tests green)

- [ ] **Step 6: Commit**

```bash
git add core/codecanvas/mcp/queries.py tests/test_mcp_queries.py
git commit -m "Add MCP query: analyze_impact (viz-free)"
```

---

### Task 9: `server.py` — FastMCP server, tool registration, packaging

Wire the 4 queries as MCP tools over stdio, map errors, and add the console script + dependency.

**Files:**
- Create: `core/codecanvas/mcp/server.py`
- Modify: `core/pyproject.toml` (add `mcp` dep + `codecanvas-mcp` script)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `mcp.server.fastmcp.FastMCP`, `session.get_builder`, `session.ProjectNotFoundError`, all four `queries.*` functions, `call_graph.ProjectTooLargeError`.
- Produces:
  - `mcp` — a `FastMCP` instance named `"codecanvas"` with 4 registered tools.
  - `main()` — console entry that runs the stdio server.

- [ ] **Step 1: Install the MCP SDK**

Run: `cd core && pip install "mcp>=1.0.0" && cd ..`
Expected: `mcp` installed (provides `mcp.server.fastmcp` and `anyio`).

- [ ] **Step 2: Write the failing test**

Create `tests/test_mcp_server.py`:

```python
from pathlib import Path

import anyio

from codecanvas.mcp import server

SAMPLE = Path(__file__).parent.parent / "sample-fastapi"


def test_all_four_tools_registered():
    tools = anyio.run(server.mcp.list_tools)
    names = {t.name for t in tools}
    assert names == {"list_entrypoints", "who_calls", "what_does", "analyze_impact"}


def test_tool_function_returns_dict():
    # The decorated tool functions remain directly callable.
    out = server.list_entrypoints(str(SAMPLE))
    assert isinstance(out, dict) and "entrypoints" in out


def test_tool_missing_project_returns_error_dict():
    out = server.list_entrypoints("/no/such/dir")
    assert "error" in out
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_mcp_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codecanvas.mcp.server'`

- [ ] **Step 4: Implement `server.py`**

Create `core/codecanvas/mcp/server.py`:

```python
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
def list_entrypoints(project_path: str) -> dict:
    """List API/script/function entrypoints discovered in the project."""
    return _with_builder(project_path, queries.list_entrypoints)


@mcp.tool()
def who_calls(project_path: str, function: str) -> dict:
    """Find direct callers of a function (qualified name, bare name, or file:line)."""
    return _with_builder(project_path, lambda b: queries.who_calls(b, function))


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


def main() -> None:
    """Console entry point: run the stdio MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_mcp_server.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Add dependency + console script to `pyproject.toml`**

In `core/pyproject.toml`, add `"mcp>=1.0.0"` to `[project].dependencies`:

```toml
dependencies = [
    "fastapi",
    "uvicorn",
    "libcst>=1.0.0",
    "httpx>=0.24.0",
    "mcp>=1.0.0",
]
```

And add a second entry under `[project.scripts]`:

```toml
[project.scripts]
codecanvas = "codecanvas.server.app:main"
codecanvas-mcp = "codecanvas.mcp.server:main"
```

- [ ] **Step 7: Verify the console script resolves**

Run: `cd core && pip install -e . && cd .. && codecanvas-mcp --help 2>/dev/null; python3 -c "import codecanvas.mcp.server as s; print(sorted(t.name for t in __import__('anyio').run(s.mcp.list_tools)))"`
Expected: prints `['analyze_impact', 'list_entrypoints', 'what_does', 'who_calls']`
(Note: `codecanvas-mcp` with no client will block on stdio — Ctrl-C is fine; the import line is the real check.)

- [ ] **Step 8: Commit**

```bash
git add core/codecanvas/mcp/server.py core/pyproject.toml tests/test_mcp_server.py
git commit -m "Add FastMCP stdio server, tool registration, and console script"
```

---

### Task 10: Full suite + manual MCP smoke check + docs

**Files:**
- Modify: `README.md` (add an MCP section)
- Test: whole suite

- [ ] **Step 1: Run the entire test suite**

Run: `python3 -m pytest tests/ -v`
Expected: PASS (existing tests + new `test_mcp_*` all green; note some tracer tests may need `httpx` per README).

- [ ] **Step 2: Manual stdio smoke test with a real MCP client**

Register the server in Claude Code and confirm the 4 tools appear and return data:

```bash
claude mcp add codecanvas -- codecanvas-mcp
```
Then in a Claude Code session, run `/mcp` and confirm `codecanvas` is connected with `list_entrypoints`, `who_calls`, `what_does`, `analyze_impact`. Call `list_entrypoints` with `project_path` set to the absolute path of `sample-fastapi/` and confirm the `/auth/login` route appears.

- [ ] **Step 3: Document the MCP server in the README**

Add this section to `README.md` after the "VS Code Commands" section:

```markdown
## MCP Server (for coding agents)

CodeCanvas exposes its analysis engine to coding agents (Claude Code, Cursor)
over MCP (stdio):

    claude mcp add codecanvas -- codecanvas-mcp

Tools:

| Tool | Answers |
|---|---|
| `list_entrypoints` | What entrypoints exist in this project? |
| `who_calls` | Who calls this function? (ground-truth reverse edges) |
| `what_does` | What does this function do? (signature, effects, risk) |
| `analyze_impact` | What endpoints break if I apply this diff? |

All tools take a `project_path`; outputs are compact and token-bounded.
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "Document MCP server usage in README"
```

---

## Self-Review

**Spec coverage:**
- Approach A (thin layer over shared core) → Tasks 3–9. ✓
- Reuse Layer 1, don't emit Layer 2 IR → session uses `FlowGraphBuilder` only for `call_graph`/entrypoints; no `build_flow`/`to_dict` anywhere. ✓
- Risk decoupling (impact.py line ~205 → `_compute_function_risk`) → Task 2. ✓
- 4 tools (`list_entrypoints`, `who_calls`, `what_does`, `analyze_impact`) → Tasks 5–8, registered in Task 9. ✓
- Token-bounded output + truncation markers → Task 4 (`capped`), applied in every query. ✓
- Function ref = qname / name / file:line + near-name suggestions → Task 5 `resolve_function`. ✓
- Error handling (ProjectTooLarge, not found, no diff, no stack) → Task 9 `_with_builder`, Task 8 no-change path. ✓
- stdio transport + `codecanvas-mcp` console script + `mcp` dep → Task 9. ✓
- Tests on `sample-fastapi`/`sample-script` fixtures → every task. ✓
- MCP protocol smoke (`list_tools`) → Task 9; manual client check → Task 10. ✓
- Extension untouched → only additive files + 2 small backward-compatible engine changes (Task 1 additive accessors, Task 2 guarded by `flow_builder is None`). ✓
- Deferred (`explain_endpoint`, `trace_path`, runtime tracing) → not in any task. ✓

**Placeholder scan:** No TBD/TODO; every code step has complete code; every command has expected output. ✓

**Type consistency:** `get_builder`/`ProjectNotFoundError` (Task 3) used in Task 9; `resolve_function` return `(func, error_dict)` consistent across Tasks 5–8; `capped(items, cap)->(list, note)` consistent across Tasks 4–8; `get_callers`/`all_functions` (Task 1) used in Tasks 3/5/6. ✓
