# Simulator worker interpreter selection

**Date:** 2026-07-25
**Status:** Approved

## Problem

`simulate_state_transition` executes project code in a subprocess, but always
uses the MCP server's own interpreter:

```python
# core/codecanvas_mcp/mcp/simulator.py:922
subprocess.run([sys.executable, worker_path, "--worker"], ...)
```

When the server runs under `uvx codecanvas-mcp` — the documented install path —
`sys.executable` points into uvx's isolated environment. The project's
dependencies are not importable there, so any target function that imports a
third-party package fails at module import with a traceback that does not
explain why.

Today the only workaround is to install `codecanvas-mcp` into every project venv
and point the MCP config at `<project>/.venv/bin/codecanvas-mcp`. That is poor
UX and easy to get wrong.

## Two findings that shape the fix

1. **`simulator.py` imports only stdlib** (lines 1–33; zero `codecanvas_mcp`
   references). It is already a self-contained worker script invoked by absolute
   path, so pointing a *different* interpreter at it requires no packaging
   change — the target venv does not need `codecanvas-mcp` installed.
2. **venv discovery already exists** in
   `core/codecanvas_mcp/tracer/app_discovery.py:124` (`_activate_project_venv`).
   It searches the project root and its parent for `.venv`/`venv` and handles the
   Windows layout, but it mutates `sys.path` in-process and is tracer-only.

`cwd=project_root` and `env=os.environ.copy()` are already passed to the worker.
Only the interpreter is wrong.

## Design

### 1. New module `core/codecanvas_mcp/mcp/interpreter.py`

`simulator.py` is 985 lines and a flagged complexity hotspot, so resolution
logic lives in its own module. Public surface:

```python
@dataclass
class WorkerInterpreter:
    executable: str
    source: str          # "explicit" | "project_venv" | "fallback"
    version: tuple[int, int, int] | None
    error: str | None    # unusable — the caller must abort
    note: str | None     # degraded but usable

def iter_venv_candidates(project_root: str) -> Iterator[Path]
def find_project_venv(project_root: str) -> Path | None
def resolve_worker_interpreter(project_root, explicit=None) -> WorkerInterpreter
```

`iter_venv_candidates` is the shared primitive: it yields candidate venv
directories in search order and lets each caller decide what makes one usable.
The simulator requires an interpreter inside; the tracer requires a
site-packages directory. Sharing `find_project_venv` directly would have
imposed the simulator's stricter predicate on the tracer.

`error` and `note` are distinct on purpose. A failed version probe on an
*auto-detected* venv is not the caller's fault, so it degrades to the server
interpreter and records a `note`; only an explicitly requested interpreter that
cannot be used produces an `error` that aborts the call.

### 2. Resolution chain

| Priority | Source | Notes |
|---|---|---|
| 1 | `python_executable` argument | Explicit escape hatch |
| 2 | `<root>/.venv` → `<root>/venv`, then the same two in the parent dir | Same search order as `_activate_project_venv` |
| 3 | `sys.executable` | Current behavior — unchanged when no venv is found |

The interpreter path inside a venv is `bin/python` on POSIX and
`Scripts/python.exe` on Windows.

**`VIRTUAL_ENV` is deliberately excluded.** Under `uvx`, the variable can leak in
from the shell that launched the MCP client and would select an interpreter
unrelated to the analyzed project. An unpredictable chain is worse than an
explicit argument.

### 3. Version guard

The worker floor is `requires-python = ">=3.10"` (core/pyproject.toml). When the
resolved interpreter is not `sys.executable`, probe it once:

```
[exe, "-c", "import sys,json;print(json.dumps(list(sys.version_info[:3])))"]
```

Results are cached per `(path, mtime)` so repeated simulate calls pay for it
once. Below 3.10, return an actionable error instead of letting the worker die
on a `SyntaxError`:

> Project interpreter is Python 3.9.6; the simulator worker requires >= 3.10.
> Pass `python_executable=` or upgrade the project venv.

A probe that times out or fails to parse falls back to `sys.executable`, with
the reason recorded in `note` (see the `error`/`note` split above).

### 4. Security guard

`python_executable` arrives from an MCP client. The tool already executes
arbitrary project code, so this is not a privilege escalation, but the argument
must not turn the tool into a general-purpose binary runner. Require the path to
be:

- an existing file,
- executable by the current user,
- named `python*` (basename check).

Invocation stays in list form (no shell), as today.

### 5. Self-diagnosing output

The core failure was that the user could not tell *which* interpreter ran. Every
`simulate_state_transition` result carries:

```json
"worker": {
  "executable": "/path/to/.venv/bin/python",
  "source": "project_venv",
  "version": "3.12.4"
}
```

### 6. `project_status` extension

`project_status` (core/codecanvas_mcp/mcp/session.py:74) gains a `worker` block
built from the same function, and states explicitly when the resolved
interpreter differs from the server's `sys.executable` — so the mismatch is
visible before a simulation is ever run.

### 7. `tracer/app_discovery.py` cleanup

`_activate_project_venv` is rewritten to iterate `iter_venv_candidates`. Its
search order, first-match-wins behavior, and `sys.path` mutation are preserved
exactly.

It had **no test coverage**, so characterization tests are written against the
current implementation *before* the refactor, and must stay green through it.
(It cannot call `find_project_venv`: that requires an interpreter inside the
venv, while the tracer only needs site-packages — a venv directory with
site-packages but no usable `bin/python` would silently stop being activated.)

## Files changed

| File | Change |
|---|---|
| `core/codecanvas_mcp/mcp/interpreter.py` | New |
| `core/codecanvas_mcp/mcp/simulator.py` | `simulate()` gains `python_executable`; subprocess uses resolved interpreter; output gains `worker` |
| `core/codecanvas_mcp/mcp/queries.py` | Thread `python_executable` through |
| `core/codecanvas_mcp/mcp/server.py` | Tool argument + docstring |
| `core/codecanvas_mcp/mcp/session.py` | `project_status` worker block |
| `core/codecanvas_mcp/tracer/app_discovery.py` | Reuse `iter_venv_candidates` |

## Testing

`tests/test_worker_interpreter.py` (16) — resolution and guards, using
executable shell stubs that answer the version probe, so no mocking is needed:

- explicit `python_executable` wins over an existing project venv
- a `.venv/bin/python` layout is auto-detected; `.venv` beats `venv`; parent dir searched
- no venv present → falls back to `sys.executable`, `source == "fallback"`
- rejects a nonexistent path, a non-executable file, and a non-`python*` basename
- `3.9` rejected with the actionable message; the minimum version accepted
- the probe runs once per interpreter; the server interpreter is never probed
- `simulate()` output carries `worker`; an unusable explicit path surfaces as `error`
- `project_status` reports the worker interpreter

`tests/test_tracer_venv_activation.py` (6) — characterization of
`_activate_project_venv`, written before the refactor.

`tests/test_simulator_project_venv.py` (3) — end-to-end. Builds a real venv
holding a module that exists nowhere else and simulates a function importing it:

- the premise holds (the test runner genuinely cannot import the module)
- the worker resolves to the project venv and the case passes
- forcing `python_executable=sys.executable` reproduces the old failure

Existing `tests/test_mcp_simulator.py` and the tracer tests pass unchanged.

## Out of scope

- `env` / extra `sys.path` injection arguments — the chain plus `python_executable`
  covers the reported need; revisit if a concrete case appears.
- Sandboxing the worker. It executes trusted project code, as documented today.
