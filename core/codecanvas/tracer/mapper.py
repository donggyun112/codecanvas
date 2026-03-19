"""Map runtime trace events onto a static FlowGraph.

Takes a TraceResult and a FlowGraph, resolves each traced function call
to a static node ID, and annotates the graph so the UI can distinguish:

- **hit**: actually executed during this request
- **static_only**: statically possible but not taken
- **runtime_only**: seen in trace but absent from static analysis

Edge ``runtime_hit`` is based on actual caller-callee transitions
observed in the trace.  Runtime-only transitions produce new edges.
"""
from __future__ import annotations

import os

from codecanvas.graph.models import (
    Confidence,
    EdgeType,
    Evidence,
    FlowEdge,
    FlowGraph,
    FlowNode,
    NodeType,
)
from codecanvas.parser.call_graph import CallGraphBuilder
from codecanvas.tracer.models import TraceEventType, TraceResult


class TraceMapper:
    """Merge a runtime trace into a static flow graph."""

    def __init__(self, call_graph: CallGraphBuilder, project_root: str | None = None):
        self.call_graph = call_graph
        self.call_graph.analyze_project()
        self._project_root = project_root or str(call_graph.project_root)

    def apply(self, graph: FlowGraph, trace: TraceResult) -> FlowGraph:
        """Annotate *graph* in-place with runtime trace data and return it."""

        hit_node_ids: list[str] = []   # ordered first-seen
        hit_set: set[str] = set()
        durations: dict[str, float] = {}
        hit_transitions: set[tuple[str, str]] = set()
        call_stack: list[str] = []

        # Maps (file_path, func_name, line) -> node_id for runtime-only stubs
        # so RETURN/EXCEPTION can find nodes created by CALL.
        runtime_id_cache: dict[tuple[str, str, int], str] = {}

        for event in trace.events:
            if event.event_type == TraceEventType.CALL:
                node_id = self._resolve_or_create(event, graph, runtime_id_cache)
                if node_id:
                    # Record caller-callee transition
                    if call_stack:
                        hit_transitions.add((call_stack[-1], node_id))
                    call_stack.append(node_id)

                    if node_id not in hit_set:
                        hit_node_ids.append(node_id)
                        hit_set.add(node_id)

            elif event.event_type == TraceEventType.RETURN:
                node_id = self._resolve_with_cache(event, runtime_id_cache)
                if node_id and node_id in hit_set:
                    ms = event.detail.get("durationMs", 0.0)
                    durations[node_id] = durations.get(node_id, 0.0) + ms
                # Pop call stack
                if call_stack and node_id and call_stack[-1] == node_id:
                    call_stack.pop()

            elif event.event_type == TraceEventType.EXCEPTION:
                node_id = self._resolve_with_cache(event, runtime_id_cache)
                if node_id:
                    if node_id in graph.nodes:
                        hit_set.add(node_id)
                        graph.nodes[node_id].metadata["runtime_exception"] = (
                            event.detail.get("exceptionType", "Unknown")
                        )
                    # Exception unwind: pop from call stack (collector suppresses
                    # the RETURN event for unwinding frames, so we must pop here).
                    if call_stack and call_stack[-1] == node_id:
                        call_stack.pop()

        # --- Mark nodes ---
        for order, node_id in enumerate(hit_node_ids, start=1):
            node = graph.nodes.get(node_id)
            if not node:
                continue
            node.metadata["runtime_hit"] = True
            node.metadata["execution_order"] = order
            if node_id in durations:
                node.metadata["duration_ms"] = round(durations[node_id], 3)
            node.evidence.append(Evidence(
                source="runtime_trace",
                file_path=node.file_path,
                line_number=node.line_start,
                detail=f"Executed at position {order} in trace",
            ))

        # Propagate hit to parent nodes (file, layer)
        for node_id in list(hit_set):
            node = graph.nodes.get(node_id)
            while node and node.parent_id:
                parent = graph.nodes.get(node.parent_id)
                if parent:
                    parent.metadata.setdefault("runtime_hit", True)
                node = parent

        for node in graph.nodes.values():
            node.metadata.setdefault("runtime_hit", False)

        # --- Mark edges based on actual transitions ---
        # First: mark existing static edges
        existing_edge_pairs: set[tuple[str, str]] = set()
        for edge in graph.edges:
            pair = (edge.source_id, edge.target_id)
            existing_edge_pairs.add(pair)
            edge.metadata["runtime_hit"] = pair in hit_transitions

        # Second: create new edges for runtime-only transitions
        edge_counter = len(graph.edges)
        for src_id, tgt_id in hit_transitions:
            if (src_id, tgt_id) in existing_edge_pairs:
                continue
            if src_id not in graph.nodes or tgt_id not in graph.nodes:
                continue
            edge_counter += 1
            graph.add_edge(FlowEdge(
                id=f"rt_e{edge_counter}",
                source_id=src_id,
                target_id=tgt_id,
                edge_type=EdgeType.CALLS,
                confidence=Confidence.RUNTIME_ONLY,
                evidence=[Evidence(
                    source="runtime_trace",
                    detail=f"Transition observed at runtime: {src_id} -> {tgt_id}",
                )],
                metadata={"runtime_hit": True},
            ))

        # --- Attach trace summary ---
        graph.entrypoint.metadata["trace"] = {
            "durationMs": trace.duration_ms,
            "method": trace.metadata.get("method"),
            "path": trace.metadata.get("path"),
            "statusCode": trace.metadata.get("statusCode"),
            "hitNodes": len(hit_set),
            "totalNodes": len(graph.nodes),
        }

        return graph

    def _resolve_or_create(
        self,
        event,
        graph: FlowGraph,
        cache: dict[tuple[str, str, int], str],
    ) -> str | None:
        """Resolve a CALL event to a node ID; create a runtime-only node
        if the function is not in the static graph."""
        cache_key = (event.file_path, event.func_name, event.line)
        node_id = self._resolve_event(event)

        # Already in graph
        if node_id and node_id in graph.nodes:
            cache[cache_key] = node_id
            return node_id

        # Resolved to a known function but not in this flow graph
        if node_id and node_id not in graph.nodes:
            graph.add_node(FlowNode(
                id=node_id,
                node_type=NodeType.FUNCTION,
                name=event.func_name,
                display_name=event.func_name,
                description=f"Discovered at runtime: `{event.func_name}()`.",
                file_path=event.file_path,
                line_start=event.line,
                confidence=Confidence.RUNTIME_ONLY,
                evidence=[Evidence(
                    source="runtime_trace",
                    file_path=event.file_path,
                    line_number=event.line,
                    detail="Called at runtime but not in static flow",
                )],
                level=3,
            ))
            cache[cache_key] = node_id
            return node_id

        # Completely unresolved but still a project file
        if self._is_project_file(event.file_path):
            stub_id = f"runtime.{event.func_name}@{event.file_path}:{event.line}"
            if stub_id not in graph.nodes:
                graph.add_node(FlowNode(
                    id=stub_id,
                    node_type=NodeType.FUNCTION,
                    name=event.func_name,
                    display_name=event.func_name,
                    description=f"Runtime-only call to `{event.func_name}()`; not found in static analysis.",
                    file_path=event.file_path,
                    line_start=event.line,
                    confidence=Confidence.RUNTIME_ONLY,
                    evidence=[Evidence(
                        source="runtime_trace",
                        file_path=event.file_path,
                        line_number=event.line,
                        detail="Unresolved runtime call",
                    )],
                    level=3,
                ))
            cache[cache_key] = stub_id
            return stub_id

        return None

    def _resolve_with_cache(
        self,
        event,
        cache: dict[tuple[str, str, int], str],
    ) -> str | None:
        """Resolve using the cache first (for RETURN/EXCEPTION that must match
        runtime-only stubs created by CALL), then fall back to static resolution."""
        cache_key = (event.file_path, event.func_name, event.line)
        cached = cache.get(cache_key)
        if cached:
            return cached
        return self._resolve_event(event)

    def _resolve_event(self, event) -> str | None:
        """Resolve a trace event to a static node ID (qualified name)."""
        return self.call_graph.resolve_function_id(
            name=event.func_name,
            file_path=event.file_path,
            line_number=event.line,
        )

    def _is_project_file(self, file_path: str) -> bool:
        """Check if a file belongs to the traced project using project_root."""
        if not file_path:
            return False
        try:
            real_root = os.path.realpath(self._project_root)
            real_file = os.path.realpath(file_path)
            return os.path.commonpath([real_root, real_file]) == real_root
        except ValueError:
            return False
