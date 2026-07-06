# DI Interface Call Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve dependency-injected interface method calls (`self.attr.method()` where `attr` is a constructor-injected interface) to the unique concrete implementation, so `who_calls`/impact bind them.

**Architecture:** Three changes in `core/codecanvas/parser/call_graph.py`: (1) record `self.x = x` attr types from the constructor param annotation; (2) a new indexed-only `_unique_impl_of` helper that returns an interface's single concrete impl (strict, re-entrance-safe); (3) apply concrete-first resolution on the `self.attr` paths via a shared helper. No cache/format/other-file changes.

**Tech Stack:** Python 3.10+, `ast`, pytest.

## Global Constraints

- Python `>=3.10`. Change ONLY `core/codecanvas/parser/call_graph.py` (plus a new test file).
- Bind to a concrete impl ONLY when the interface has exactly ONE implementation; 0 or ≥2 → do not invent a concrete edge (bind to interface node / leave unresolved).
- Concrete-first on the `self.attr` resolution paths (intentional divergence from the interface-first `local_var` path, which must stay unchanged).
- Interface detection is nominal: `is_protocol` (base name ends with `Protocol`) or `is_abstract` (ABC base / `@abstractmethod`). Ordinary base classes must NOT be redirected.
- Resolution helpers used from `_resolve_call` MUST use indexed data only — no `analyze_project()` / `find_implementations` / `resolve_bound_implementation` (they re-enter analysis and recurse).
- DI-inferred edges set `self._last_resolve_confidence = "inferred"`.
- Do not touch: cache format, `_build_caller_index`, `_get_callers`, `ast_execution.py`, `Depends()` handling, the `local_var` path, `_resolve_concrete_type`.
- No AI attribution in commit messages.
- Test command (lean-ctx shell gate: no `bash`, no `python3 -c/-e/-p`, no brace-groups, no pip): `cd /Users/dongkseo99/project/codecanvas && PYTHONPATH=core .venv/bin/python3 -m pytest <path> -v`

---

### Task 1: `_unique_impl_of` helper

Pure, indexed-only helper that returns the single concrete implementation class name for an interface type, else `None`. Independently testable.

**Files:**
- Modify: `core/codecanvas/parser/call_graph.py` (add method immediately BEFORE `_resolve_concrete_type`, ~line 2080)
- Test: `tests/test_di_resolution.py` (new)

**Interfaces:**
- Consumes: existing `_normalize_type_name`, `_name_index`, `_functions`, `FunctionDef.{definition_type,is_protocol,is_abstract,name,bases}`.
- Produces: `CallGraphBuilder._unique_impl_of(self, type_name: str) -> str | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_di_resolution.py`:

```python
from codecanvas.parser.call_graph import CallGraphBuilder


def _build(tmp_path, src, name="di_app.py"):
    (tmp_path / name).write_text(src)
    cg = CallGraphBuilder(str(tmp_path))
    cg.analyze_project()
    return cg


UNIQUE_FIX = '''
from typing import Protocol

class OneIface(Protocol):
    def f(self): ...

class OnlyImpl(OneIface):
    def f(self):
        return 1

class TwoIface(Protocol):
    def g(self): ...

class ImplA(TwoIface):
    def g(self):
        return 1

class ImplB(TwoIface):
    def g(self):
        return 2

class Plain:
    def h(self):
        return 1
'''


def test_unique_impl_of_helper(tmp_path):
    cg = _build(tmp_path, UNIQUE_FIX)
    assert cg._unique_impl_of("OneIface") == "OnlyImpl"   # exactly one impl
    assert cg._unique_impl_of("TwoIface") is None          # ambiguous (2 impls)
    assert cg._unique_impl_of("Plain") is None             # concrete type
    assert cg._unique_impl_of("Nonexistent") is None       # unknown
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/dongkseo99/project/codecanvas && PYTHONPATH=core .venv/bin/python3 -m pytest tests/test_di_resolution.py::test_unique_impl_of_helper -v`
Expected: FAIL — `AttributeError: 'CallGraphBuilder' object has no attribute '_unique_impl_of'`

