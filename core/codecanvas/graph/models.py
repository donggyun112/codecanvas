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

    # Canonical IR (merged from CFG / ExecutionGraph)
    CFG_BLOCK = "cfg_block"
    EXEC_STEP = "exec_step"


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
    # Canonical IR
    CONTAINS = "contains"      # Structural parent → child
    CFG_FLOW = "cfg_flow"      # Control flow between CFG blocks
    DATA_FLOW = "data_flow"    # Data/sequence flow between execution steps


@dataclass
class Evidence:
    """Proof for why a node or edge exists."""
    source: str                # "static_analysis", "runtime_trace", "decorator", "type_hint"
    file_path: str | None = None
    line_number: int | None = None
    detail: str = ""           # e.g. "Found in @app.get('/login')"


_KIND_MAP: dict[NodeType, str] = {
    NodeType.TRIGGER: "trigger", NodeType.CLIENT: "trigger",
    NodeType.API: "trigger", NodeType.ENTRYPOINT: "trigger",
    NodeType.DATABASE: "trigger", NodeType.CACHE: "trigger",
    NodeType.EXTERNAL_API: "trigger", NodeType.ERROR_RESPONSE: "trigger",
    NodeType.ROUTER: "pipeline", NodeType.SERVICE: "pipeline",
    NodeType.REPOSITORY: "pipeline", NodeType.MIDDLEWARE: "pipeline",
    NodeType.DEPENDENCY: "pipeline", NodeType.SCHEMA: "pipeline",
    NodeType.VALIDATION: "pipeline", NodeType.SERIALIZATION: "pipeline",
    NodeType.FILE: "file", NodeType.MODULE: "file",
    NodeType.FUNCTION: "function", NodeType.METHOD: "function", NodeType.CLASS: "function",
    NodeType.BRANCH: "statement", NodeType.EXCEPTION: "statement",
    NodeType.LOOP: "statement", NodeType.ASSIGNMENT: "statement",
    NodeType.RETURN: "statement", NodeType.STEP: "statement",
    NodeType.CFG_BLOCK: "cfg_block", NodeType.EXEC_STEP: "exec_step",
}


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
    kind: str = ""                     # Semantic kind: trigger | pipeline | file | function | statement | cfg_block | exec_step
    scope: str = ""                    # Containing function/module qualified name
    level: int = 3                     # Structural abstraction level, not execution order

    def __post_init__(self):
        if not self.display_name:
            self.display_name = self.name
        if self.description:
            self.description = " ".join(self.description.split())
        if not self.kind:
            self.kind = _KIND_MAP.get(self.node_type, "function")
        if not self.scope:
            # Statement-level nodes know their containing function via metadata;
            # this matches the qualified-name convention used by cfg_block /
            # exec_step merges so all `scope` values are comparable.
            fid = self.metadata.get("function_id") if self.metadata else None
            if fid:
                self.scope = fid


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
class DataFlowStep:
    """A high-level data transformation step for the data-flow view.

    Unlike LogicStep (code-level), DataFlowStep represents *what happens to the data*:
    "세션 소유권 확인", "메시지 조회", "포맷 변환" — not code statements.
    """
    id: str
    label: str                         # Human-readable: "메시지 조회", "Check ownership"
    operation: str                     # query | transform | validate | branch | respond | side_effect
    inputs: list[str] = field(default_factory=list)     # Variable names consumed
    output: str | None = None          # Variable name produced
    output_type: str | None = None     # Return type of the callee, if known
    error_label: str | None = None     # "404", "400" for validate failure
    branch_condition: str | None = None  # For branch steps
    branch_id: str | None = None         # Groups steps into the same branch
    branch_paths: list[str] = field(default_factory=list)  # ["stream", "non-stream"] labels
    source_step_ids: list[str] = field(default_factory=list)  # Original L4 step IDs merged into this
    callee_id: str | None = None       # L3 function called, if any


