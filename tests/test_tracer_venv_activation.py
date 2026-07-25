"""Characterization tests for the tracer's venv activation.

``_activate_project_venv`` had no coverage. These tests pin its current
behavior so the venv search order can be shared with the simulator's
interpreter resolution without drifting.
"""
import sys

import pytest

from codecanvas_mcp.tracer.app_discovery import _activate_project_venv


@pytest.fixture
def restore_sys_path():
    original = list(sys.path)
    yield
    sys.path[:] = original


def _site_packages(root, venv_name=".venv", version="python3.12"):
    path = root / venv_name / "lib" / version / "site-packages"
    path.mkdir(parents=True)
    return path


def test_adds_project_site_packages_to_sys_path(tmp_path, restore_sys_path):
    expected = _site_packages(tmp_path)

    _activate_project_venv(str(tmp_path))

    assert sys.path[0] == str(expected)


def test_prefers_dot_venv_over_venv(tmp_path, restore_sys_path):
    expected = _site_packages(tmp_path, ".venv")
    _site_packages(tmp_path, "venv")

    _activate_project_venv(str(tmp_path))

    assert sys.path[0] == str(expected)


def test_falls_back_to_parent_directory(tmp_path, restore_sys_path):
    expected = _site_packages(tmp_path)
    nested = tmp_path / "src"
    nested.mkdir()

    _activate_project_venv(str(nested))

    assert sys.path[0] == str(expected)


def test_does_nothing_when_no_venv_exists(tmp_path, restore_sys_path):
    before = list(sys.path)

    _activate_project_venv(str(tmp_path))

    assert sys.path == before


def test_venv_directory_without_site_packages_is_skipped(tmp_path, restore_sys_path):
    """An empty .venv must not shadow a usable venv later in the search order."""
    (tmp_path / ".venv").mkdir()
    expected = _site_packages(tmp_path, "venv")

    _activate_project_venv(str(tmp_path))

    assert sys.path[0] == str(expected)


def test_does_not_duplicate_an_already_present_path(tmp_path, restore_sys_path):
    expected = _site_packages(tmp_path)
    sys.path.insert(0, str(expected))

    _activate_project_venv(str(tmp_path))

    assert sys.path.count(str(expected)) == 1
