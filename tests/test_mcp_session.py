from pathlib import Path

import pytest

from codecanvas.mcp import session
from codecanvas.mcp.session import (
    get_builder, resolve_project, ProjectNotFoundError, NoDefaultProjectError,
)

SAMPLE = Path(__file__).parent.parent / "sample-fastapi"


@pytest.fixture(autouse=True)
def _clear_default():
    session._default_project = None
    yield
    session._default_project = None


def test_get_builder_returns_analyzed_builder():
    builder = get_builder(str(SAMPLE))
    # Analyzed: functions are populated.
    assert builder.call_graph.all_functions(), "call graph should be analyzed"


def test_get_builder_is_cached():
    b1 = get_builder(str(SAMPLE))
    b2 = get_builder(str(SAMPLE))
    assert b1 is b2, "same project path returns the cached builder"


def test_get_builder_missing_dir_raises():
    with pytest.raises(ProjectNotFoundError):
        get_builder("/no/such/dir/xyz")


def test_get_builder_normalizes_path():
    b1 = get_builder(str(SAMPLE))
    b2 = get_builder(str(SAMPLE) + "/")   # trailing slash, same dir
    assert b1 is b2


def test_resolve_project_explicit_sets_default():
    resolved = resolve_project(str(SAMPLE))
    assert resolved == str(SAMPLE.resolve())
    # After an explicit call, the default is remembered.
    assert resolve_project(None) == str(SAMPLE.resolve())


def test_resolve_project_no_default_raises():
    with pytest.raises(NoDefaultProjectError):
        resolve_project(None)


def test_resolve_project_explicit_updates_default():
    resolve_project(str(SAMPLE))
    # A second explicit path becomes the new default (last-explicit-wins).
    other = str(SAMPLE.parent)
    resolve_project(other)
    assert resolve_project(None) == str(Path(other).resolve())


def test_resolve_project_missing_dir_raises():
    with pytest.raises(ProjectNotFoundError):
        resolve_project("/no/such/dir/xyz")


def test_server_tool_uses_default_after_first_call():
    from codecanvas.mcp import server
    first = server.list_entrypoints(str(SAMPLE))
    assert "entrypoints" in first
    # Omitting project_path reuses the last project.
    second = server.who_calls("verify_user")
    assert "callers" in second, second