- [ ] **Step 3: Implement the helper**

In `core/codecanvas/parser/call_graph.py`, immediately before `def _resolve_concrete_type(` (~line 2080), add:

```python
    def _unique_impl_of(self, type_name: str) -> str | None:
        """Return the single concrete implementation's class name for an
        interface (Protocol/ABC), or None if the type is concrete or has
        zero/multiple implementations.

        Indexed-data only (no analyze_project() re-entrance) so it is safe to
        call from _resolve_call during the enrichment phase. Stricter than
        _resolve_concrete_type: never guesses among multiple implementations.
        """
        simple_name = self._normalize_type_name(type_name)
        if not simple_name:
            return None
        candidates = [
            self._functions[qname]
            for qname in self._name_index.get(simple_name, [])
            if self._functions[qname].definition_type in {"class", "schema"}
        ]
        if not candidates:
            return None
        type_def = candidates[0]
        if not type_def.is_protocol and not type_def.is_abstract:
            return None  # concrete type — nothing to redirect
        implementations = [
            func for func in self._functions.values()
            if func.definition_type == "class"
            and self._normalize_type_name(func.name) != simple_name
            and any(self._normalize_type_name(base) == simple_name for base in func.bases)
        ]
        if len(implementations) == 1:
            return implementations[0].name
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/dongkseo99/project/codecanvas && PYTHONPATH=core .venv/bin/python3 -m pytest tests/test_di_resolution.py::test_unique_impl_of_helper -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add core/codecanvas/parser/call_graph.py tests/test_di_resolution.py
git commit -m "Add _unique_impl_of: strict indexed-only interface->impl lookup"
```

---

### Task 2: Record `self.x = x` attr type from the constructor param annotation

Bridge the constructor-injection gap so `self.attr`'s type is known.

**Files:**
- Modify: `core/codecanvas/parser/call_graph.py` (`_visit_type_assignments`, the `ast.Assign` branch, ~lines 3498-3502)
- Test: `tests/test_di_resolution.py` (append)

**Interfaces:**
- Consumes: `local_types` (already pre-populated with param annotations at `_extract_assignment_types` ~lines 3469-3479), `_record_assignment_type`.
- Produces: after analysis, `cg._class_attr_types[<class_qname>][<attr>]` contains the param-annotation type for `self.<attr> = <param>` assignments in `__init__`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_di_resolution.py`:

```python
SINGLE = '''
from typing import Protocol

class TokenService(Protocol):
    async def create_token_pair(self, user): ...

class JWTService(TokenService):
    async def create_token_pair(self, user):
        return {"a": 1}

class OAuthHandler:
    def __init__(self, token_service: TokenService):
        self.token_service = token_service
    async def handle_exchange(self, user):
        return await self.token_service.create_token_pair(user)
'''


def test_constructor_param_injection_recorded_as_attr_type(tmp_path):
    cg = _build(tmp_path, SINGLE)
    handler_cls = next(
        f for f in cg.all_functions()
        if f.definition_type == "class" and f.qualified_name.endswith("OAuthHandler")
    )
    attrs = cg._class_attr_types.get(handler_cls.qualified_name, {})
    assert attrs.get("token_service") == "TokenService", attrs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/dongkseo99/project/codecanvas && PYTHONPATH=core .venv/bin/python3 -m pytest tests/test_di_resolution.py::test_constructor_param_injection_recorded_as_attr_type -v`
Expected: FAIL — `AssertionError` (attrs is empty / has no `token_service`), because a bare-`Name` RHS is not captured.

- [ ] **Step 3: Implement the bridge**

In `core/codecanvas/parser/call_graph.py`, in `_visit_type_assignments`, replace the `ast.Assign` branch (currently ~lines 3498-3502):

```python
        if isinstance(node, ast.Assign):
            inferred_type = self._infer_assigned_type(node.value)
            if inferred_type:
                for target in node.targets:
                    self._record_assignment_type(target, inferred_type, local_types, self_attr_types)
