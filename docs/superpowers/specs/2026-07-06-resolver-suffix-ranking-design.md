# Resolver Suffix Matching + Candidate Ranking Design

**Date:** 2026-07-06
**Status:** Approved — ready for implementation plan
**Topic:** Make `resolve_function` (the MCP-agent-facing function reference resolver) resolve `Class.method` suffixes, and rank ambiguous candidates so a dominant one auto-selects instead of forcing the agent to disambiguate on every ambiguous name.

## Problem

`core/codecanvas/mcp/queries.py::resolve_function(builder, ref)` maps a function reference to a `FunctionDef`. It is consumed by three MCP tools — `who_calls` (queries.py:108), `what_does` (:157), `function_flow` (:230) — all via `func, err = resolve_function(...); if err is not None: return err`, so the returned error dict is the tool's response to the agent.

Today it resolves in four steps:
1. Exact qualified name (`module.Class.method`).
2. `file:line` — linear scan, **first match wins** (no ambiguity check; shared basenames silently pick the wrong file — a known latent bug).
3. Bare name via `f.name == ref`: unique → return; multiple → **refuse** with `{error, suggestions}` (a flat, unordered list of up to 10 qualified names).
4. Miss → `difflib` near-name `suggestions`.

Two friction sources:

- **(a) No `Class.method` / suffix matching.** Step 3 is `f.name == ref` — exact simple name only. A partially-qualified reference like `UploadSingleFileUseCase.execute` (unique in the project) is not the full qualified name, is not `file:line`, and does not equal the bare `execute`, so it misses. Agents work around this by manually filtering `all_functions()` with `endswith` — repeatedly.
- **(b) Ambiguous names are refused, not ranked.** The resolver correctly does not pick silently, but it dumps an unordered name list and pushes the choice back to the agent every time — even when one candidate is obviously the intended one (a production implementation vs. a test double, a concrete class vs. the interface it implements, or a widely-called function vs. an obscure namesake). This round-trip is the recurring cost.

## Goal

- Resolve a dot-boundary suffix (`Class.method`, `module.Class.method`) when it uniquely identifies a function.
- When a name/suffix is ambiguous, rank the candidates by reliable signals and **auto-select the dominant one**; when none dominates, return a ranked, annotated candidate list so the agent chooses from an ordered, informative set rather than a flat dump.
- Harden `file:line` to detect multiple matches instead of silently taking the first.

## Non-goals

- Fuzzy/semantic matching beyond dot-boundary suffix and difflib near-name (already present) for true misses.
- Directory-based ranking (router/handler/usecase heuristics). Deferred as YAGNI — it is the fuzziest signal and the highest misfire risk. The reliable signals below cover the real duplicate cases; directory rank can be added later if evidence shows it is needed.
- Changing the three consumers. The `(func, None) | (None, dict)` contract is preserved.

## Design

All changes are inside `core/codecanvas/mcp/queries.py`. Small private helpers may be added to the same module.

### Resolution flow (revised)

1. **Exact qualified name** — unchanged (`cg.get_function(ref)`).
2. **`file:line`** — collect *all* functions whose file matches and whose line range contains the line (not just the first). Exactly 1 → return it. More than 1 → feed into ranking (step 4). 0 → fall through.
3. **Candidate gathering (unified bare + suffix).** Replace the `f.name == ref` step with a dot-boundary suffix match:

   ```python
   cands = [f for f in funcs
            if f.qualified_name == ref or f.qualified_name.endswith("." + ref)]
   ```

   This subsumes the old bare-name case (`ref="execute"` matches any `qn` ending in `.execute`, and top-level `qn == "execute"`) and adds partial qualification (`ref="Foo.execute"` matches only `qn` ending in `.Foo.execute`). The leading `.` enforces a segment boundary, so `execute` does not match `reexecute`.

4. **Resolve the candidate set:**
   - 0 candidates → miss: `difflib` near-name `suggestions` (unchanged behavior/shape).
   - 1 candidate → return it.
   - >1 candidates → rank and apply the dominance rule.

### Ranking signals

Per candidate, compute (higher is better):

