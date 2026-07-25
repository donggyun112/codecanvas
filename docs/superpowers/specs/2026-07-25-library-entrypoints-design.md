# Library entrypoints for `list_entrypoints`

**Date:** 2026-07-25
**Status:** Approved

## Problem

`list_entrypoints` has no concept of a library's public API. Run against
LangGraph it returns 25 entrypoints, **all `kind="script"`**, and every one is
a bench script, CI script, docs generator, or integration-test script. The
library's actual surface — `StateGraph`, `Pregel`, `create_react_agent` — is
absent.

Two causes, measured against `/Users/dongkseo/project/contribution/langgraph`:

1. **The function fallback is suppressed wholesale.**
   `entrypoint_extractor.py:161` reads
   `if api_entrypoints or script_entrypoints: return []`. Twenty-five junk
   `__main__` guards are enough to disable it entirely.
2. **Even unsuppressed it would not help.** The fallback only considers
   module-level *functions*. Classes are never entrypoints, so `StateGraph` and
   `Pregel` could not appear regardless.

Meanwhile the declarative signals are right there:

| Signal | Present in LangGraph |
|---|---|
| `[project.scripts]` | `langgraph = langgraph_cli.cli:cli` — currently unused |
| `__all__` in `__init__.py` | 23 files; `langgraph/graph/__init__.py` exports `StateGraph`, `add_messages`, `MessagesState`, `START`/`END` |

## Measured scale

After applying the extractor's existing exclusion set (critical: each package
root contains its own `.venv`, e.g. `libs/langgraph/.venv/`, which otherwise
inflates the count to 9111):

- 20 pyproject packages, of which **12 are fixtures** under `libs/cli/examples/`,
  `libs/cli/uv-examples/`, `libs/cli/python-monorepo-example/`. 8 are real.
- **129 `__all__` names** total — comfortably under the output cap.
- Of those: **79 classes**, 32 functions, 18 unresolved (constants like
  `START`/`END`, re-export chains, TypedDicts).
- Expanding the 79 classes into public methods would yield 424 more rows (474
  total).

## Design

### 1. Detection scope

Explicit declarations first, one bounded inference step:

1. `[project.scripts]` / `[project.entry-points]` → `kind="script"`
2. `__all__` in `__init__.py` → `kind="export"`
3. no `__all__` → public names in that `__init__.py` (imports, `def`, `class`)

Symbols in internal modules (`langgraph/pregel/loop.py`) are **not** entrypoints.
That is `find_symbols` territory. Entrypoints answer "where does this start",
not "what exists".

### 2. Distributed-package gate

Exports are only extracted inside a directory that declares a distribution:
a `pyproject.toml` with `[project].name`. Consequences:

- A monorepo's packages are each discovered naturally (LangGraph's `libs/*`).
- An application repo with no pyproject yields **zero** exports — existing
  behavior is untouched, so inference rule 3 cannot fire where it does not belong.

Fixture packages are excluded: any path component equal to `examples`, or ending
in `-example`/`-examples`/`_example`/`_examples`.

The scanner reuses `EntryPointExtractor`'s exclusion set (`.venv`, `venv`,
`node_modules`, `__pycache__`, `.git`, `migrations`, `.tox`, `.eggs`, `dist`,
`build`) and must apply it *during* the walk, since venvs live inside package
roots.

### 3. Classes: one row, many anchors

`impact.py:225` resolves an entrypoint via
`cg._find_function(ep.handler_name, ...)`, which matches **functions only**. A
class-named entrypoint would silently contribute nothing to `analyze_impact`.

So an exported class produces **one row** (129 rows stay readable; 474 would
bury `StateGraph` under method noise) but carries every callable anchor:

```python
EntryPoint(
    kind="export", group="Exports",
    handler_name="StateGraph",                   # display
    handler_file=".../graph/state.py", handler_line=88,
    tags=["langgraph", "export"],
    metadata={
        "package": "langgraph",
        "handler_candidates": [
            {"name": "__init__", "file": ..., "line": 91},
            {"name": "compile",  "file": ..., "line": 610},
        ],
    },
)
```

An exported *function* carries itself as the sole candidate, so both kinds
travel one code path.

`ImpactAnalyzer` unions the reachable sets of all anchors, so changing
`Pregel.stream` correctly reports the `Pregel` export as affected.

### 4. Re-export resolution

`__all__` lists the name as imported, not where it is defined. Pointing an
entrypoint at `__init__.py` would be useless for `call_tree` / `who_calls`, so
names resolve to their defining module: `from langgraph.graph.state import
StateGraph` → `graph/state.py:88`. Absolute and relative imports both, chains
followed to a depth of 5.

Names that do not resolve to a class or function (constants, TypedDicts) are
dropped rather than anchored at the wrong place.

### 5. Integration

`EntryPointExtractor.analyze()` gains the library results ahead of api/script.
Console scripts dedupe against `__main__` script entrypoints on
`(file, name, line)`. The existing function-fallback suppression rule is kept,
with exports added to its condition.

Ordering: `export` > `api` > `script` > `function`.

`ENTRYPOINT_CACHE_VERSION` 1 → 2 (payload layout changes).

## Files changed

| File | Change |
|---|---|
| `core/codecanvas_mcp/parser/library_extractor.py` | New |
| `core/codecanvas_mcp/parser/entrypoint_extractor.py` | Merge + order library results |
| `core/codecanvas_mcp/graph/impact.py` | Multi-anchor entrypoint resolution |
| `core/codecanvas_mcp/graph/models.py` | `export` kind in group/label defaults |
| `core/codecanvas_mcp/graph/builder.py` | Cache version bump |

## Testing

New `tests/test_library_entrypoints.py`:

- `__all__` (list and tuple forms) becomes `export` entrypoints
- a re-export chain resolves to the defining file and line, not `__init__.py`
- an exported class carries `__init__` and public methods as `handler_candidates`
- an exported function is its own sole candidate
- `[project.scripts]` becomes a `script` entrypoint at the target's line
- no `__all__` → public names in `__init__.py`; `_private` excluded
- **a repo with no pyproject yields zero exports** (regression guard)
- fixture/example packages are skipped
- a `.venv` inside a package root is not scanned
- unresolvable names (constants) are dropped
- exports sort ahead of scripts

Extend `tests/test_impact_analysis.py`: a change to an exported class's public
method reports that class export as affected.

## Out of scope

- Ranking exports by importance. 129 rows fit; revisit if a project overflows.
- `setup.py` / `setup.cfg` metadata. `pyproject.toml` covers current practice;
  the gate simply will not fire on legacy-only packages.
