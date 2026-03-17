"""HTTP server for CodeCanvas - communicates with VS Code extension.

Provides endpoints:
- POST /analyze: Analyze a project directory
- POST /flow: Build flow graph for a specific entrypoint
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from codecanvas.graph.builder import FlowGraphBuilder

app = FastAPI(title="CodeCanvas Analysis Server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global state: active project builder
_builders: dict[str, FlowGraphBuilder] = {}


class AnalyzeRequest(BaseModel):
    project_path: str


class FlowRequest(BaseModel):
    project_path: str
    entry_id: str


class EntryPointResponse(BaseModel):
    id: str
    kind: str
    group: str
    label: str
    trigger: str
    method: str
    path: str
    handler_name: str
    handler_file: str
    handler_line: int
    dependencies: list[str]
    tags: list[str]
    description: str


@app.post("/analyze")
async def analyze_project(req: AnalyzeRequest):
    """Analyze a project and return discovered entrypoints."""
    project_path = req.project_path
    if not Path(project_path).is_dir():
        raise HTTPException(404, f"Directory not found: {project_path}")

    builder = FlowGraphBuilder(project_path)
    entrypoints = builder.get_entrypoints()
    _builders[project_path] = builder

    return {
        "project_path": project_path,
        "entrypoint_count": len(entrypoints),
        "endpoint_count": len([entry for entry in entrypoints if entry.kind == "api"]),
        "entrypoints": [
            EntryPointResponse(
                id=entry.id,
                kind=entry.kind,
                group=entry.group,
                label=entry.label,
                trigger=entry.trigger,
                method=entry.method,
                path=entry.path,
                handler_name=entry.handler_name,
                handler_file=entry.handler_file,
                handler_line=entry.handler_line,
                dependencies=entry.dependencies,
                tags=entry.tags,
                description=entry.description,
            ).model_dump()
            for entry in entrypoints
        ],
        "endpoints": [
            EntryPointResponse(
                id=entry.id,
                kind=entry.kind,
                group=entry.group,
                label=entry.label,
                trigger=entry.trigger,
                method=entry.method,
                path=entry.path,
                handler_name=entry.handler_name,
                handler_file=entry.handler_file,
                handler_line=entry.handler_line,
                dependencies=entry.dependencies,
                tags=entry.tags,
                description=entry.description,
            ).model_dump()
            for entry in entrypoints
            if entry.kind == "api"
        ],
        "middlewares": [
            {"class_name": m.class_name, "file": m.file_path, "line": m.line}
            for m in builder.extractor.middlewares
        ],
        "exception_handlers": [
            {"exception": h.exception_class, "handler": h.handler_name,
             "file": h.file_path, "line": h.line}
            for h in builder.extractor.exception_handlers
        ],
    }


@app.post("/flow")
async def build_flow(req: FlowRequest):
    """Build flow graph for a specific entrypoint."""
    builder = _builders.get(req.project_path)
    if builder is None:
        # Auto-analyze if not cached
        builder = FlowGraphBuilder(req.project_path)
        builder.get_entrypoints()
        _builders[req.project_path] = builder

    # Find matching entrypoint
    entrypoints = builder.get_entrypoints()
    target = None
    for entry in entrypoints:
        if entry.id == req.entry_id:
            target = entry
            break

    if target is None:
        raise HTTPException(
            404,
            f"Entrypoint not found: {req.entry_id}. "
            f"Available: {[entry.id for entry in entrypoints]}"
        )

    flow_graph = builder.build_flow(target)
    return flow_graph.to_dict()


def main():
    import uvicorn
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9120
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
