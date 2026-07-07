"""Resolve a project path to an analyzed, cached FlowGraphBuilder.

The MCP layer reuses FlowGraphBuilder purely as a composition + cache root
(call graph, entrypoint discovery, disk caches). It never calls build_flow().
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from codecanvas_mcp.graph.builder import FlowGraphBuilder

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
