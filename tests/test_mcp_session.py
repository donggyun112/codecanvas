from pathlib import Path

import pytest

from codecanvas.mcp.session import get_builder, ProjectNotFoundError

SAMPLE = Path(__file__).parent.parent / "sample-fastapi"


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
