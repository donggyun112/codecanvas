from pathlib import Path

import pytest

from codecanvas_mcp.mcp import session
from codecanvas_mcp.mcp.session import (
    AmbiguousProjectRootError, get_builder, resolve_project,
    ProjectNotFoundError, NoDefaultProjectError,
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


def test_resolve_project_explicit_updates_default(tmp_path):
    resolve_project(str(SAMPLE))
    # A second explicit path becomes the new default (last-explicit-wins).
    other = str(tmp_path)
    resolve_project(other)
    assert resolve_project(None) == str(Path(other).resolve())


def test_resolve_project_missing_dir_raises():
    with pytest.raises(ProjectNotFoundError):
        resolve_project("/no/such/dir/xyz")


def test_server_tool_uses_default_after_first_call():
    from codecanvas_mcp.mcp import server
    first = server.list_entrypoints(str(SAMPLE))
    assert "entrypoints" in first
    # Omitting project_path reuses the last project.
    second = server.who_calls("verify_user")
    assert "callers" in second, second


def test_resolve_project_blocks_broad_root_with_nested_python_root(tmp_path):
    nested = tmp_path / "product"
    nested.mkdir()
    (nested / "pyproject.toml").write_text(
        '[project]\nname = "product"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "loose.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(AmbiguousProjectRootError) as exc:
        resolve_project(str(tmp_path))

    assert exc.value.candidates == [str(nested.resolve())]
    assert session._default_project is None


def test_project_status_allows_inspecting_ambiguous_root(tmp_path):
    from codecanvas_mcp.mcp import server

    for name in ("alpha", "beta"):
        nested = tmp_path / name
        nested.mkdir()
        (nested / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )

    out = server.project_status(str(tmp_path))

    assert out["requires_root_selection"] is True
    assert out["safe_to_summarize"] is False
    assert out["analysis_root"] == str(tmp_path.resolve())
    assert len(out["candidate_roots"]) == 2

    blocked = server.list_entrypoints(str(tmp_path))
    assert "ambiguous" in blocked["error"].lower()
    assert blocked["analysis_root"] == str(tmp_path.resolve())
    assert blocked["candidate_roots"] == out["candidate_roots"]
    assert blocked["safe_to_summarize"] is False
