"""Pick the Python interpreter that runs the simulator worker.

The worker executes project code, so it must run on an interpreter that can
import the project's dependencies. The MCP server's own interpreter usually
cannot: under ``uvx codecanvas-mcp`` it lives in an isolated environment that
knows nothing about the analyzed project.

``simulator.py`` imports only the standard library and is invoked by absolute
path, so any sufficiently new interpreter can run it — the target venv does not
need ``codecanvas-mcp`` installed.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys

# Floor for the worker script itself (core/pyproject.toml requires-python).
MIN_WORKER_VERSION = (3, 10)

_PROBE_TIMEOUT_SECONDS = 5.0
_PROBE_CODE = "import sys,json;print(json.dumps(list(sys.version_info[:3])))"

# Keyed by (path, mtime) so a rebuilt venv is re-probed but repeated calls are not.
_probe_cache: dict[tuple[str, float], tuple[int, int, int] | None] = {}


@dataclass
class WorkerInterpreter:
    """The interpreter chosen for the simulator worker, and how it was chosen."""

    executable: str
    source: str                                  # explicit | project_venv | fallback
    version: tuple[int, int, int] | None = None
    error: str | None = None                     # unusable — caller must abort
    note: str | None = None                      # degraded but usable

    def as_dict(self) -> dict:
        out = {
            "executable": self.executable,
            "source": self.source,
            "version": ".".join(str(part) for part in self.version) if self.version else None,
        }
        if self.note:
            out["note"] = self.note
        return out


def iter_venv_candidates(project_root: str) -> Iterator[Path]:
    """Yield candidate virtualenv directories, best first.

    The project root then its parent, preferring ``.venv`` over ``venv``.
    Callers decide what makes a candidate usable: the simulator needs an
    interpreter, the tracer needs a site-packages directory.
    """
    root = Path(project_root)
    for base in (root, root.parent):
        for name in (".venv", "venv"):
            yield base / name


def find_project_venv(project_root: str) -> Path | None:
    """Return the project's virtualenv directory, or None."""
    for candidate in iter_venv_candidates(project_root):
        if _venv_python(candidate) is not None:
            return candidate
    return None


def resolve_worker_interpreter(
    project_root: str,
    explicit: str | None = None,
) -> WorkerInterpreter:
    """Resolve the worker interpreter: explicit > project venv > this process."""
    if explicit:
        return _from_explicit(explicit)

    venv_dir = find_project_venv(project_root)
    if venv_dir is not None:
        python = _venv_python(venv_dir)
        version = _probe_version(str(python))
        if version is None:
            return _server_interpreter(
                note=f"Could not determine the version of {python}; "
                     f"fell back to the server interpreter."
            )
        if version < MIN_WORKER_VERSION:
            return WorkerInterpreter(
                executable=str(python), source="project_venv", version=version,
                error=_version_error(python, version),
            )
        return WorkerInterpreter(
            executable=str(python), source="project_venv", version=version,
        )

    return _server_interpreter()


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------

def _venv_python(venv_dir: Path) -> Path | None:
    """Return the interpreter inside ``venv_dir`` if it is usable."""
    for relative in (("bin", "python"), ("Scripts", "python.exe")):
        candidate = venv_dir.joinpath(*relative)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _server_interpreter(note: str | None = None) -> WorkerInterpreter:
    return WorkerInterpreter(
        executable=sys.executable,
        source="fallback",
        version=tuple(sys.version_info[:3]),
        note=note,
    )


def _from_explicit(explicit: str) -> WorkerInterpreter:
    path = Path(explicit)
    failure = _explicit_rejection(path)
    if failure is not None:
        return WorkerInterpreter(executable=explicit, source="explicit", error=failure)

    version = _probe_version(explicit)
    if version is None:
        return WorkerInterpreter(
            executable=explicit, source="explicit",
            error=f"Could not determine the Python version of {explicit}. "
                  f"Is it a working interpreter?",
        )
    if version < MIN_WORKER_VERSION:
        return WorkerInterpreter(
            executable=explicit, source="explicit", version=version,
            error=_version_error(path, version),
        )
    return WorkerInterpreter(executable=explicit, source="explicit", version=version)


def _explicit_rejection(path: Path) -> str | None:
    """Reject anything that is not plausibly a Python interpreter.

    This tool already runs project code, so the check is not a privilege
    boundary — it keeps `python_executable` from being used as a general
    purpose binary runner.
    """
    if not path.is_file():
        return f"python_executable not found: {path}"
    if not os.access(path, os.X_OK):
        return f"python_executable is not executable: {path}"
    if not path.name.lower().startswith("python"):
        return (f"python_executable must name a Python interpreter "
                f"(basename starting with 'python'), got: {path.name}")
    return None


def _version_error(path: Path | str, version: tuple[int, int, int]) -> str:
    found = ".".join(str(part) for part in version)
    required = ".".join(str(part) for part in MIN_WORKER_VERSION)
    return (f"Interpreter {path} is Python {found}; the simulator worker "
            f"requires >= {required}. Pass python_executable= to select another "
            f"interpreter, or upgrade the project venv.")


def _probe_version(executable: str) -> tuple[int, int, int] | None:
    """Ask an interpreter for its version, once per (path, mtime)."""
    try:
        key = (executable, os.path.getmtime(executable))
    except OSError:
        return None
    if key in _probe_cache:
        return _probe_cache[key]

    version: tuple[int, int, int] | None = None
    try:
        completed = subprocess.run(
            [executable, "-c", _PROBE_CODE],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SECONDS,
        )
        if completed.returncode == 0:
            parts = json.loads(completed.stdout.strip())
            if isinstance(parts, list) and len(parts) == 3:
                version = tuple(int(part) for part in parts)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        version = None

    _probe_cache[key] = version
    return version
