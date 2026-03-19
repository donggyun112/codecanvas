"""Enrich static analysis with OpenAPI runtime introspection.

When a live FastAPI app is available, ``/openapi.json`` provides the
ground-truth route list, request/response schemas, and parameter info.
This module merges that into the existing static analysis results.
"""
from __future__ import annotations

from typing import Any


def extract_openapi_spec(app: Any) -> dict[str, Any] | None:
    """Extract the OpenAPI schema dict from a live FastAPI app.

    Does NOT start a server — calls the app's ``openapi()`` method directly.
    Returns None if the app doesn't support OpenAPI.
    """
    openapi_fn = getattr(app, "openapi", None)
    if not callable(openapi_fn):
        return None
    try:
        return openapi_fn()
    except Exception:
        return None


def enrich_endpoints(
    endpoints: list[Any],
    spec: dict[str, Any],
) -> list[dict[str, Any]]:
    """Merge OpenAPI spec info into existing endpoint data.

    Returns a list of enrichment dicts keyed by (method, path) with:
    - request_body schema name
    - response schema name
    - parameters (path, query, header)
    - description / summary
    - tags
    """
    enrichments: list[dict[str, Any]] = []

    paths = spec.get("paths", {})
    for path, methods in paths.items():
        for method, operation in methods.items():
            if not isinstance(operation, dict):
                continue
            method_upper = method.upper()

            enrichment: dict[str, Any] = {
                "method": method_upper,
                "path": path,
                "operation_id": operation.get("operationId", ""),
                "summary": operation.get("summary", ""),
                "description": operation.get("description", ""),
                "tags": operation.get("tags", []),
            }

            # Request body schema
            req_body = operation.get("requestBody", {})
            req_content = req_body.get("content", {})
            json_schema = req_content.get("application/json", {}).get("schema", {})
            req_ref = json_schema.get("$ref", "")
            if req_ref:
                enrichment["request_schema"] = _ref_to_name(req_ref)

            # Response schema (first 2xx response)
            responses = operation.get("responses", {})
            for status, resp in responses.items():
                if status.startswith("2") and isinstance(resp, dict):
                    resp_content = resp.get("content", {})
                    resp_schema = resp_content.get("application/json", {}).get("schema", {})
                    resp_ref = resp_schema.get("$ref", "")
                    if resp_ref:
                        enrichment["response_schema"] = _ref_to_name(resp_ref)
                    break

            # Parameters
            params = operation.get("parameters", [])
            if params:
                enrichment["parameters"] = [
                    {
                        "name": p.get("name", ""),
                        "in": p.get("in", ""),
                        "required": p.get("required", False),
                        "schema_type": p.get("schema", {}).get("type", ""),
                    }
                    for p in params
                    if isinstance(p, dict)
                ]

            enrichments.append(enrichment)

    return enrichments


def apply_enrichments(
    endpoints: list[Any],
    enrichments: list[dict[str, Any]],
) -> None:
    """Apply OpenAPI enrichments back to endpoint objects.

    Updates request_body, response_model, description, and tags
    when the static analysis missed them.
    """
    enrich_map: dict[tuple[str, str], dict[str, Any]] = {
        (e["method"], e["path"]): e for e in enrichments
    }

    for ep in endpoints:
        key = (getattr(ep, "method", ""), getattr(ep, "path", ""))
        info = enrich_map.get(key)
        if not info:
            continue

        if not getattr(ep, "request_body", None) and info.get("request_schema"):
            ep.request_body = info["request_schema"]

        if not getattr(ep, "response_model", None) and info.get("response_schema"):
            ep.response_model = info["response_schema"]

        if not getattr(ep, "description", "") and info.get("summary"):
            ep.description = info["summary"]

        if not getattr(ep, "tags", []) and info.get("tags"):
            ep.tags = info["tags"]

        # Store full OpenAPI info in metadata for the detail panel
        meta = getattr(ep, "metadata", None)
        if meta is not None:
            meta["openapi"] = {
                k: v for k, v in info.items()
                if k not in ("method", "path")
            }


def discover_missing_routes(
    endpoints: list[Any],
    enrichments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Find routes in OpenAPI spec that static analysis missed."""
    known = {
        (getattr(ep, "method", ""), getattr(ep, "path", ""))
        for ep in endpoints
    }
    return [
        e for e in enrichments
        if (e["method"], e["path"]) not in known
    ]


def _ref_to_name(ref: str) -> str:
    """Convert ``#/components/schemas/LoginRequest`` → ``LoginRequest``."""
    return ref.rsplit("/", 1)[-1] if "/" in ref else ref