- `non_test` (bool): the file is **not** a test file — path does not contain a `tests/`/`test/` segment and the basename is not `test_*.py` or `*_test.py`.
- `concrete` (bool): `not f.is_protocol and not f.is_abstract` (both fields exist on `FunctionDef`, call_graph.py:151-152).
- `fan_in` (int): `len(cg.get_callers(f.qualified_name))` — number of distinct callers (get_callers returns one representative per caller).

Sort candidates by the key `(non_test, concrete, fan_in)` descending. This is the order used for the returned candidate list.

### Dominance rule (auto-select)

Let `top` and `second` be the two highest-ranked candidates.

- Define the **categorical key** = `(non_test, concrete)` — the two reliable, discrete signals.
- If `top.categorical > second.categorical` (lexicographic) → **auto-select `top`**.
- Else (categorical tie) → auto-select `top` **only** if its fan-in dominates by a clear margin: `top.fan_in >= 2 * second.fan_in and top.fan_in - second.fan_in >= 2`. Otherwise do not auto-select.
- Auto-selection returns `(top, None)` — consumers are unchanged, and the tool output echoes `top.qualified_name`, so the agent sees which function was chosen and can correct if it was wrong.

The fan-in margin gate keeps auto-selection from firing on a flimsy 1-caller difference between otherwise-identical candidates, honoring "suppress occasional mis-selection" while still letting a clearly-popular function win (the chosen A behavior).

### Ambiguous return (no dominant candidate)

Return `(None, error_dict)` where:

```python
{
    "error": f"Ambiguous '{ref}' ({len(cands)} matches); pick one by qualified_name.",
    "candidates": [
        {
            "qualified_name": f.qualified_name,
            "location": _location(f),               # "file:line"
            "kind": "method" if f.class_name else "function",
            "is_interface": f.is_protocol or f.is_abstract,
            "callers": len(cg.get_callers(f.qualified_name)),
        }
        for f in ranked[:10]                          # best-first
    ],
}
```

This replaces the flat `suggestions` list **for the ambiguous case**. The `suggestions` key is retained only for the **miss case** (step 4, 0 candidates), where difflib near-name guesses are not real candidates and carry no metadata — a semantically distinct situation ("did you mean this name?" vs. "these all match, choose one").

### Error/edge behavior

- All new code is read-only over the analysis (no mutation of the graph).
- `get_callers` on each candidate is O(candidates × callers); candidate sets are small (typically 2–5), so cost is negligible.
- Empty project / no functions → miss path, unchanged.

## Testing

New tests (in the repo's `tests/` suite; a dedicated `test_resolver.py` if no resolver test file exists yet):

1. **Suffix unique** — `Class.method` that is unique resolves directly.
2. **Suffix boundary** — `execute` does not match `reexecute` (no false suffix match).
3. **Ambiguous → categorical dominance** — a production `concrete` function and a same-named test double; assert the production one is auto-selected (`err is None`, correct qualified_name).
4. **Ambiguous → interface vs concrete** — a `is_protocol` def and its concrete impl share a name; concrete auto-selected.
5. **Weak fan-in tie → list** — two categorically-identical candidates with fan-in 1 vs 2 (below margin); assert `err` returned with a ranked `candidates` list (no auto-select).
6. **Strong fan-in margin → auto-select** — categorically identical, fan-in e.g. 6 vs 1; assert auto-selected.
7. **`file:line` multiple matches** — two functions in same-basename files where the line hits a function in each; assert it no longer silently returns the first (either resolves via ranking or returns candidates).
8. **Miss** — unknown name still returns `suggestions` (difflib), unchanged.
9. **Consumer regression** — `who_calls` / `what_does` / `function_flow` still work for an unambiguous reference and propagate the ambiguous error dict.

## Acceptance criteria

- `Class.method` suffixes that are unique resolve without the agent passing a full qualified name.
- An ambiguous name with a clearly dominant candidate (non-test/concrete, or strong fan-in margin) resolves in one call; the chosen qualified name appears in the tool output.
- An ambiguous name with no dominant candidate returns a best-first `candidates` list with `qualified_name`, `location`, `kind`, `is_interface`, and `callers` for each.
- `file:line` no longer silently returns the first of several matches.
- The three consumer tools are unchanged and all existing resolver behavior for unique names and true misses is preserved.
