"""HTTP server for CodeCanvas - communicates with VS Code extension.

Provides endpoints:
- POST /analyze: Analyze a project directory
- POST /flow: Build flow graph for a specific entrypoint
- POST /flow/from-location: Build flow graph for the function at a file/line
- POST /trace: Execute a request against the target app with tracing
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

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


class LocationFlowRequest(BaseModel):
    project_path: str
    file_path: str
    line: int


class TraceRequest(BaseModel):
    project_path: str
    entry_id: str
    request: dict[str, Any]  # {method, path, headers?, body?}


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


@app.post("/flow/from-location")
async def build_flow_from_location(req: LocationFlowRequest):
    """Build flow graph for the function enclosing a file/line location."""
    builder = _builders.get(req.project_path)
    if builder is None:
        builder = FlowGraphBuilder(req.project_path)
        builder.get_entrypoints()
        _builders[req.project_path] = builder

    target = builder.entrypoint_extractor.locate_function_entrypoint(
        req.file_path,
        req.line,
    )
    if target is None:
        raise HTTPException(
            404,
            f"No function found at {req.file_path}:{req.line}",
        )

    flow_graph = builder.build_flow(target)
    return flow_graph.to_dict()


@app.post("/trace")
async def trace_request(req: TraceRequest):
    """Execute a request against the target app with runtime tracing.

    Returns a FlowGraph annotated with runtime_hit, execution_order,
    and duration_ms on each node and edge.
    """
    from httpx import ASGITransport, AsyncClient

    from codecanvas.tracer.app_discovery import discover_app
    from codecanvas.tracer.mapper import TraceMapper
    from codecanvas.tracer.middleware import TracingMiddleware, tracing_state

    # 1. Ensure static analysis is ready
    builder = _builders.get(req.project_path)
    if builder is None:
        builder = FlowGraphBuilder(req.project_path)
        builder.get_entrypoints()
        _builders[req.project_path] = builder

    # 2. Find the entrypoint
    entrypoints = builder.get_entrypoints()
    target = next((e for e in entrypoints if e.id == req.entry_id), None)
    if target is None:
        raise HTTPException(
            404,
            f"Entrypoint not found: {req.entry_id}. "
            f"Available: {[e.id for e in entrypoints]}",
        )

    # 3. Build static flow graph
    static_graph = builder.build_flow(target)

    # 4. Discover and prepare the target app
    target_app = _discover_target_app(req.project_path)

    # 5. Enable tracing and send request through ASGITransport
    if not tracing_state.enable(req.project_path):
        return {"error": "A previous trace is still in progress. Please wait and retry."}
    user_req = req.request
    method = user_req.get("method", "GET").upper()
    path = user_req.get("path", target.path or "/")
    headers = user_req.get("headers", {})
    body = user_req.get("body")

    response = None
    request_error = None
    try:
        async with AsyncClient(
            transport=ASGITransport(app=target_app),
            base_url="http://trace-target",
        ) as client:
            response = await client.request(
                method=method,
                url=path,
                headers=headers,
                json=body if isinstance(body, (dict, list)) else None,
                content=body if isinstance(body, (str, bytes)) else None,
            )
    except Exception as exc:
        request_error = exc

    # 6. Collect trace — even if the request raised, the trace may exist
    trace = tracing_state.last_result
    if trace is None:
        static_graph.entrypoint.metadata["trace"] = {
            "error": f"Trace was not captured. {request_error or 'Middleware may not be installed.'}",
        }
        return static_graph.to_dict()

    # 7. Merge trace onto static graph
    mapper = TraceMapper(builder.call_graph, project_root=req.project_path)
    merged = mapper.apply(static_graph, trace)

    # 8. Attach the response summary
    if response is not None:
        merged.entrypoint.metadata.setdefault("trace", {}).update({
            "responseStatus": response.status_code,
            "responseBodyLength": len(response.content),
        })
    elif request_error is not None:
        merged.entrypoint.metadata.setdefault("trace", {}).update({
            "responseStatus": 500,
            "requestError": str(request_error),
        })

    return merged.to_dict()


def _discover_target_app(project_path: str) -> Any:
    """Discover the target FastAPI app — always re-imports for fresh code.

    No caching: the user may have edited source since the last trace.
    """
    from codecanvas.tracer.app_discovery import discover_app
    from codecanvas.tracer.middleware import TracingMiddleware

    # Invalidate cached modules from the target project so we pick up edits
    _invalidate_project_modules(project_path)

    target_app = discover_app(project_path)

    # Attach tracing middleware if not already present
    already = any(
        getattr(m, "cls", None) is TracingMiddleware
        for m in getattr(target_app, "user_middleware", [])
    )
    if not already:
        target_app.add_middleware(TracingMiddleware)
        target_app.middleware_stack = None  # force Starlette to rebuild

    return target_app


def _invalidate_project_modules(project_path: str) -> None:
    """Remove cached sys.modules entries for the target project."""
    import os
    real_root = os.path.realpath(project_path)
    to_remove = [
        name for name, mod in sys.modules.items()
        if hasattr(mod, '__file__') and mod.__file__
        and os.path.realpath(mod.__file__).startswith(real_root)
    ]
    for name in to_remove:
        del sys.modules[name]


def main():
    import uvicorn
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9120
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
