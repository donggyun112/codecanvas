"""End-to-end: the simulator worker must see the project's dependencies.

Builds a real virtualenv containing a module that exists nowhere else, then
simulates a function that imports it. This is the failure the change fixes:
under `uvx`, the server's interpreter cannot import project dependencies.
"""
import subprocess
import sys

import pytest

from codecanvas_mcp.mcp import queries
from codecanvas_mcp.mcp.session import get_builder

SCHEMA = {
    "type": "object",
    "properties": {"count": {"type": "integer"}},
    "required": ["count"],
}

MODULE_NAME = "only_in_project_venv"


@pytest.fixture(scope="module")
def project_with_private_dependency(tmp_path_factory):
    """A project whose venv holds a module the test runner cannot import."""
    project = tmp_path_factory.mktemp("project")
    subprocess.run(
        [sys.executable, "-m", "venv", str(project / ".venv")],
        check=True, capture_output=True,
    )
    site_packages = next((project / ".venv" / "lib").glob("python*/site-packages"))
    (site_packages / f"{MODULE_NAME}.py").write_text("VALUE = 42\n", encoding="utf-8")

    (project / "agent.py").write_text(
        f"import {MODULE_NAME}\n"
        "\n"
        "def bump(state):\n"
        f'    return {{"count": state["count"] + {MODULE_NAME}.VALUE}}\n',
        encoding="utf-8",
    )
    return project


def test_the_dependency_is_invisible_to_the_test_runner():
    """Guards the premise: without the venv the import genuinely fails."""
    with pytest.raises(ImportError):
        __import__(MODULE_NAME)


def test_worker_imports_a_dependency_only_the_project_venv_has(
    project_with_private_dependency,
):
    project = project_with_private_dependency

    out = queries.simulate_state_transition(
        get_builder(str(project)), "bump", SCHEMA,
        cases=[{"count": 1}],
        invariants=["no_exception", "return_has_required_keys"],
    )

    assert out["worker"]["source"] == "project_venv"
    assert out["worker"]["executable"] == str(project / ".venv" / "bin" / "python")
    assert out["failed"] == 0
    assert out["results"][0]["return_value"] == {"count": 43}


def test_server_interpreter_cannot_import_it(project_with_private_dependency):
    """The old behavior, still reachable by forcing the server's interpreter."""
    out = queries.simulate_state_transition(
        get_builder(str(project_with_private_dependency)), "bump", SCHEMA,
        cases=[{"count": 1}],
        invariants=["no_exception"],
        python_executable=sys.executable,
    )

    assert out["worker"]["source"] == "explicit"
    assert out["failed"] == 1
    detail = str(out["results"][0]["violations"])
    assert MODULE_NAME in detail