```

with:

```python
        if isinstance(node, ast.Assign):
            inferred_type = self._infer_assigned_type(node.value)
            if inferred_type is None and isinstance(node.value, ast.Name):
                # self.x = x / y = x — carry the type from an already-known
                # local or constructor-param annotation (e.g. DI: an injected
                # dependency assigned to self).
                inferred_type = local_types.get(node.value.id)
            if inferred_type:
                for target in node.targets:
                    self._record_assignment_type(target, inferred_type, local_types, self_attr_types)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/dongkseo99/project/codecanvas && PYTHONPATH=core .venv/bin/python3 -m pytest tests/test_di_resolution.py::test_constructor_param_injection_recorded_as_attr_type -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Run existing resolution tests (no regression)**

Run: `cd /Users/dongkseo99/project/codecanvas && PYTHONPATH=core .venv/bin/python3 -m pytest tests/test_call_graph_resolution.py -v`
Expected: PASS (all existing resolution tests green)

- [ ] **Step 6: Commit**

```bash
git add core/codecanvas/parser/call_graph.py tests/test_di_resolution.py
git commit -m "Record self.attr type from constructor param annotation"
```

---

### Task 3: Concrete-first resolution on the `self.attr` paths

Wire `_unique_impl_of` into `_resolve_attribute_call` via a shared helper, at BOTH the multi-level chain result and the single-level fallback (the single-attr case `self.x.method()` is routed through the chain path because `owner_parts` has length 2).

**Files:**
- Modify: `core/codecanvas/parser/call_graph.py` (`_resolve_attribute_call` ~lines 1843-1855; add a small helper next to it)
- Test: `tests/test_di_resolution.py` (append)

**Interfaces:**
- Consumes: `_unique_impl_of` (Task 1), `_class_attr_types` populated by Task 2, `_resolve_method_on_class`, `_last_resolve_confidence`.
- Produces: `CallGraphBuilder._resolve_method_prefer_unique_impl(self, type_name: str, method_name: str, caller: FunctionDef) -> FunctionDef | None`; and `self.attr.method()` calls now bind to the unique concrete impl when one exists.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_di_resolution.py`:

```python
def _callers_of(cg, suffix):
    target = next(
        (f for f in cg.all_functions() if f.qualified_name.endswith(suffix)), None
    )
    assert target is not None, f"{suffix} not found in graph"
    return {caller.qualified_name for caller, _ref in cg.get_callers(target.qualified_name)}


def test_di_single_impl_binds_to_concrete(tmp_path):
    cg = _build(tmp_path, SINGLE)
    callers = _callers_of(cg, "JWTService.create_token_pair")
    assert any(q.endswith("OAuthHandler.handle_exchange") for q in callers), callers


TWO_IMPLS = SINGLE + '''
class OpaqueTokenService(TokenService):
    async def create_token_pair(self, user):
        return {}
'''


def test_di_two_impls_does_not_bind_concrete(tmp_path):
    cg = _build(tmp_path, TWO_IMPLS)
    jwt = _callers_of(cg, "JWTService.create_token_pair")
    opaque = _callers_of(cg, "OpaqueTokenService.create_token_pair")
    assert not any(q.endswith("OAuthHandler.handle_exchange") for q in jwt), jwt
    assert not any(q.endswith("OAuthHandler.handle_exchange") for q in opaque), opaque


GUARD = '''
class Base:
    def m(self):
        return 0

class Sub(Base):
    def m(self):
        return 1

class User:
    def __init__(self, b: Base):
        self.b = b
    def go(self):
        return self.b.m()
'''