@dataclass
class FlowGraph:
    """Complete flow graph for a single request path."""
    entrypoint: EntryPoint
    nodes: dict[str, FlowNode] = field(default_factory=dict)
    edges: list[FlowEdge] = field(default_factory=list)
    _call_graph: Any = field(default=None, repr=False)

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

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def _serialize_node(self, n: "FlowNode") -> dict[str, Any]:
        return {
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
            "kind": n.kind,
            "scope": n.scope,
            "level": n.level,
        }

    def _serialize_edge(self, e: "FlowEdge") -> dict[str, Any]:
        return {
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

    def to_dict(self, include_execution_graph: bool = True) -> dict[str, Any]:
        """Serialize to dict for JSON transport to VS Code webview.

        Pure function — merges CFG/Execution into a snapshot copy,
        never mutates self.nodes / self.edges.
        """
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
        result: dict[str, Any] = {
            "entrypoint": entrypoint_payload,
            "endpoint": entrypoint_payload,
        }

        # Snapshot: merge into copies, never touch self.nodes / self.edges
        all_nodes: dict[str, FlowNode] = dict(self.nodes)
        all_edges: list[FlowEdge] = list(self.edges)

        # Build CFG / ExecutionGraph and merge into snapshot
        if include_execution_graph:
            cg = self._call_graph if hasattr(self, '_call_graph') else None
            if cg:
                from codecanvas.graph.ast_execution import ASTExecutionBuilder
                from codecanvas.graph.cfg import CFGBuilder

                handler = self.entrypoint
                aeb = ASTExecutionBuilder(cg)
                eg = aeb.build(
                    handler.handler_name,
                    handler.handler_file,
                    handler.handler_line,
                    flow_graph=self,
                )
                _merge_execution_into(all_nodes, all_edges, eg)

                eg_l3 = eg.merge_to_l3()
                if eg_l3.steps:
                    _merge_execution_into(all_nodes, all_edges, eg_l3, prefix="exec_l3")

                cfg_builder = CFGBuilder(cg)
                cfg = cfg_builder.build(
                    handler.handler_name,
                    handler.handler_file,
                    handler.handler_line,
                )
                if cfg.blocks:
                    _merge_cfg_into(all_nodes, all_edges, cfg)

                # Build CFGs for inlined callees so Code Flow view can
                # show their source code in nodes. Collect unique callee
                # functions referenced by exec_l4 steps.
                seen_callees: set[str] = set()
                for step in eg.steps:
                    callee_fn = step.callee_function
                    if not callee_fn or callee_fn in seen_callees:
                        continue
                    seen_callees.add(callee_fn)
                    callee_func = cg.get_function(callee_fn)
                    if not callee_func:
                        continue
                    callee_cfg = cfg_builder.build(
                        callee_func.name,
                        callee_func.file_path,
                        callee_func.line_start,
                    )
                    if callee_cfg.blocks:
                        _merge_cfg_into(
                            all_nodes, all_edges, callee_cfg,
                            prefix=f"cfg_{callee_fn}",
                        )

        # Serialize snapshot (original self unchanged)
        result["nodes"] = {nid: self._serialize_node(n) for nid, n in all_nodes.items()}
        result["edges"] = [self._serialize_edge(e) for e in all_edges]

        return result


# ------------------------------------------------------------------
# Free-function merge helpers (operate on snapshot copies, not self)
# ------------------------------------------------------------------

def _merge_cfg_into(
    nodes: dict[str, FlowNode],
    edges: list[FlowEdge],
    cfg: Any,
    prefix: str = "cfg",
) -> None:
    """Merge ControlFlowGraph blocks/edges into snapshot dicts."""
    existing_edge_ids = {e.id for e in edges}
    for block in cfg.blocks:
        nid = f"{prefix}:{block.id}"
        if nid in nodes:
            continue
        nodes[nid] = FlowNode(
            id=nid,
            node_type=NodeType.CFG_BLOCK,
            name=block.label,
            kind="cfg_block",
            scope=cfg.function_name,
            file_path=block.file_path,
            line_start=block.line_start,
            line_end=block.line_end,
            level=5,
            metadata={
                "cfg_kind": block.kind,
                "statements": [
                    {"line": s.line, "lineEnd": s.line_end, "text": s.text, "kind": s.kind}
                    for s in block.statements
                ],
                **block.metadata,
            },
        )
    for edge in cfg.edges:
        eid = f"{prefix}_e:{edge.id}"
        if eid in existing_edge_ids:
            continue
        existing_edge_ids.add(eid)
        edges.append(FlowEdge(
            id=eid,
            source_id=f"{prefix}:{edge.source_block_id}",
            target_id=f"{prefix}:{edge.target_block_id}",
            edge_type=EdgeType.CFG_FLOW,
            label=edge.label,
            condition=edge.condition,
            metadata={"cfg_kind": edge.kind},
        ))


def _safe_confidence(value: Any) -> Confidence:
    """Map a string confidence to the enum, defaulting on unknown values.

    ExecutionStep.confidence is free-form so we never want a builder typo
    or a future value to crash to_dict().
    """
    if not value:
        return Confidence.DEFINITE
    try:
        return Confidence(value)
    except ValueError:
        return Confidence.DEFINITE


def _merge_execution_into(
    nodes: dict[str, FlowNode],
    edges: list[FlowEdge],
    eg: Any,
    prefix: str = "exec",
) -> None:
    """Merge ExecutionGraph steps/links into snapshot dicts.

    The merged kind disambiguates L3 summary vs L4 detail
    (`exec_l3` / `exec_l4`) so frontend transforms can project by kind
    alone without inspecting node-id prefixes.
    """
    step_kind = "exec_l3" if prefix == "exec_l3" else "exec_l4"
    existing_edge_ids = {e.id for e in edges}
    for step in eg.steps:
        nid = f"{prefix}:{step.id}"
        if nid in nodes:
            continue
        nodes[nid] = FlowNode(
            id=nid,
            node_type=NodeType.EXEC_STEP,
            name=step.label,
            kind=step_kind,
            scope=step.scope,
            file_path=step.file_path,
            line_start=step.line_start,
            line_end=step.line_end,
            confidence=_safe_confidence(step.confidence),
            level=5,
            metadata={
                # Preserve original step metadata first (review_signals,
                # response_origins, branch_explanation, db_query, etc).
                **(step.metadata or {}),
                # Structural fields from named ExecutionStep attrs.
                "operation": step.operation,
                "phase": step.phase,
                "depth": step.depth,
                "inputs": step.inputs,
                "output": step.output,
                "output_type": step.output_type,
                "branch_condition": step.branch_condition,
                "branch_id": step.branch_id,
                "error_label": step.error_label,
                "callee_function": step.callee_function,
                "source_node_ids": step.source_node_ids,
            },
        )
    for link in eg.links:
        eid = f"{prefix}_e:{link.id}"
        if eid in existing_edge_ids:
            continue
        existing_edge_ids.add(eid)
        edges.append(FlowEdge(
            id=eid,
            source_id=f"{prefix}:{link.source_step_id}",
            target_id=f"{prefix}:{link.target_step_id}",
            edge_type=EdgeType.DATA_FLOW,
            label=link.label,
            is_error_path=link.is_error_path,
            metadata={"data_kind": link.kind, "variable": link.variable},
        ))
