"""Resolve a project path to an analyzed, cached FlowGraphBuilder.

The MCP layer reuses FlowGraphBuilder purely as a composition + cache root
(call graph, entrypoint discovery, disk caches). It never calls build_flow().
"""
from __future__ import annotations

from collections import OrderedDict
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


def resolve_project(project_path: str | None) -> str:
    """Resolve the effective project path for a tool call.

    An explicit ``project_path`` wins and becomes the remembered default
    (last-explicit-wins). If omitted, the last-used project is reused; with
    no project used yet, ``NoDefaultProjectError`` is raised.
    """
    global _default_project
    if project_path:
        if not Path(project_path).is_dir():
            raise ProjectNotFoundError(f"Directory not found: {project_path}")
        _default_project = str(Path(project_path).resolve())
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
    markers = ("pyproject.toml", "setup.py", "setup.cfg")
    candidates = set()
    for marker in markers:
        for found in root.glob(marker):
            candidates.add(str(found.parent))
        for found in root.glob(f"*/{marker}"):
            candidates.add(str(found.parent))
        for found in root.glob(f"*/*/{marker}"):
            candidates.add(str(found.parent))
    cache_path = root / CACHE_DIR_NAME / CACHE_FILE_NAME
    recommendations = sorted(candidates, key=lambda p: (p.count("/"), p))

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
        "recommended_root": (
            str(root) if str(root) in candidates
            else recommendations[0] if recommendations
            else str(root)
        ),
        "note": (
            "Multiple Python project roots detected; consider a narrower project_path."
            if len(recommendations) > 1 else None
        ),
    }