def test_concrete_base_not_redirected_to_subclass(tmp_path):
    cg = _build(tmp_path, GUARD)
    base_callers = _callers_of(cg, "Base.m")
    sub_callers = _callers_of(cg, "Sub.m")
    assert any(q.endswith("User.go") for q in base_callers), base_callers
    assert not any(q.endswith("User.go") for q in sub_callers), sub_callers
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/dongkseo99/project/codecanvas && PYTHONPATH=core .venv/bin/python3 -m pytest tests/test_di_resolution.py -k "single_impl or two_impls or concrete_base" -v`
Expected: `test_di_single_impl_binds_to_concrete` FAILS (binds to the interface node, not `JWTService`). The other two may already pass; all three must pass after Step 3.

- [ ] **Step 3: Add the shared helper and wire it in**

In `core/codecanvas/parser/call_graph.py`, add this method immediately before `_resolve_attribute_call` (~line 1819):

```python
    def _resolve_method_prefer_unique_impl(
        self,
        type_name: str,
        method_name: str,
        caller: "FunctionDef",
    ) -> "FunctionDef | None":
        """Resolve method_name on type_name, preferring the unique concrete
        implementation of an interface (concrete-first) over the declared
        interface type. Falls back to the declared type when there is no
        single implementation. DI redirects are tagged 'inferred'.
        """
        impl = self._unique_impl_of(type_name)
        if impl:
            result = self._resolve_method_on_class(impl, method_name, caller)
            if result:
                self._last_resolve_confidence = "inferred"
                return result
        return self._resolve_method_on_class(type_name, method_name, caller)
```

Then, in `_resolve_attribute_call`, the multi-level chain branch currently reads (~lines 1843-1848):

```python
            # Follow multi-level chain: self.repo.session.method()
            resolved_type = self._follow_attr_chain(
                caller.class_qname, owner_parts[1:],
            )
            if resolved_type:
                return self._resolve_method_on_class(resolved_type, method_name, caller)
```

Replace the return with the prefer-impl helper:

```python
            # Follow multi-level chain: self.repo.session.method()
            resolved_type = self._follow_attr_chain(
                caller.class_qname, owner_parts[1:],
            )
            if resolved_type:
                return self._resolve_method_prefer_unique_impl(
                    resolved_type, method_name, caller,
                )
```

And the single-level fallback branch currently reads (~lines 1850-1855):

```python
            # Fallback: single-level self.attr (original behavior)
            if caller.class_qname:
                attr_name = owner_parts[1]
                attr_type = self._class_attr_types.get(caller.class_qname, {}).get(attr_name)
                if attr_type:
                    return self._resolve_method_on_class(attr_type, method_name, caller)
```

Replace its inner return with the prefer-impl helper:

```python
            # Fallback: single-level self.attr
            if caller.class_qname:
                attr_name = owner_parts[1]
                attr_type = self._class_attr_types.get(caller.class_qname, {}).get(attr_name)
                if attr_type:
                    return self._resolve_method_prefer_unique_impl(
                        attr_type, method_name, caller,
                    )
