# Resolver Suffix Matching + Candidate Ranking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `resolve_function` resolve `Class.method` suffixes and auto-select a dominant candidate when a name is ambiguous, falling back to a ranked candidate list — eliminating the agent's per-ambiguity disambiguation round-trip.

**Architecture:** All logic lives in `core/codecanvas/mcp/queries.py`. A unified dot-boundary suffix match replaces the bare-name step; ambiguous candidate sets go through a shared `_rank_and_select` helper that ranks by `(non_test, concrete, fan_in)` and auto-selects when the top candidate dominates. The `file:line` path is hardened to detect multiple matches. The `(func, None) | (None, dict)` contract and the three consumer tools are unchanged.

**Tech Stack:** Python 3.10+, pytest. No new dependencies.

## Global Constraints

- Design reference: `docs/superpowers/specs/2026-07-06-resolver-suffix-ranking-design.md`.
- All production changes are confined to `core/codecanvas/mcp/queries.py`.
- Preserve the contract: `resolve_function` returns `(FunctionDef, None)` on success or `(None, dict)` on failure; the three consumers (`who_calls`, `what_does`, `function_flow`) are not modified.
- Suffix match rule (exact): `f.qualified_name == ref or f.qualified_name.endswith("." + ref)` (leading dot enforces a segment boundary).
- Ranking key, higher-is-better: `(non_test, concrete, fan_in)` where `fan_in = len(cg.get_callers(qn))`.
- Dominance (auto-select): top wins on categorical key `(non_test, concrete)`; on a categorical tie, auto-select only if `top_fan_in >= 2 * second_fan_in and top_fan_in - second_fan_in >= 2`.
- Error dict keys: `candidates` (ranked, best-first, with metadata) for the **ambiguous** case; `suggestions` (difflib near-names) retained for the **miss** case only.
- `FunctionDef` fields used: `qualified_name`, `name`, `file_path`, `line_start`, `line_end`, `class_name`, `is_protocol`, `is_abstract` (all exist).
- Tests run from repo root with the venv interpreter (this environment's lean-ctx shell gate forbids `python3 -c`, `-e`, and bash pipes; plain `python3` lacks deps): `PYTHONPATH=core .venv/bin/python3 -m pytest <path> -v`.
- Commit messages: no AI/tool attribution or co-author footer.

---

### Task 1: Suffix matching + candidate ranking core

**Files:**
- Modify: `core/codecanvas/mcp/queries.py` — add three helpers after `_location` (~line 15); replace the bare-name block in `resolve_function` (~lines 44-53).
- Test: `tests/test_resolver_ranking.py` (new file).

**Interfaces:**
- Consumes: `builder.call_graph` (a `CallGraphBuilder`) with `.all_functions()`, `.get_function(qn)`, `.get_callers(qn) -> list`; `FunctionDef` fields listed in Global Constraints; existing module-level `_location(func) -> "file:line"`.
- Produces:
  - `_is_test_path(fp: str) -> bool`
  - `_rank_key(cg, f) -> tuple` → `(non_test: bool, concrete: bool, fan_in: int)`
  - `_rank_and_select(cg, ref: str, cands: list) -> tuple` → `(FunctionDef, None)` or `(None, {"error", "candidates"})`
  - `resolve_function` now resolves dot-boundary suffixes and routes ambiguous name/suffix sets through `_rank_and_select`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_resolver_ranking.py`:

```python
from codecanvas.graph.builder import FlowGraphBuilder
from codecanvas.mcp import queries


def _resolved(tmp_path, files):
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    b = FlowGraphBuilder(str(tmp_path))
    b.call_graph.analyze_project()
    return b


def test_suffix_unique_resolves(tmp_path):
    b = _resolved(tmp_path, {
        "app/uploads.py":
            "class UploadSingleFileUseCase:\n"
            "    def execute(self):\n"
            "        return 1\n"
            "class DownloadUseCase:\n"
            "    def execute(self):\n"
            "        return 2\n",
    })
    func, err = queries.resolve_function(b, "UploadSingleFileUseCase.execute")
    assert err is None
    assert func is not None and func.class_name == "UploadSingleFileUseCase"


def test_bare_ambiguous_returns_candidates(tmp_path):
    b = _resolved(tmp_path, {
        "app/uploads.py":
            "class UploadSingleFileUseCase:\n"
            "    def execute(self):\n"
            "        return 1\n"
            "class DownloadUseCase:\n"
            "    def execute(self):\n"
            "        return 2\n",
    })
    func, err = queries.resolve_function(b, "execute")
    assert func is None
    assert "candidates" in err and len(err["candidates"]) == 2
    assert {c["kind"] for c in err["candidates"]} == {"method"}


def test_suffix_boundary_no_false_match(tmp_path):
    b = _resolved(tmp_path, {
        "app/x.py":
            "def execute():\n"
            "    return 1\n"
            "def reexecute():\n"
            "    return 2\n",
    })
    func, err = queries.resolve_function(b, "execute")
    assert err is None
    assert func is not None and func.name == "execute"


def test_non_test_beats_test_double(tmp_path):
    b = _resolved(tmp_path, {
        "app/svc.py":
            "def process():\n"
            "    return 1\n",
        "tests/fakes.py":
            "def process():\n"
            "    return 2\n",
    })
    func, err = queries.resolve_function(b, "process")
    assert err is None
    assert func is not None and func.file_path.endswith("app/svc.py")


def test_concrete_beats_protocol(tmp_path):
    b = _resolved(tmp_path, {
        "app/auth.py":
            "from typing import Protocol\n"
            "class TokenService(Protocol):\n"
            "    def create_token_pair(self): ...\n"
            "class JWTService(TokenService):\n"
            "    def create_token_pair(self):\n"
            "        return 1\n",
    })
    func, err = queries.resolve_function(b, "create_token_pair")
    assert err is None
    assert func is not None
    assert func.qualified_name.endswith("JWTService.create_token_pair")


def test_weak_fan_in_returns_list(tmp_path):
    b = _resolved(tmp_path, {
        "app/a.py":
            "def proc():\n"
            "    return 1\n"
            "def c1():\n"
            "    return proc()\n",
        "app/b.py":
            "def proc():\n"
            "    return 2\n"
            "def d1():\n"
            "    return proc()\n"
            "def d2():\n"
            "    return proc()\n",
    })
    func, err = queries.resolve_function(b, "proc")
    assert func is None
    assert "candidates" in err and len(err["candidates"]) == 2


def test_strong_fan_in_auto_selects(tmp_path):
    b = _resolved(tmp_path, {
        "app/a.py":
            "def helper():\n"
            "    return 1\n"
            "def c1():\n"
            "    return helper()\n"
            "def c2():\n"
            "    return helper()\n",
        "app/b.py":
            "def helper():\n"
            "    return 2\n",
    })
    func, err = queries.resolve_function(b, "helper")
    assert err is None
    assert func is not None and func.file_path.endswith("app/a.py")


def test_miss_returns_suggestions(tmp_path):
    b = _resolved(tmp_path, {
        "app/x.py":
            "def compute():\n"
            "    return 1\n",
    })
    func, err = queries.resolve_function(b, "kompute")
    assert func is None
    assert "suggestions" in err and isinstance(err["suggestions"], list)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=core .venv/bin/python3 -m pytest tests/test_resolver_ranking.py -v`
Expected: FAILs — e.g. `test_suffix_unique_resolves` fails because `Class.method` currently misses (returns an error), and `test_bare_ambiguous_returns_candidates` fails because today's ambiguous error uses `suggestions`, not `candidates`.

- [ ] **Step 3: Add the helper functions**

In `core/codecanvas/mcp/queries.py`, immediately after the `_location` function (ends ~line 15), insert:

```python
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
```

- [ ] **Step 4: Rewire the bare-name step to suffix + ranking**

In `resolve_function`, replace this block (the `# 3. Bare name` section):

```python
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
```

with:

```python
    # 3. Bare name or dot-boundary suffix (Class.method / module.Class.method).
    cands = [f for f in funcs
             if f.qualified_name == ref or f.qualified_name.endswith("." + ref)]
    if len(cands) == 1:
        return cands[0], None
    if len(cands) > 1:
        return _rank_and_select(cg, ref, cands)
```

Leave the `# 4. Miss -> near-name suggestions.` block unchanged.

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=core .venv/bin/python3 -m pytest tests/test_resolver_ranking.py -v`
Expected: PASS (8 tests)

- [ ] **Step 6: Commit**

```bash
git add core/codecanvas/mcp/queries.py tests/test_resolver_ranking.py
git commit -m "Resolve Class.method suffixes and rank ambiguous candidates"
```

---

### Task 2: Harden file:line against multiple matches

**Files:**
- Modify: `core/codecanvas/mcp/queries.py` — the `file:line` block in `resolve_function` (~lines 32-42).
- Test: `tests/test_resolver_ranking.py` (append).

**Interfaces:**
- Consumes: `_rank_and_select` (Task 1).
- Produces: `file:line` returns the single match, or routes multiple matches through `_rank_and_select` instead of silently returning the first.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_resolver_ranking.py`:

```python
def test_file_line_single_resolves(tmp_path):
    b = _resolved(tmp_path, {
        "app/only.py":
            "def alpha():\n"
            "    return 1\n",
    })
    func, err = queries.resolve_function(b, "app/only.py:1")
    assert err is None
    assert func is not None and func.name == "alpha"


def test_file_line_multiple_returns_candidates(tmp_path):
    b = _resolved(tmp_path, {
        "pkg_a/svc.py":
            "def run():\n"
            "    return 1\n",
        "pkg_b/svc.py":
            "def go():\n"
            "    return 2\n",
    })
    func, err = queries.resolve_function(b, "svc.py:1")
    assert func is None
    assert "candidates" in err and len(err["candidates"]) == 2
```

- [ ] **Step 2: Run tests to verify the multi-match one fails**

Run: `PYTHONPATH=core .venv/bin/python3 -m pytest "tests/test_resolver_ranking.py::test_file_line_multiple_returns_candidates" "tests/test_resolver_ranking.py::test_file_line_single_resolves" -v`
Expected: `test_file_line_multiple_returns_candidates` FAILS — today the loop returns the first match (`err is None`), so `"candidates" in err` raises/fails. `test_file_line_single_resolves` should already PASS.

- [ ] **Step 3: Rewrite the file:line block**

In `resolve_function`, replace this block (the `# 2. file:line form.` section):

```python
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
```

with:

```python
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
```

(Zero matches falls through to the name/suffix step, as before.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=core .venv/bin/python3 -m pytest tests/test_resolver_ranking.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add core/codecanvas/mcp/queries.py tests/test_resolver_ranking.py
git commit -m "Detect multiple file:line matches instead of taking the first"
```

---

### Task 3: Consumer integration + regression gate

**Files:**
- Test: `tests/test_resolver_ranking.py` (append one integration test).
- No production changes.

**Interfaces:**
- Consumes: `queries.who_calls` (unchanged) + the new resolver behavior.
- Produces: confirmation that the ambiguous error dict (with `candidates`) propagates through a consumer, and that the existing suite is green.

- [ ] **Step 1: Write the integration test**

Append to `tests/test_resolver_ranking.py`:

```python
def test_who_calls_ambiguous_propagates_candidates(tmp_path):
    b = _resolved(tmp_path, {
        "app/a.py":
            "def proc():\n"
            "    return 1\n"
            "def c1():\n"
            "    return proc()\n",
        "app/b.py":
            "def proc():\n"
            "    return 2\n"
            "def d1():\n"
            "    return proc()\n"
            "def d2():\n"
            "    return proc()\n",
    })
    out = queries.who_calls(b, "proc")
    assert "error" in out and "candidates" in out
```

- [ ] **Step 2: Run the new test**

Run: `PYTHONPATH=core .venv/bin/python3 -m pytest "tests/test_resolver_ranking.py::test_who_calls_ambiguous_propagates_candidates" -v`
Expected: PASS — `who_calls` returns the error dict verbatim, so it carries `candidates`.

- [ ] **Step 3: Run the existing resolver/consumer suite**

Run: `PYTHONPATH=core .venv/bin/python3 -m pytest tests/test_mcp_queries.py tests/test_mcp_function_flow.py -v`
Expected: PASS — existing `test_resolve_by_bare_name`, `test_resolve_unknown_returns_suggestions`, `who_calls`/`what_does`/`function_flow` tests still green (bare-name unique resolution and miss suggestions are preserved).

- [ ] **Step 4: Run the full suite**

Run: `PYTHONPATH=core .venv/bin/python3 -m pytest tests/ -q`
Expected: PASS — no regressions. (A pre-existing RuntimeWarning in `test_tracer_collector.py` is unrelated; note any pre-existing failure but do not fix out of scope.)

- [ ] **Step 5: Commit**

```bash
git add tests/test_resolver_ranking.py
git commit -m "Add resolver consumer-integration regression test"
```

---

## Self-Review

**Spec coverage:**
- Dot-boundary suffix matching → Task 1 (Step 4) + `test_suffix_unique_resolves`, `test_suffix_boundary_no_false_match`.
- Ranking by `(non_test, concrete, fan_in)` → Task 1 `_rank_key` + `test_non_test_beats_test_double`, `test_concrete_beats_protocol`.
- Dominance auto-select (categorical + fan-in margin) → Task 1 `_rank_and_select` + `test_strong_fan_in_auto_selects`, `test_weak_fan_in_returns_list`.
- Ambiguous ranked `candidates` list with metadata → Task 1 (`test_bare_ambiguous_returns_candidates` checks shape) + Task 3 (propagation).
- `suggestions` retained for miss only → Task 1 `test_miss_returns_suggestions`.
- `file:line` multi-match hardening → Task 2.
- Consumer contract unchanged → Task 3 + existing `tests/test_mcp_queries.py`.
- Acceptance criteria (one-call resolution when dominant; echoed qname; list otherwise; file:line no silent first) → Tasks 1-3.

**Placeholder scan:** No TBD/TODO; every code step shows full code; no vague "handle errors".

**Type consistency:** `_rank_key` returns `(bool, bool, int)`; `_rank_and_select` reads `top_key[:2]` (categorical) and `[2]` (fan_in) consistently; candidate dict keys (`qualified_name`, `location`, `kind`, `is_interface`, `callers`) are identical between the design and Task 1; error key is `candidates` (ambiguous) / `suggestions` (miss) throughout; `_rank_and_select` is defined in Task 1 and consumed in Task 2.
