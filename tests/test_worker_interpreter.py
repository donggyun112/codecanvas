"""Worker interpreter resolution for the state-transition simulator.

The simulator worker must run on the *project's* interpreter so that project
dependencies are importable, not on whatever interpreter happens to be running
the MCP server (under ``uvx`` that is an isolated environment).
"""
import os
import sys
import textwrap
from pathlib import Path

import pytest

from codecanvas_mcp.mcp.interpreter import (
    MIN_WORKER_VERSION,
    find_project_venv,
    resolve_worker_interpreter,
)

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="fake shell-script interpreters are POSIX-only"
)


def _fake_python(path: Path, version=(3, 12, 4), counter: Path | None = None) -> Path:
    """Write an executable stub that answers the version probe like python does."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#!/bin/sh"]
    if counter is not None:
        lines.append(f'printf x >> "{counter}"')
    lines.append(f'echo "[{version[0]}, {version[1]}, {version[2]}]"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _venv(root: Path, name: str = ".venv", **kwargs) -> Path:
    return _fake_python(root / name / "bin" / "python", **kwargs)


# --------------------------------------------------------------------------
# Resolution chain
# --------------------------------------------------------------------------

def test_falls_back_to_server_interpreter_when_project_has_no_venv(tmp_path):
    resolved = resolve_worker_interpreter(str(tmp_path))

    assert resolved.executable == sys.executable
    assert resolved.source == "fallback"
    assert resolved.error is None


@posix_only
def test_detects_project_venv_interpreter(tmp_path):
    expected = _venv(tmp_path)

    resolved = resolve_worker_interpreter(str(tmp_path))

    assert resolved.executable == str(expected)
    assert resolved.source == "project_venv"
    assert resolved.error is None


@posix_only
def test_explicit_executable_wins_over_project_venv(tmp_path):
    _venv(tmp_path)
    explicit = _fake_python(tmp_path / "other" / "python3")

    resolved = resolve_worker_interpreter(str(tmp_path), explicit=str(explicit))

    assert resolved.executable == str(explicit)
    assert resolved.source == "explicit"


@posix_only
def test_dot_venv_is_preferred_over_venv(tmp_path):
    preferred = _venv(tmp_path, ".venv")
    _venv(tmp_path, "venv")

    assert find_project_venv(str(tmp_path)) == preferred.parent.parent


@posix_only
def test_venv_in_parent_directory_is_found(tmp_path):
    expected = _venv(tmp_path)
    nested = tmp_path / "src"
    nested.mkdir()

    resolved = resolve_worker_interpreter(str(nested))

    assert resolved.executable == str(expected)
    assert resolved.source == "project_venv"


def test_project_without_venv_resolves_to_none(tmp_path):
    assert find_project_venv(str(tmp_path)) is None


# --------------------------------------------------------------------------
# Guards on the explicit argument
# --------------------------------------------------------------------------

def test_rejects_nonexistent_explicit_path(tmp_path):
    resolved = resolve_worker_interpreter(
        str(tmp_path), explicit=str(tmp_path / "nope" / "python")
    )

    assert resolved.error is not None
    assert "not found" in resolved.error.lower()


@posix_only
def test_rejects_explicit_path_that_is_not_executable(tmp_path):
    plain = tmp_path / "python"
    plain.write_text("#!/bin/sh\n", encoding="utf-8")
    plain.chmod(0o644)

    resolved = resolve_worker_interpreter(str(tmp_path), explicit=str(plain))

    assert resolved.error is not None
    assert "not executable" in resolved.error.lower()


@posix_only
def test_rejects_explicit_path_that_is_not_a_python_interpreter(tmp_path):
    """Guards against the tool being used as a general-purpose binary runner."""
    binary = _fake_python(tmp_path / "curl")

    resolved = resolve_worker_interpreter(str(tmp_path), explicit=str(binary))

    assert resolved.error is not None
    assert "python" in resolved.error.lower()


# --------------------------------------------------------------------------
# Version guard
# --------------------------------------------------------------------------

@posix_only
def test_rejects_interpreter_below_minimum_version(tmp_path):
    _venv(tmp_path, version=(3, 9, 6))

    resolved = resolve_worker_interpreter(str(tmp_path))

    assert resolved.error is not None
    assert "3.9.6" in resolved.error
    major, minor = MIN_WORKER_VERSION
    assert f"{major}.{minor}" in resolved.error
    assert "python_executable" in resolved.error


@posix_only
def test_accepts_interpreter_at_minimum_version(tmp_path):
    major, minor = MIN_WORKER_VERSION
    _venv(tmp_path, version=(major, minor, 0))

    resolved = resolve_worker_interpreter(str(tmp_path))

    assert resolved.error is None
    assert resolved.version == (major, minor, 0)


@posix_only
def test_version_probe_runs_once_per_interpreter(tmp_path):
    counter = tmp_path / "probe-calls"
    counter.write_text("", encoding="utf-8")
    _venv(tmp_path, counter=counter)

    resolve_worker_interpreter(str(tmp_path))
    resolve_worker_interpreter(str(tmp_path))
    resolve_worker_interpreter(str(tmp_path))

    assert len(counter.read_text(encoding="utf-8")) == 1


def test_server_interpreter_is_not_probed(tmp_path):
    """The fallback is the running interpreter; its version is already known."""
    resolved = resolve_worker_interpreter(str(tmp_path))

    assert resolved.version == sys.version_info[:3]


# --------------------------------------------------------------------------
# Wiring into the simulator
# --------------------------------------------------------------------------

def _builder(tmp_path, source: str):
    from codecanvas_mcp.mcp.session import get_builder

    (tmp_path / "agent.py").write_text(
        textwrap.dedent(source).strip() + "\n", encoding="utf-8"
    )
    return get_builder(str(tmp_path))


SCHEMA = {
    "type": "object",
    "properties": {"count": {"type": "integer"}},
    "required": ["count"],
}


def test_simulate_reports_which_interpreter_ran(tmp_path):
    from codecanvas_mcp.mcp import queries

    builder = _builder(tmp_path, """
        def bump(state):
            return {"count": state["count"] + 1}
    """)

    out = queries.simulate_state_transition(
        builder, "bump", SCHEMA, cases=[{"count": 1}],
    )

    assert out["worker"]["executable"] == sys.executable
    assert out["worker"]["source"] == "fallback"
    assert out["worker"]["version"] == ".".join(str(p) for p in sys.version_info[:3])


def test_simulate_surfaces_an_unusable_explicit_interpreter(tmp_path):
    from codecanvas_mcp.mcp import queries

    builder = _builder(tmp_path, """
        def bump(state):
            return {"count": state["count"] + 1}
    """)

    out = queries.simulate_state_transition(
        builder, "bump", SCHEMA, cases=[{"count": 1}],
        python_executable=str(tmp_path / "nope" / "python"),
    )

    assert "error" in out
    assert "not found" in out["error"].lower()


def test_project_status_reports_the_worker_interpreter(tmp_path):
    from codecanvas_mcp.mcp.session import project_status

    status = project_status(str(tmp_path))

    assert status["worker"]["executable"] == sys.executable
    assert status["worker"]["source"] == "fallback"