```

- [ ] **Step 4: Run the DI tests to verify they pass**

Run: `cd /Users/dongkseo99/project/codecanvas && PYTHONPATH=core .venv/bin/python3 -m pytest tests/test_di_resolution.py -v`
Expected: PASS (all 5 tests: helper, attr-recording, single-impl→concrete, two-impls→not-bound, guard)

- [ ] **Step 5: Run the full suite (no regression)**

Run: `cd /Users/dongkseo99/project/codecanvas && PYTHONPATH=core .venv/bin/python3 -m pytest tests/ -q`
Expected: all pass except the (unrelated) known state — confirm `tests/test_call_graph_resolution.py`, `tests/test_dispatch_and_cfg.py`, and `tests/test_mcp_queries.py` are green and no new failures are introduced.

- [ ] **Step 6: Commit**

```bash
git add core/codecanvas/parser/call_graph.py tests/test_di_resolution.py
git commit -m "Resolve DI'd interface calls to the unique concrete impl"
```

---

### Task 4: Verify on the real DI-heavy project

Confirm the fix binds the real edge that motivated the work (no code change; verification + note).

**Files:** none (verification only).

- [ ] **Step 1: Confirm the previously-missing production edge now resolves**

Using the codecanvas engine against `/Users/dongkseo99/project/IN7/apps/IN7-Service/Backend`, check that `who_calls` for the concrete token-service implementation's `create_token_pair` now includes `OAuthHandler.handle_exchange` (previously zero callers). Run via the MCP tool `mcp__codecanvas__who_calls` with `project_path=/Users/dongkseo99/project/IN7/apps/IN7-Service/Backend` and `function="create_token_pair"`, OR a short script:

```
cd /Users/dongkseo99/project/codecanvas && PYTHONPATH=core .venv/bin/python3 - <<PYEOF  # NOTE: heredoc/-c are blocked by the shell gate; write this to a scratch .py file and run `.venv/bin/python3 scratch.py` instead
PYEOF
```

Because the shell gate blocks inline code, write a scratch file `<scratchpad>/verify_di.py`:

```python
import sys
sys.path.insert(0, "/Users/dongkseo99/project/codecanvas/core")
from codecanvas.parser.call_graph import CallGraphBuilder
cg = CallGraphBuilder("/Users/dongkseo99/project/IN7/apps/IN7-Service/Backend")
cg.analyze_project()
for f in cg.all_functions():
    if f.qualified_name.endswith("create_token_pair") and f.definition_type != "class":
        callers = [c.qualified_name for c, _ in cg.get_callers(f.qualified_name)]
        print(f.qualified_name, "<-", callers)
```

Run: `cd /Users/dongkseo99/project/codecanvas && PYTHONPATH=core .venv/bin/python3 <scratchpad>/verify_di.py`
Expected: at least one concrete `create_token_pair` now lists a handler caller (e.g. `handle_exchange`). If the real interface has ≥2 impls, per the agreed semantics it will remain unbound at the concrete level — record the actual observed result either way.

- [ ] **Step 2: Record the outcome**

Note in the final summary whether the real edge resolved (single impl) or stayed unbound (multiple impls) — both are correct behavior. No commit.

---

## Self-Review

**Spec coverage:**
- Gap 1 (constructor param → attr type) → Task 2. ✓
- Gap 2 (interface→impl fallback on `self.attr`) → Task 3 (chain + single-level via shared helper). ✓ (Refines the spec's "single-level path" to also cover the multi-level chain entry point, which is where a single `self.attr.method()` actually routes — verified via `_follow_attr_chain`.)
- Unique-impl-only semantics → Task 1 `_unique_impl_of` strict `len==1`; negative test in Task 3. ✓
- Concrete-first divergence from local_var → Task 3 helper (prefer impl, fallback to declared type); local_var path untouched. ✓
- Indexed-only / no re-entrance → Task 1 helper uses `_functions`/`_name_index` only; Global Constraints forbid `analyze_project`-calling helpers. ✓
- Confidence "inferred" → Task 3 helper sets `_last_resolve_confidence`. ✓
- Nominal interface detection / ordinary base not redirected → Task 1 `is_protocol/is_abstract` guard; guard test in Task 3. ✓
- Dedicated fixture (no sample-fastapi perturbation) → `tmp_path`-written modules in `tests/test_di_resolution.py`. ✓
- Regression (call_graph_resolution, mcp_queries) → Task 2 Step 5, Task 3 Step 5. ✓
- Real-project verification → Task 4. ✓
- Out-of-scope items (dataclass fields, DI containers, Depends, local_var, cache) → not in any task. ✓

**Placeholder scan:** No TBD/TODO; every code step has complete code; commands have expected output. (Task 4's heredoc is explicitly called out as blocked and replaced with a scratch-file instruction.) ✓

**Type consistency:** `_unique_impl_of(type_name)->str|None` (Task 1) consumed by `_resolve_method_prefer_unique_impl` (Task 3); `_class_attr_types[qname][attr]` populated in Task 2 and read in Task 3; `get_callers`/`all_functions` (existing public accessors) used consistently in tests. ✓
