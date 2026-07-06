"""Resolve a project path to an analyzed, cached FlowGraphBuilder.

The MCP layer reuses FlowGraphBuilder purely as a composition + cache root
(call graph, entrypoint discovery, disk caches). It never calls build_flow().
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from codecanvas.graph.builder import FlowGraphBuilder

_MAX_BUILDERS = 8
_builders: "OrderedDict[str, FlowGraphBuilder]" = OrderedDict()


class ProjectNotFoundError(Exception):
    """Raised when the requested project path is not a directory."""


def get_builder(project_path: str) -> FlowGraphBuilder:
    """Return an analyzed, LRU-cached FlowGraphBuilder for ``project_path``."""
    if not Path(project_path).is_dir():
        raise ProjectNotFoundError(f"Directory not found: {project_path}")

    key = str(Path(project_path).resolve())

    if key in _builders:
        _builders.move_to_end(key)
        return _builders[key]

    builder = FlowGraphBuilder(project_path)
    builder.call_graph.analyze_project()  # idempotent; warm via disk cache
    _builders[key] = builder
    while len(_builders) > _MAX_BUILDERS:
        _builders.popitem(last=False)
    return builder
