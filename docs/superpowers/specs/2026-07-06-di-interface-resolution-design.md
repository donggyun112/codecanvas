# DI Interface Call Resolution — Design (v1)

**Date:** 2026-07-06
**Status:** Approved (design), pending implementation plan
**Scope:** Resolve dependency-injected interface method calls in the call graph so `who_calls` / impact analysis bind them to the concrete implementation.

---

## 1. Problem

When a class receives a dependency through a constructor parameter typed as an interface
(Protocol/ABC) and calls a method on it, the call graph currently records **no edge**:

```python
class OAuthHandler:
    def __init__(self, token_service: TokenService):   # TokenService: Protocol/ABC
        self.token_service = token_service
    async def handle_exchange(self, ...):
        await self.token_service.create_token_pair(user)   # <-- not bound to anything
```

`who_calls` reports zero callers for both `TokenService.create_token_pair` (interface) and
`JWTService.create_token_pair` (concrete impl). On a real DI-heavy FastAPI backend
(IN7-Service, 601 entrypoints) this makes the reverse call graph under-report production edges.

## 2. Root cause (verified in `core/codecanvas/parser/call_graph.py`)

Two gaps in the existing resolution:

- **Gap 1 — constructor-injection type not recorded.** `_extract_assignment_types` /
  `_visit_type_assignments` collect constructor **param annotations** into the method's
  *local* `local_types` dict, but `_infer_assigned_type` only yields a type when the RHS is a
  `Capitalized(...)` constructor call. For `self.token_service = token_service` the RHS is a bare
  `ast.Name`, so `self_attr_types` is never populated and `_class_attr_types[OAuthHandler]` never
  learns `token_service → TokenService`.
- **Gap 2 — no interface→impl fallback on the `self.attr` path.** In `_resolve_attribute_call`,
  the single-level `self.attr.method()` branch (~lines 1850-1855) unconditionally returns
  `_resolve_method_on_class(attr_type, ...)`. Unlike the `local_var` path (~1882-1890), it has no
  fallback to a concrete implementation when `attr_type` is an interface.

## 3. Agreed semantics

- **Bind to the concrete implementation ONLY when the interface has exactly one concrete
  implementation.** If 0 or ≥2 impls exist, do not invent a concrete edge — bind to the interface
  method node if one exists, else leave unresolved. Precision over recall; minimize false edges.
- **Concrete-first (when unique) on the `self.attr` path.** This is an intentional divergence from
  the `local_var` path, which is interface-first. In the target case `TokenService` is a Protocol
  that *declares* `create_token_pair`, so an interface-first rule would bind to the Protocol node
  and never reach the concrete impl — contradicting the goal. The `self.attr` path therefore
  prefers the unique concrete impl, then falls back to the interface node. (Aligning `local_var`
  is explicitly out of scope.)
- **v1 wiring sources (only two):**
  1. Constructor parameter injection — `def __init__(self, x: IFace): self.x = x` — infer
     `self.x`'s type from the parameter annotation.
  2. Interface→unique-concrete-impl fallback on the single-level `self.attr.method()` path.
- **Confidence:** DI-inferred edges are tagged `_last_resolve_confidence = "inferred"` (matching the
  existing `local_var` concrete fallback), so the flow view marks them as inferred, not definite.
  (`who_calls` records the reverse edge regardless of confidence, so the binding surfaces there.)

## 4. Design — three changes, all in `call_graph.py`

**Change 1 — record `self.x = x` from the param annotation.**
In `_visit_type_assignments`, the `ast.Assign` branch: when `_infer_assigned_type(rhs)` returns
`None` and the RHS is a bare `ast.Name`, look up `local_types.get(name)` (constructor param
annotations are already collected into `local_types` before the body walk) and use it as the
inferred type. `_record_assignment_type` already routes a `self.<attr>` target into
`self_attr_types`, which flows to `_class_attr_types` via the `__init__` merge. No signature change.

**Change 2 — interface→unique-impl fallback on the `self.attr` single-level path.**
Replace the unconditional return at ~1850-1855 with: prefer the unique concrete impl
(`_unique_impl_of(attr_type)` → `_resolve_method_on_class(impl, method_name, caller)`, tagged
`"inferred"`); if there is no unique impl, fall back to `_resolve_method_on_class(attr_type, ...)`
(interface node / existing behavior).

**Change 3 — new `_unique_impl_of(type_name) -> str | None` helper** near `_resolve_concrete_type`.
- Uses **indexed data only** (`_name_index` / `_functions` / `bases`) — **no call to
  `analyze_project()`** and none of `find_implementations` / `resolve_bound_implementation` /
  `_resolve_concrete_type`'s re-entrant path.
- Guarded by `is_protocol` / `is_abstract`: returns `None` for ordinary (non-interface) base
  classes, so normal inheritance is never redirected.
- Returns the single implementation's class name only when exactly one class has the contract in
  its `bases`; returns `None` for 0 or ≥2.

**Why indexed-only is mandatory:** `_resolve_call` runs *during* `analyze_project()` (via
`_infer_param_types_from_callers` and `_enrich_logic_step_calls`, before `_analyzed=True`). Using
any helper that calls `analyze_project()` would re-enter and recurse. This is why the existing
`local_var` path uses `_resolve_concrete_type` (documented as re-entrance-safe); `_unique_impl_of`
follows the same rule but with the strict `len==1` gate that `_resolve_concrete_type` lacks
(`_resolve_concrete_type` falls back to `implementations[0]` on ≥2, which would violate our
precision rule).

**Unchanged:** cache format, `_build_caller_index`, `_get_callers`, `ast_execution.py`, the
`local_var` path, `Depends()` handling.

## 5. Testing

A dedicated minimal fixture is used so existing `sample-fastapi` assertions (entrypoint counts,
impact) are not perturbed — either a new fixture directory or a `tmp_path`-written module in the
test, decided in the plan.

- **Positive:** a Protocol/ABC interface with exactly one concrete impl, injected via a constructor
  param and called through `self.attr` → the caller binds to the concrete impl. Assert via
  `get_callers(concrete_impl_method)` including the caller (or the CALLS edge exists). Optionally
  assert the edge confidence is `inferred`.
- **Negative (required):** the same interface with **two** impls → the caller binds to **neither**
  concrete impl (may bind to the interface node or stay unresolved, but must not invent a concrete
  edge).
- **Guard:** an ordinary (non-Protocol/non-ABC) base class with a single subclass, injected via
  `self.attr` → `_unique_impl_of` does not redirect (normal resolution only).
- **Regression:** full `tests/test_call_graph_resolution.py` and `tests/test_mcp_queries.py` pass
  (sample-fastapi service/repository bindings and MCP `who_calls` unchanged).

## 6. Out of scope

- Class-level / `@dataclass` field annotations as attr-type sources.
- DI containers / registries (`container.get(IFace)`).
- FastAPI `Depends()` resolution (handled elsewhere; not touched).
- Making the `local_var` path concrete-first (kept interface-first).
- Multi-level `self.a.b.method()` interface→impl redirection (only single-level `self.attr` in v1).
