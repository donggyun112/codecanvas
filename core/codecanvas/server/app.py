"""HTTP server for CodeCanvas - communicates with VS Code extension.

Provides endpoints:
- POST /analyze: Analyze a project directory
- GET /endpoints: List discovered FastAPI endpoints
- POST /flow: Build flow graph for a specific endpoint
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
    method: str
    path: str


class EndpointResponse(BaseModel):
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
    """Analyze a project and return discovered endpoints."""
    project_path = req.project_path
    if not Path(project_path).is_dir():
        raise HTTPException(404, f"Directory not found: {project_path}")

    builder = FlowGraphBuilder(project_path)
    endpoints = builder.get_endpoints()
    _builders[project_path] = builder

    return {
        "project_path": project_path,
        "endpoint_count": len(endpoints),
        "endpoints": [
            EndpointResponse(
                method=ep.method,
                path=ep.path,
                handler_name=ep.handler_name,
                handler_file=ep.handler_file,
                handler_line=ep.handler_line,
                dependencies=ep.dependencies,
                tags=ep.tags,
                description=ep.description,
            ).model_dump()
            for ep in endpoints
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
    """Build flow graph for a specific endpoint."""
    builder = _builders.get(req.project_path)
    if builder is None:
        # Auto-analyze if not cached
        builder = FlowGraphBuilder(req.project_path)
        builder.get_endpoints()
        _builders[req.project_path] = builder

    # Find matching endpoint
    endpoints = builder.get_endpoints()
    target = None
    for ep in endpoints:
        if ep.method == req.method.upper() and ep.path == req.path:
            target = ep
            break

    if target is None:
        raise HTTPException(
            404,
            f"Endpoint not found: {req.method.upper()} {req.path}. "
            f"Available: {[(e.method, e.path) for e in endpoints]}"
        )

    flow_graph = builder.build_flow(target)
    return flow_graph.to_dict()


def main():
    import uvicorn
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9120
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
