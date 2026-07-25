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
    error: str | None    # set when the interpreter is unusable

def find_project_venv(project_root: str) -> Path | None
def resolve_worker_interpreter(project_root, explicit=None) -> WorkerInterpreter
```

`find_project_venv` is the shared primitive; `resolve_worker_interpreter` adds
validation, the version probe, and source labelling.

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

A probe that times out or fails to parse falls back to `sys.executable` with the
reason recorded in `error`.

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

`_activate_project_venv` is rewritten to call `find_project_venv`. Its search
order, first-match-wins behavior, and `sys.path` mutation are preserved exactly;
existing tracer tests act as the regression check.

## Files changed

| File | Change |
|---|---|
| `core/codecanvas_mcp/mcp/interpreter.py` | New |
| `core/codecanvas_mcp/mcp/simulator.py` | `simulate()` gains `python_executable`; subprocess uses resolved interpreter; output gains `worker` |
| `core/codecanvas_mcp/mcp/queries.py` | Thread `python_executable` through |
| `core/codecanvas_mcp/mcp/server.py` | Tool argument + docstring |
| `core/codecanvas_mcp/mcp/session.py` | `project_status` worker block |
| `core/codecanvas_mcp/tracer/app_discovery.py` | Reuse `find_project_venv` |

## Testing

New `tests/test_worker_interpreter.py`:

- explicit `python_executable` wins over an existing project venv
- a `tmp_path` venv layout (`.venv/bin/python`) is auto-detected
- no venv present → falls back to `sys.executable`, `source == "fallback"`
- rejects a nonexistent path, a non-executable file, and a non-`python*` basename
- version probe parsing: `3.9` rejected with the actionable message, `3.10`+ accepted
- probe result is cached per path
- `simulate()` output includes the `worker` block

Existing `tests/test_mcp_simulator.py` and the tracer tests must pass unchanged.

## Out of scope

- `env` / extra `sys.path` injection arguments — the chain plus `python_executable`
  covers the reported need; revisit if a concrete case appears.
- Sandboxing the worker. It executes trusted project code, as documented today.
