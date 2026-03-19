"""Core graph data models for CodeCanvas.

Every node and edge carries confidence and evidence,
distinguishing definite connections from inferred ones.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class NodeType(enum.Enum):
    """Types of nodes in the flow graph."""
    # Level 0
    TRIGGER = "trigger"
    CLIENT = "client"
    API = "api"
    ENTRYPOINT = "entrypoint"
    DATABASE = "database"
    CACHE = "cache"
    EXTERNAL_API = "external_api"

    # Level 1
    ROUTER = "router"
    SERVICE = "service"
    REPOSITORY = "repository"
    MIDDLEWARE = "middleware"
    DEPENDENCY = "dependency"

    # Level 2
    FILE = "file"
    MODULE = "module"

    # Level 3
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"

    ERROR_RESPONSE = "error_response"
    SCHEMA = "schema"              # Pydantic request/response model
    VALIDATION = "validation"      # Body parsing / validation step
    SERIALIZATION = "serialization"  # Response serialization step

    # Level 4
    BRANCH = "branch"
    EXCEPTION = "exception"
    LOOP = "loop"
    ASSIGNMENT = "assignment"
    RETURN = "return"
    STEP = "step"


class Confidence(enum.Enum):
    """How confident we are about a connection."""
    DEFINITE = "definite"      # Statically verified (direct call, import)
    HIGH = "high"              # Strong evidence (type hint, decorator pattern)
    INFERRED = "inferred"      # Best guess (dynamic dispatch, unresolved)
    RUNTIME_ONLY = "runtime"   # Only seen in runtime trace, not in static


class EdgeType(enum.Enum):
    """Relationship between nodes."""
    CALLS = "calls"
    RETURNS = "returns"
    DEPENDS_ON = "depends_on"
    BINDS = "binds"
    RAISES = "raises"
    QUERIES = "queries"        # DB query
    REQUESTS = "requests"      # External HTTP
    MIDDLEWARE_CHAIN = "middleware_chain"
    INJECTS = "injects"        # FastAPI Depends()
    HANDLES = "handles"        # Exception handler catches error


@dataclass
class Evidence:
    """Proof for why a node or edge exists."""
    source: str                # "static_analysis", "runtime_trace", "decorator", "type_hint"
    file_path: str | None = None
    line_number: int | None = None
    detail: str = ""           # e.g. "Found in @app.get('/login')"


@dataclass
class FlowNode:
    """A node in the request flow graph."""
    id: str
    node_type: NodeType
    name: str                          # Function/class/module name
    display_name: str = ""             # Human-readable label
    description: str = ""              # Natural language explanation
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    confidence: Confidence = Confidence.DEFINITE
    evidence: list[Evidence] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    children: list[str] = field(default_factory=list)  # Child node IDs (for abstraction grouping)
    parent_id: str | None = None
    level: int = 3                     # Structural abstraction level, not execution order

    def __post_init__(self):
        if not self.display_name:
            self.display_name = self.name
        if self.description:
            self.description = " ".join(self.description.split())


@dataclass
class FlowEdge:
    """A directed edge between two flow nodes."""
    id: str
    source_id: str
    target_id: str
    edge_type: EdgeType
    label: str = ""
    confidence: Confidence = Confidence.DEFINITE
    evidence: list[Evidence] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # For branching paths
    condition: str | None = None       # e.g. "if user is None"
    is_error_path: bool = False


@dataclass
class EntryPoint:
    """An execution entry point extracted from the codebase."""
    kind: str = "api"                  # api, script, function, job, worker
    id: str = ""
    group: str = ""
    label: str = ""
    trigger: str = ""
    method: str = ""                   # GET, POST, PUT, DELETE for API
    path: str = ""                     # /api/v1/login or logical path/label
    handler_name: str = ""             # Function name
    handler_file: str = ""             # File path
    handler_line: int = 0              # Line number
    dependencies: list[str] = field(default_factory=list)  # Depends() refs
    tags: list[str] = field(default_factory=list)
    response_model: str | None = None
    request_body: str | None = None    # Pydantic model for request body
    return_type: str | None = None     # Handler return annotation
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.group:
            self.group = {
                "api": "API",
                "script": "Scripts",
                "function": "Functions",
                "job": "Jobs",
                "worker": "Workers",
            }.get(self.kind, "Entrypoints")

        if not self.label:
            if self.kind == "api":
                self.label = f"{self.method} {self.path}".strip()
            elif self.path:
                self.label = self.path
            else:
                self.label = self.handler_name

        if not self.trigger:
            if self.kind == "api":
                self.trigger = f"HTTP {self.method} {self.path}".strip()
            elif self.kind == "script":
                self.trigger = f"Script: {self.label}"
            elif self.kind == "function":
                self.trigger = f"Function: {self.label}"
            else:
                self.trigger = self.label

        if not self.id:
            if self.kind == "api":
                self.id = f"api:{self.method}:{self.path}"
            else:
                self.id = f"{self.kind}:{self.handler_file}:{self.handler_name}:{self.handler_line}"

        if self.description:
            self.description = " ".join(self.description.split())


Endpoint = EntryPoint


@dataclass
class FlowGraph:
    """Complete flow graph for a single request path."""
    entrypoint: EntryPoint
    nodes: dict[str, FlowNode] = field(default_factory=dict)
    edges: list[FlowEdge] = field(default_factory=list)

    def add_node(self, node: FlowNode) -> None:
        self.nodes[node.id] = node

    def add_edge(self, edge: FlowEdge) -> None:
        self.edges.append(edge)

    def get_nodes_at_level(self, level: int) -> list[FlowNode]:
        """Get nodes visible at a given abstraction level."""
        return [n for n in self.nodes.values() if n.level <= level]

    def get_edges_at_level(self, level: int) -> list[FlowEdge]:
        """Get edges between nodes visible at a given level."""
        visible_ids = {n.id for n in self.get_nodes_at_level(level)}
        return [e for e in self.edges
                if e.source_id in visible_ids and e.target_id in visible_ids]

    @property
    def endpoint(self) -> EntryPoint:
        """Backward-compatible alias for older API-centric callers."""
        return self.entrypoint

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON transport to VS Code webview."""
        entrypoint_payload = {
            "id": self.entrypoint.id,
            "kind": self.entrypoint.kind,
            "group": self.entrypoint.group,
            "label": self.entrypoint.label,
            "trigger": self.entrypoint.trigger,
            "method": self.entrypoint.method,
            "path": self.entrypoint.path,
            "handler_name": self.entrypoint.handler_name,
            "handler_file": self.entrypoint.handler_file,
            "handler_line": self.entrypoint.handler_line,
            "dependencies": self.entrypoint.dependencies,
            "tags": self.entrypoint.tags,
            "response_model": self.entrypoint.response_model,
            "description": self.entrypoint.description,
            "metadata": self.entrypoint.metadata,
        }
        return {
            "entrypoint": entrypoint_payload,
            "endpoint": entrypoint_payload,
            "nodes": {
                nid: {
                    "id": n.id,
                    "type": n.node_type.value,
                    "name": n.name,
                    "displayName": n.display_name,
                    "description": n.description,
                    "filePath": n.file_path,
                    "lineStart": n.line_start,
                    "lineEnd": n.line_end,
                    "confidence": n.confidence.value,
                    "evidence": [
                        {"source": e.source, "filePath": e.file_path,
                         "lineNumber": e.line_number, "detail": e.detail}
                        for e in n.evidence
                    ],
                    "metadata": n.metadata,
                    "children": n.children,
                    "parentId": n.parent_id,
                    "level": n.level,
                }
                for nid, n in self.nodes.items()
            },
            "edges": [
                {
                    "id": e.id,
                    "sourceId": e.source_id,
                    "targetId": e.target_id,
                    "type": e.edge_type.value,
                    "label": e.label,
                    "confidence": e.confidence.value,
                    "evidence": [
                        {"source": ev.source, "filePath": ev.file_path,
                         "lineNumber": ev.line_number, "detail": ev.detail}
                        for ev in e.evidence
                    ],
                    "condition": e.condition,
                    "isErrorPath": e.is_error_path,
                    "metadata": e.metadata,
                }
                for e in self.edges
            ],
        }
