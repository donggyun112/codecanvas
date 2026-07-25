"""Resolve a project path to an analyzed, cached FlowGraphBuilder.

The MCP layer reuses FlowGraphBuilder purely as a composition + cache root
(call graph, entrypoint discovery, disk caches). It never calls build_flow().
"""
from __future__ import annotations

from collections import OrderedDict
import os
from pathlib import Path

from codecanvas_mcp.graph.builder import FlowGraphBuilder
from codecanvas_mcp.parser.call_graph import (
    CACHE_DIR_NAME, CACHE_FILE_NAME, _iter_project_python_files,
)

_MAX_BUILDERS = 8
_builders: "OrderedDict[str, FlowGraphBuilder]" = OrderedDict()

# The most recently used project, so tools can omit project_path on
# follow-up calls (pass it once, reuse it thereafter).
_default_project: str | None = None


class ProjectNotFoundError(Exception):
    """Raised when the requested project path is not a directory."""


class NoDefaultProjectError(Exception):
    """Raised when project_path is omitted and no project has been used yet."""


class AmbiguousProjectRootError(Exception):
    """Raised when a broad path contains one or more narrower Python roots."""

    def __init__(self, requested: str, candidates: list[str]):
        self.requested = requested
        self.candidates = candidates
        choices = ", ".join(candidates)
        super().__init__(
            f"Analysis root is ambiguous: {requested}. "
            f"Choose an explicit Python project root: {choices}"
        )


_ROOT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg")
_ROOT_SCAN_EXCLUDES = {
    ".git", ".venv", "venv", "__pycache__", "node_modules", "migrations",
    ".tox", ".eggs", "dist", "build", "references", "reference", "vendor",
    "vendored", "third_party",
}


def _candidate_python_roots(root: Path) -> list[str]:
    candidates: set[str] = set()
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = [d for d in subdirs if d not in _ROOT_SCAN_EXCLUDES]
        if any(marker in files for marker in _ROOT_MARKERS):
            candidates.add(str(Path(directory).resolve()))
    return sorted(candidates, key=lambda path: (path.count(os.sep), path))


def resolve_project(
    project_path: str | None,
    *,
    allow_ambiguous: bool = False,
) -> str:
    """Resolve the effective project path for a tool call.

    An explicit ``project_path`` wins and becomes the remembered default
    (last-explicit-wins). If omitted, the last-used project is reused; with
    no project used yet, ``NoDefaultProjectError`` is raised.
    """
    global _default_project
    if project_path:
        if not Path(project_path).is_dir():
            raise ProjectNotFoundError(f"Directory not found: {project_path}")
        resolved = str(Path(project_path).resolve())
        candidates = _candidate_python_roots(Path(resolved))
        narrower = [candidate for candidate in candidates if candidate != resolved]
        explicit_root_is_candidate = resolved in candidates
        if (
            not allow_ambiguous
            and not explicit_root_is_candidate
            and (len(candidates) > 1 or narrower)
        ):
            raise AmbiguousProjectRootError(resolved, candidates)
        _default_project = resolved
        return _default_project
    if _default_project is not None:
        return _default_project
    raise NoDefaultProjectError(
        "No project_path given and no project used yet. Pass project_path "
        "once — it is remembered for subsequent calls in this session."
    )


def get_builder(project_path: str) -> FlowGraphBuilder:
    """Return an analyzed, LRU-cached FlowGraphBuilder for ``project_path``."""
    global _default_project
    if not Path(project_path).is_dir():
        raise ProjectNotFoundError(f"Directory not found: {project_path}")

    key = str(Path(project_path).resolve())
    _default_project = key

    if key in _builders:
        _builders.move_to_end(key)
        return _builders[key]

    builder = FlowGraphBuilder(project_path)
    builder.call_graph.analyze_project()  # idempotent; warm via disk cache
    _builders[key] = builder
    while len(_builders) > _MAX_BUILDERS:
        _builders.popitem(last=False)
    return builder


def project_status(project_path: str) -> dict:
    """Inspect analysis scope and suggest narrower Python project roots."""
    root = Path(project_path).resolve()
    py_files = _iter_project_python_files(root)
    candidates = set(_candidate_python_roots(root))
    cache_path = root / CACHE_DIR_NAME / CACHE_FILE_NAME
    recommendations = sorted(candidates, key=lambda p: (p.count("/"), p))
    explicit_root_is_candidate = str(root) in candidates
    requires_root_selection = (
        not explicit_root_is_candidate
        and (
            len(recommendations) > 1
            or any(candidate != str(root) for candidate in recommendations)
        )
    )

    from codecanvas_mcp.mcp.interpreter import resolve_worker_interpreter

    interpreter = resolve_worker_interpreter(str(root))
    worker = interpreter.as_dict()
    if interpreter.error:
        worker["error"] = interpreter.error
    elif interpreter.source == "fallback":
        worker["warning"] = (
            "The simulator worker will run on this server's interpreter, which "
            "may not be able to import the project's dependencies. Create a "
            "project venv or pass python_executable to simulate_state_transition."
        )

    return {
        "project_root": str(root),
        "python_files": len(py_files),
        "worker": worker,
        "cache": {
            "path": str(cache_path),
            "exists": cache_path.is_file(),
        },
        "candidate_roots": recommendations,
        "requires_root_selection": requires_root_selection,
        "recommended_root": (
            str(root) if str(root) in candidates
            else recommendations[0] if recommendations
            else str(root)
        ),
        "note": (
            "Analysis is blocked for other tools until an explicit candidate root is selected."
            if requires_root_selection
            else None
        ),
    }
