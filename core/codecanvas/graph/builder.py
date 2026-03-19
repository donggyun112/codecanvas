"""Build complete FlowGraph for a given entry point.

Combines entry-point extraction with call graph analysis
to produce a multi-level flow graph.
"""
from __future__ import annotations

import ast
import os

from codecanvas.graph.models import (
    Confidence,
    EdgeType,
    EntryPoint,
    Evidence,
    FlowEdge,
    FlowGraph,
    FlowNode,
    NodeType,
)
from codecanvas.parser.call_graph import CallGraphBuilder
from codecanvas.parser.fastapi_extractor import ExceptionHandlerInfo, FastAPIExtractor
from codecanvas.parser.entrypoint_extractor import EntryPointExtractor

# Map node types to semantic layer names for Level 1 grouping
_LAYER_MAP = {
    NodeType.ROUTER: "routers",
    NodeType.SERVICE: "services",
    NodeType.REPOSITORY: "repositories",
    NodeType.MIDDLEWARE: "middleware",
    NodeType.DEPENDENCY: "dependencies",
    NodeType.ENTRYPOINT: "entrypoints",
    NodeType.FUNCTION: "logic",
    NodeType.METHOD: "logic",
}


class FlowGraphBuilder:
    """Build a complete FlowGraph from static analysis."""

    def __init__(self, project_root: str):
        self.project_root = project_root
        self.extractor = FastAPIExtractor(project_root)
        self.entrypoint_extractor = EntryPointExtractor(project_root, self.extractor)
        self.call_graph = CallGraphBuilder(project_root)
        self._entrypoints: list[EntryPoint] | None = None
        self._endpoints: list[EntryPoint] | None = None

    def get_entrypoints(self) -> list[EntryPoint]:
        """Full entrypoint scan: APIs + scripts + function fallbacks.

        Triggers full project analysis. Use get_endpoints() for the
        lightweight sidebar listing.
        """
        if self._entrypoints is None:
            self._entrypoints = self.entrypoint_extractor.analyze()
        return self._entrypoints

    def get_endpoints(self) -> list[EntryPoint]:
        """Lightweight API-only listing for the sidebar.

        Only parses route decorators — skips call graph, middleware,
        dependency resolution, and function/script entrypoint scanning.
        """
        if self._endpoints is None:
            self._endpoints = list(self.extractor.scan_routes())
        return self._endpoints

    def build_flow(self, entrypoint: EntryPoint) -> FlowGraph:
        """Build a complete flow graph for a single entry point.

        Triggers full analysis lazily (call graph, middleware, dependencies)
        only on first flow build — not during sidebar listing.
        """
        self.extractor.analyze()  # middleware + exception handlers (lazy, skips if done)
        graph = FlowGraph(entrypoint=entrypoint)
        caller_depth = 0
        if entrypoint.kind == "function":
            caller_depth = int(entrypoint.metadata.get("caller_depth", 0) or 0)
        mark_context_root = bool(entrypoint.metadata.get("from_location"))

        # Level 1-2: Module/service grouping (derived from file paths)
        # Level 3: Function-level call graph — build FIRST so handler node exists
        nodes, edges = self.call_graph.build_flow_from(
            handler_name=entrypoint.handler_name,
            handler_file=entrypoint.handler_file,
            line_number=entrypoint.handler_line,
            caller_depth=caller_depth,
            mark_context_root=mark_context_root,
        )
        for node in nodes.values():
            graph.add_node(node)
        for edge in edges:
            graph.add_edge(edge)
        self._add_dependency_callers_for_function_context(graph, entrypoint)

        # Level 0: Trigger -> API/EntryPoint -> handler (must come after call graph)
        self._add_level0_nodes(graph, entrypoint)

        if entrypoint.kind == "api":
            # Add dependency injection nodes
            self._add_dependency_nodes(graph, entrypoint)
            # Add middleware chain
            self._add_middleware_nodes(graph)

        # Build abstraction levels
        self._build_level_hierarchy(graph)
        self._rewrite_execution_pipeline(graph)
        self._connect_error_paths(graph)
        self._annotate_pipeline_phases(graph)
        self._fill_missing_descriptions(graph)

        return graph

    def _add_level0_nodes(self, graph: FlowGraph, entrypoint: EntryPoint) -> None:
        """Add Level 0 nodes: Trigger, entrypoint/API, DB, Cache, External."""
        trigger_label = entrypoint.trigger or entrypoint.label or entrypoint.handler_name
        graph.add_node(FlowNode(
            id="trigger",
            node_type=NodeType.TRIGGER,
            name="Trigger",
            display_name=trigger_label,
            description=self._describe_trigger(entrypoint),
            confidence=Confidence.DEFINITE,
            level=0,
            metadata={
                "kind": entrypoint.kind,
                "method": entrypoint.method,
                "path": entrypoint.path,
                "label": entrypoint.label,
            },
        ))

        target_id = self._find_handler_node_id(graph, entrypoint)
        source_id = "trigger"

        if entrypoint.kind == "api":
            graph.add_node(FlowNode(
                id="api",
                node_type=NodeType.API,
                name="API",
                display_name=entrypoint.label or f"{entrypoint.method} {entrypoint.path}".strip(),
                description=entrypoint.description or f"Handle {entrypoint.method} {entrypoint.path}.",
                confidence=Confidence.DEFINITE,
                level=0,
                metadata={"kind": entrypoint.kind},
            ))
            graph.add_edge(FlowEdge(
                id="e_trigger_api",
                source_id="trigger",
                target_id="api",
                edge_type=EdgeType.CALLS,
                label=entrypoint.label,
                confidence=Confidence.DEFINITE,
            ))
            source_id = "api"
        else:
            graph.add_node(FlowNode(
                id="entrypoint",
                node_type=NodeType.ENTRYPOINT,
                name=entrypoint.handler_name,
                display_name=entrypoint.label or entrypoint.handler_name,
                description=entrypoint.description or self._describe_non_api_entrypoint(entrypoint),
                file_path=entrypoint.handler_file,
                line_start=entrypoint.handler_line,
                confidence=Confidence.DEFINITE,
                level=0,
                metadata={"kind": entrypoint.kind, "group": entrypoint.group},
            ))
            graph.add_edge(FlowEdge(
                id="e_trigger_entrypoint",
                source_id="trigger",
                target_id="entrypoint",
                edge_type=EdgeType.CALLS,
                label=entrypoint.group,
                confidence=Confidence.DEFINITE,
            ))
            source_id = "entrypoint"

        if entrypoint.kind != "api":
            if entrypoint.metadata.get("from_location"):
                upstream_roots = self._find_upstream_roots(graph, target_id)
                if upstream_roots:
                    for i, root_id in enumerate(upstream_roots):
                        graph.add_edge(FlowEdge(
                            id=f"e_entry_context_{i}",
                            source_id=source_id,
                            target_id=root_id,
                            edge_type=EdgeType.CALLS,
                            confidence=Confidence.DEFINITE,
                            metadata={"context_edge": True},
                        ))
                    return
            graph.add_edge(FlowEdge(
                id="e_entry_handler",
                source_id=source_id,
                target_id=target_id,
                edge_type=EdgeType.CALLS,
                confidence=Confidence.DEFINITE,
            ))

    def _add_dependency_nodes(self, graph: FlowGraph, entrypoint: EntryPoint) -> None:
        """Add Depends() / Security() injection nodes with nested chain support."""
        deps = self.extractor.dependencies.get(
            FastAPIExtractor.dependency_key(entrypoint.handler_name, entrypoint.handler_file), [],
        )
        handler_id = self._find_handler_node_id(graph, entrypoint)
        seen_dep_ids: set[str] = set()
        self._process_dependency_list(
            graph, deps, handler_id, seen_dep_ids, edge_prefix="dep",
        )

    def _process_dependency_list(
        self,
        graph: FlowGraph,
        deps: list,
        target_id: str,
        seen_dep_ids: set[str],
        edge_prefix: str,
    ) -> None:
        """Process a list of DependencyCall, recursing into sub-dependencies."""
        for i, dep in enumerate(deps):
            dep_id = self._dependency_node_id(dep.func_name, dep.resolved_file_path or dep.file_path)

            # Infer declared_type from the dependency function's return annotation
            # when the handler parameter has no explicit type annotation.
            effective_type = dep.declared_type
            if not effective_type:
                effective_type = self._infer_dep_return_type(dep)

            inject_label = self._dependency_injection_label(dep.param_name, effective_type)
            scopes_label = f" scopes={dep.scopes}" if dep.scopes else ""
            marker = "Security" if dep.scopes else "Depends"

            if dep_id not in graph.nodes:
                graph.add_node(FlowNode(
                    id=dep_id,
                    node_type=NodeType.DEPENDENCY,
                    name=dep.func_name,
                    display_name=f"{marker}({dep.func_name})",
                    description=(
                        f"Resolve dependency `{dep.func_name}` for `{inject_label}` before the route handler runs."
                        if inject_label else
                        f"Resolve dependency `{dep.func_name}` before the route handler runs."
                    ),
                    file_path=dep.file_path,
                    line_start=dep.line,
                    confidence=Confidence.DEFINITE,
                    evidence=[Evidence(
                        source="decorator",
                        file_path=dep.file_path,
                        line_number=dep.line,
                        detail=f"{marker}({dep.func_name}){scopes_label}",
                    )],
                    level=1,
                    metadata={
                        "dependency_param": dep.param_name,
                        "declared_type": effective_type,
                        "scopes": dep.scopes or [],
                        "pipeline_phase": "dependency",
                    },
                ))
            else:
                graph.nodes[dep_id].metadata.setdefault("dependency_param", dep.param_name)
                if effective_type:
                    graph.nodes[dep_id].metadata.setdefault("declared_type", effective_type)

            graph.add_edge(FlowEdge(
                id=self._unique_edge_id(graph, f"e_{edge_prefix}_{i}"),
                source_id=dep_id,
                target_id=target_id,
                edge_type=EdgeType.INJECTS,
                label=inject_label or f"{marker}({dep.func_name})",
                confidence=Confidence.DEFINITE,
                metadata={
                    "dependency_param": dep.param_name,
                    "declared_type": effective_type,
                    "call_kind": "dependency",
                    "scopes": dep.scopes or [],
                },
            ))

            dep_file = dep.resolved_file_path or dep.file_path
            if not dep_file:
                continue

            dep_nodes, dep_edges = self.call_graph.build_flow_from(
                handler_name=dep.func_name,
                handler_file=dep_file,
                line_number=dep.resolved_line,
            )
            self._merge_subgraph(graph, dep_nodes, dep_edges, edge_prefix=f"{edge_prefix}{i}")

            dep_root_id = self.call_graph.resolve_function_id(
                dep.func_name, dep_file, dep.resolved_line,
            )
            if dep_root_id and dep_root_id in graph.nodes:
                root_desc = graph.nodes[dep_root_id].description
                if root_desc:
                    graph.nodes[dep_id].description = (
                        f"Resolve dependency `{dep.func_name}` before the route runs. {root_desc}"
                    )
                graph.add_edge(FlowEdge(
                    id=self._unique_edge_id(graph, f"e_{edge_prefix}_root_{i}"),
                    source_id=dep_id,
                    target_id=dep_root_id,
                    edge_type=EdgeType.DEPENDS_ON,
                    label=inject_label or "resolves dependency",
                    confidence=Confidence.HIGH,
                    evidence=[Evidence(
                        source="decorator",
                        file_path=dep.file_path,
                        line_number=dep.line,
                        detail=f"{marker}({dep.func_name}) resolves to {dep_root_id}",
                    )],
                    metadata={
                        "dependency_param": dep.param_name,
                        "declared_type": effective_type,
                    },
                ))

            # Recurse into nested sub-dependencies of this dependency function.
            if dep_id not in seen_dep_ids:
                seen_dep_ids.add(dep_id)
                sub_deps = self._extract_sub_dependencies(dep)
                if sub_deps:
                    self._process_dependency_list(
                        graph, sub_deps, dep_id, seen_dep_ids,
                        edge_prefix=f"{edge_prefix}{i}s",
                    )

    def _extract_sub_dependencies(self, dep) -> list:
        """Extract Depends() from a dependency function's own parameters.

        Uses the call_graph's cached AST node index — no ast.walk().
        """
        dep_file = dep.resolved_file_path or dep.file_path
        if not dep_file:
            return []

        func_id = self.call_graph.resolve_function_id(
            dep.func_name, dep_file, dep.resolved_line,
        )
        if func_id:
            ast_node = self.call_graph._ast_nodes.get(func_id)
            func_def = self.call_graph._functions.get(func_id)
            if ast_node and func_def:
                return self.extractor._extract_depends(ast_node, func_def.file_path)
        return []

    def _infer_dep_return_type(self, dep) -> str | None:
        """Infer the return type from a dependency function's annotation."""
        func_id = self.call_graph.resolve_function_id(
            dep.func_name,
            dep.resolved_file_path or dep.file_path,
            dep.resolved_line,
        )
        if not func_id:
            return None
        func_def = self.call_graph._functions.get(func_id)
        if func_def and func_def.return_annotation:
            return func_def.return_annotation
        return None

    def _add_middleware_nodes(self, graph: FlowGraph) -> None:
        """Add middleware chain nodes."""
        for i, mw in enumerate(self.extractor.middlewares):
            mw_id = f"middleware.{mw.class_name}"
            graph.add_node(FlowNode(
                id=mw_id,
                node_type=NodeType.MIDDLEWARE,
                name=mw.class_name,
                display_name=mw.class_name,
                description=f"Run {mw.class_name} before passing control to the route layer.",
                file_path=mw.file_path,
                line_start=mw.line,
                confidence=Confidence.DEFINITE,
                level=1,
            ))

    def _build_level_hierarchy(self, graph: FlowGraph) -> None:
        """Build abstraction levels with proper parent/child and lifted edges.

        - Level 2: file nodes grouping L3 functions
        - Level 1: layer nodes grouping L2 files by semantic role
        - Lifted edges: derived from lower-level edges so each level is connected
        """
        # --- Level 2: group L3 nodes by file ---
        file_groups: dict[str, list[str]] = {}
        for node in graph.nodes.values():
            if node.level == 3 and node.file_path:
                file_groups.setdefault(node.file_path, []).append(node.id)

        file_node_map: dict[str, str] = {}  # file_path -> file_node_id
        for file_path, node_ids in file_groups.items():
            basename = os.path.basename(file_path)
            file_id = f"file.{file_path}"
            graph.add_node(FlowNode(
                id=file_id,
                node_type=NodeType.FILE,
                name=basename,
                display_name=basename,
                description=self._describe_file_node(graph, basename, node_ids),
                file_path=file_path,
                confidence=Confidence.DEFINITE,
                level=2,
                children=node_ids,
            ))
            file_node_map[file_path] = file_id
            for nid in node_ids:
                graph.nodes[nid].parent_id = file_id

        # Also assign L4 nodes to their parent function when known, otherwise file
        for node in list(graph.nodes.values()):
            if node.level == 4 and node.file_path and node.file_path in file_node_map:
                function_id = node.metadata.get("function_id")
                if function_id and function_id in graph.nodes:
                    node.parent_id = function_id
                    parent_func = graph.nodes[function_id]
                    if node.id not in parent_func.children:
                        parent_func.children.append(node.id)
                    continue

                node.parent_id = file_node_map[node.file_path]
                file_node = graph.nodes[file_node_map[node.file_path]]
                if node.id not in file_node.children:
                    file_node.children.append(node.id)

        # --- Level 1: group files by semantic layer ---
        layer_groups: dict[str, list[str]] = {}  # layer_name -> [file_node_ids]
        for file_path, file_node_id in file_node_map.items():
            layer = self._classify_file_layer(graph, file_groups.get(file_path, []), file_path)
            layer_groups.setdefault(layer, []).append(file_node_id)

        layer_node_map: dict[str, str] = {}  # file_node_id -> layer_node_id
        for layer_name, file_node_ids in layer_groups.items():
            layer_id = f"layer.{layer_name}"
            display = layer_name.replace("_", " ").title()
            graph.add_node(FlowNode(
                id=layer_id,
                node_type=NodeType.MODULE,
                name=layer_name,
                display_name=display,
                description=self._describe_layer_node(graph, layer_name, file_node_ids),
                confidence=Confidence.DEFINITE,
                level=1,
                children=file_node_ids,
            ))
            for fid in file_node_ids:
                graph.nodes[fid].parent_id = layer_id
                layer_node_map[fid] = layer_id

        # --- Lift L3 edges to L2 (file→file) ---
        seen_l2: set[tuple[str, str]] = set()
        for edge in list(graph.edges):
            src = graph.nodes.get(edge.source_id)
            tgt = graph.nodes.get(edge.target_id)
            if not src or not tgt:
                continue
            src_file = src.parent_id if src.level == 3 else None
            tgt_file = tgt.parent_id if tgt.level == 3 else None
            if src_file and tgt_file and src_file != tgt_file:
                key = (src_file, tgt_file)
                if key not in seen_l2:
                    seen_l2.add(key)
                    graph.add_edge(FlowEdge(
                        id=f"e_l2_{src_file}_{tgt_file}",
                        source_id=src_file,
                        target_id=tgt_file,
                        edge_type=edge.edge_type,
                        confidence=edge.confidence,
                    ))

        # --- Lift L2 edges to L1 (layer→layer) ---
        seen_l1: set[tuple[str, str]] = set()
        for edge in list(graph.edges):
            src = graph.nodes.get(edge.source_id)
            tgt = graph.nodes.get(edge.target_id)
            if not src or not tgt:
                continue
            if src.level != 2 or tgt.level != 2:
                continue
            src_layer = layer_node_map.get(src.id)
            tgt_layer = layer_node_map.get(tgt.id)
            if src_layer and tgt_layer and src_layer != tgt_layer:
                key = (src_layer, tgt_layer)
                if key not in seen_l1:
                    seen_l1.add(key)
                    graph.add_edge(FlowEdge(
                        id=f"e_l1_{src_layer}_{tgt_layer}",
                        source_id=src_layer,
                        target_id=tgt_layer,
                        edge_type=edge.edge_type,
                        confidence=edge.confidence,
                    ))

        # --- Lift cross-level edges (L0→L3 like api→handler) ---
        handler_id = self._find_handler_node_id(graph, graph.entrypoint)
        handler_node = graph.nodes.get(handler_id)
        if handler_node:
            handler_file_id = handler_node.parent_id
            if handler_file_id:
                # api→file (L0→L2)
                graph.add_edge(FlowEdge(
                    id="e_root_file",
                    source_id=self._root_flow_node_id(graph),
                    target_id=handler_file_id,
                    edge_type=EdgeType.CALLS,
                    confidence=Confidence.DEFINITE,
                ))
                handler_layer_id = layer_node_map.get(handler_file_id)
                if handler_layer_id:
                    # api→layer (L0→L1)
                    graph.add_edge(FlowEdge(
                        id="e_root_layer",
                        source_id=self._root_flow_node_id(graph),
                        target_id=handler_layer_id,
                        edge_type=EdgeType.CALLS,
                        confidence=Confidence.DEFINITE,
                    ))

        # --- Connect middleware/dependency nodes at L1 with edges ---
        # Middleware sits between client and the router layer
        mw_ids = [nid for nid, n in graph.nodes.items()
                   if n.node_type == NodeType.MIDDLEWARE and n.level == 1]
        router_layer = "layer.routers"
        if mw_ids and router_layer in graph.nodes:
            prev_id = self._root_flow_node_id(graph)
            for mw_id in mw_ids:
                graph.add_edge(FlowEdge(
                    id=f"e_mw_{prev_id}_{mw_id}",
                    source_id=prev_id,
                    target_id=mw_id,
                    edge_type=EdgeType.MIDDLEWARE_CHAIN,
                    confidence=Confidence.DEFINITE,
                ))
                prev_id = mw_id
            graph.add_edge(FlowEdge(
                id=f"e_mw_{prev_id}_router",
                source_id=prev_id,
                target_id=router_layer,
                edge_type=EdgeType.MIDDLEWARE_CHAIN,
                confidence=Confidence.DEFINITE,
            ))

        # Dependency nodes inject into the handler's layer
        dep_ids = [nid for nid, n in graph.nodes.items()
                   if n.node_type == NodeType.DEPENDENCY and n.level == 1]
        if dep_ids and router_layer in graph.nodes:
            for dep_id in dep_ids:
                graph.add_edge(FlowEdge(
                    id=f"e_dep_l1_{dep_id}",
                    source_id=dep_id,
                    target_id=router_layer,
                    edge_type=EdgeType.INJECTS,
                    confidence=Confidence.DEFINITE,
                ))

        # --- L0 system-level edges ---
        # Detect if flow touches DB or external APIs and create L0 edges
        has_db = any(n.node_type in (NodeType.DATABASE, NodeType.REPOSITORY)
                     for n in graph.nodes.values())
        has_ext = any(n.node_type == NodeType.EXTERNAL_API
                      for n in graph.nodes.values())
        if has_db and "database" not in graph.nodes:
            graph.add_node(FlowNode(
                id="database",
                node_type=NodeType.DATABASE,
                name="Database",
                display_name="Database",
                description="Database touched by this request flow.",
                confidence=Confidence.DEFINITE,
                level=0,
            ))
            graph.add_edge(FlowEdge(
                id="e_api_db",
                source_id=self._root_flow_node_id(graph),
                target_id="database",
                edge_type=EdgeType.QUERIES,
                confidence=Confidence.INFERRED,
            ))
        if has_ext and "external" not in graph.nodes:
            graph.add_node(FlowNode(
                id="external",
                node_type=NodeType.EXTERNAL_API,
                name="External API",
                display_name="External API",
                description="External HTTP dependency touched by this request flow.",
                confidence=Confidence.INFERRED,
                level=0,
            ))
            graph.add_edge(FlowEdge(
                id="e_api_ext",
                source_id=self._root_flow_node_id(graph),
                target_id="external",
                edge_type=EdgeType.REQUESTS,
                confidence=Confidence.INFERRED,
            ))

    @staticmethod
    def _classify_file_layer(
        graph: FlowGraph, node_ids: list[str], file_path: str | None = None,
    ) -> str:
        """Determine the semantic layer for a file.

        File path heuristics take priority over node-type counting,
        because a file like dependencies.py may contain utility classes
        (FakeDB → METHOD → "logic") that skew the count.
        """
        if file_path:
            fp = file_path.lower()
            if any(p in fp for p in ("dependenc", "deps", "inject")):
                return "dependencies"
            if any(p in fp for p in ("script", "cli", "command")) or fp.endswith("main.py"):
                return "entrypoints"
            if any(p in fp for p in ("middleware",)):
                return "middleware"
            if any(p in fp for p in ("route", "router", "endpoint", "view", "controller")):
                return "routers"
            if any(p in fp for p in ("service", "usecase", "logic")):
                return "services"
            if any(p in fp for p in ("repo", "repository", "crud", "dao", "dal")):
                return "repositories"

        # Fallback: count node types
        type_counts: dict[str, int] = {}
        for nid in node_ids:
            node = graph.nodes.get(nid)
            if not node:
                continue
            layer = _LAYER_MAP.get(node.node_type, "logic")
            type_counts[layer] = type_counts.get(layer, 0) + 1
        if not type_counts:
            return "logic"
        return max(type_counts, key=type_counts.get)  # type: ignore

    def _find_handler_node_id(self, graph: FlowGraph, entrypoint: EntryPoint) -> str:
        """Find the node ID for the entrypoint handler function."""
        # Look for exact match by name
        for nid, node in graph.nodes.items():
            if (node.name == entrypoint.handler_name
                    and node.file_path == entrypoint.handler_file):
                return nid
        return entrypoint.handler_name

    @staticmethod
    def _find_upstream_roots(graph: FlowGraph, target_id: str) -> list[str]:
        """Return the top-most upstream caller branches for a centered function flow."""
        upstream_ids = {
            node.id for node in graph.nodes.values()
            if node.metadata.get("upstream_distance")
        }
        if not upstream_ids:
            return []

        roots: list[str] = []
        for node_id in sorted(upstream_ids):
            has_upstream_parent = any(
                edge.target_id == node_id
                and edge.source_id in upstream_ids
                for edge in graph.edges
            )
            if not has_upstream_parent:
                roots.append(node_id)
        return roots

    def _add_dependency_callers_for_function_context(
        self,
        graph: FlowGraph,
        entrypoint: EntryPoint,
    ) -> None:
        """Attach FastAPI Depends() route callers above a selected function flow."""
        if entrypoint.kind != "function" or not entrypoint.metadata.get("from_location"):
            return

        target_id = self.call_graph.resolve_function_id(
            entrypoint.handler_name,
            entrypoint.handler_file,
            entrypoint.handler_line,
        )
        if not target_id or target_id not in graph.nodes:
            return

        endpoint_index = {
            (endpoint.handler_file, endpoint.handler_name): endpoint
            for endpoint in self.get_endpoints()
        }
        prefix_index = 0
        for handler_key, deps in self.extractor.dependencies.items():
            if ":" not in handler_key:
                continue
            handler_file, handler_name = handler_key.rsplit(":", 1)
            route_entry = endpoint_index.get((handler_file, handler_name))
            route_line = route_entry.handler_line if route_entry else None

            for dep in deps:
                dep_target_id = self.call_graph.resolve_function_id(
                    dep.func_name,
                    dep.resolved_file_path or dep.file_path,
                    dep.resolved_line,
                )
                if dep_target_id != target_id:
                    continue

                route_id = self.call_graph.resolve_function_id(
                    handler_name,
                    handler_file,
                    route_line,
                )
                if not route_id:
                    continue

                if route_id not in graph.nodes:
                    route_nodes, route_edges = self.call_graph.build_flow_from(
                        handler_name=handler_name,
                        handler_file=handler_file,
                        line_number=route_line,
                        max_depth=-1,
                    )
                    self._merge_subgraph(
                        graph,
                        route_nodes,
                        route_edges,
                        edge_prefix=f"depcaller{prefix_index}",
                    )
                    prefix_index += 1

                route_node = graph.nodes.get(route_id)
                if route_node is not None:
                    existing_distance = route_node.metadata.get("upstream_distance")
                    if existing_distance is None or 1 < existing_distance:
                        route_node.metadata["upstream_distance"] = 1
                    route_node.metadata["context_direction"] = "upstream"

                self._upsert_edge(
                    graph,
                    preferred_id=f"e_depcaller_{route_id}_{target_id}",
                    source_id=route_id,
                    target_id=target_id,
                    edge_type=EdgeType.INJECTS,
                    confidence=Confidence.HIGH,
                    label=self._dependency_injection_label(dep.param_name, dep.declared_type)
                    or f"Depends({dep.func_name})",
                    metadata={
                        "upstream_edge": True,
                        "upstream_relation": "dependency",
                        "dependency_param": dep.param_name,
                        "declared_type": dep.declared_type,
                    },
                )

    def _rewrite_execution_pipeline(self, graph: FlowGraph) -> None:
        """Rewrite API entrypoint edges to match request pipeline semantics.

        The graph ``level`` is structural abstraction only. For API flows, the
        request pipeline should read as:
            trigger -> api -> middleware* -> dependency* -> handler
        while file/layer lift edges hang off the last pre-handler stage.
        """
        if graph.entrypoint.kind != "api":
            return

        handler_id = self._find_handler_node_id(graph, graph.entrypoint)
        handler_node = graph.nodes.get(handler_id)
        if not handler_node:
            return

        graph.edges = [
            edge for edge in graph.edges
            if edge.id != "e_entry_handler"
            and edge.id != "e_root_file"
            and edge.id != "e_root_layer"
            and not edge.id.startswith("e_mw_")
            and not edge.id.startswith("e_dep_l1_")
        ]

        source_id = self._root_flow_node_id(graph)
        middleware_ids = self._pipeline_nodes(graph, NodeType.MIDDLEWARE)
        dependency_ids = self._pipeline_nodes(graph, NodeType.DEPENDENCY)

        for middleware_id in middleware_ids:
            self._upsert_edge(
                graph,
                preferred_id=f"e_mw_{source_id}_{middleware_id}",
                source_id=source_id,
                target_id=middleware_id,
                edge_type=EdgeType.MIDDLEWARE_CHAIN,
                confidence=Confidence.DEFINITE,
                metadata={"pipeline_edge": True, "pipeline_phase": "middleware"},
            )
            source_id = middleware_id

        handler_file_id = handler_node.parent_id
        handler_layer_id = None
        if handler_file_id and handler_file_id in graph.nodes:
            handler_layer_id = graph.nodes[handler_file_id].parent_id

        if dependency_ids:
            for dependency_id in dependency_ids:
                self._upsert_edge(
                    graph,
                    preferred_id=f"e_pipeline_dep_{source_id}_{dependency_id}",
                    source_id=source_id,
                    target_id=dependency_id,
                    edge_type=EdgeType.CALLS,
                    confidence=Confidence.DEFINITE,
                    metadata={"pipeline_edge": True, "pipeline_phase": "dependency"},
                )

                # Find the resolved function for this dependency
                dep_resolved_id = self._find_dependency_resolved_function(
                    graph, dependency_id,
                )

                if dep_resolved_id:
                    # Dependency resolved function → handler (inject edge)
                    dependency_node = graph.nodes.get(dependency_id)
                    dep_resolved_node = graph.nodes.get(dep_resolved_id)
                    dependency_param = (
                        dependency_node.metadata.get("dependency_param")
                        if dependency_node else None
                    )
                    dependency_type = (
                        (dependency_node.metadata.get("declared_type") if dependency_node else None)
                        or (dep_resolved_node.metadata.get("return_type") if dep_resolved_node else None)
                    )
                    inject_label = (
                        self._dependency_injection_label(
                            str(dependency_param or ""),
                            str(dependency_type or ""),
                        )
                        or "injects result"
                    )
                    self._upsert_edge(
                        graph,
                        preferred_id=f"e_dep_resolved_handler_{dep_resolved_id}",
                        source_id=dep_resolved_id,
                        target_id=handler_id,
                        edge_type=EdgeType.INJECTS,
                        confidence=Confidence.DEFINITE,
                        label=inject_label,
                        metadata={
                            "pipeline_edge": True,
                            "pipeline_phase": "dependency_result",
                            "dependency_param": dependency_param,
                            "dependency_type": dependency_type,
                            "call_kind": "dependency_result",
                        },
                    )
                else:
                    # No resolved function — connect dep node directly to handler
                    self._upsert_edge(
                        graph,
                        preferred_id=f"e_dep_handler_{dependency_id}",
                        source_id=dependency_id,
                        target_id=handler_id,
                        edge_type=EdgeType.INJECTS,
                        confidence=Confidence.DEFINITE,
                        label=f"Depends({graph.nodes[dependency_id].name})",
                        metadata={"pipeline_edge": True, "pipeline_phase": "dependency"},
                    )
                if handler_layer_id:
                    self._upsert_edge(
                        graph,
                        preferred_id=f"e_dep_l1_{dependency_id}",
                        source_id=dependency_id,
                        target_id=handler_layer_id,
                        edge_type=EdgeType.INJECTS,
                        confidence=Confidence.DEFINITE,
                        metadata={
                            "pipeline_edge": True,
                            "pipeline_phase": "dependency",
                            "structural_lift": True,
                        },
                    )
                if handler_file_id:
                    self._upsert_edge(
                        graph,
                        preferred_id=f"e_dep_l2_{dependency_id}",
                        source_id=dependency_id,
                        target_id=handler_file_id,
                        edge_type=EdgeType.CALLS,
                        confidence=Confidence.DEFINITE,
                        metadata={
                            "pipeline_edge": True,
                            "pipeline_phase": "dependency",
                            "structural_lift": True,
                        },
                    )
            return

        self._upsert_edge(
            graph,
            preferred_id="e_entry_handler",
            source_id=source_id,
            target_id=handler_id,
            edge_type=EdgeType.CALLS,
            confidence=Confidence.DEFINITE,
            metadata={"pipeline_edge": True, "pipeline_phase": "handler"},
        )
        if handler_layer_id:
            self._upsert_edge(
                graph,
                preferred_id="e_root_layer",
                source_id=source_id,
                target_id=handler_layer_id,
                edge_type=EdgeType.CALLS,
                confidence=Confidence.DEFINITE,
                metadata={
                    "pipeline_edge": True,
                    "pipeline_phase": "handler",
                    "structural_lift": True,
                },
            )
        if handler_file_id:
            self._upsert_edge(
                graph,
                preferred_id="e_root_file",
                source_id=source_id,
                target_id=handler_file_id,
                edge_type=EdgeType.CALLS,
                confidence=Confidence.DEFINITE,
                metadata={
                    "pipeline_edge": True,
                    "pipeline_phase": "handler",
                    "structural_lift": True,
                },
            )

    def _connect_error_paths(self, graph: FlowGraph) -> None:
        """Route error nodes to exception handlers or a default error response.

        For each RAISES edge:
        - If a registered exception handler matches, add an edge to it.
        - Otherwise, add a default error response node (e.g. HTTP 401 → Client).
        """
        if graph.entrypoint.kind != "api":
            return

        # Index exception handlers by class name
        handler_map: dict[str, ExceptionHandlerInfo] = {}
        for eh in self.extractor.exception_handlers:
            handler_map[eh.exception_class] = eh

        error_nodes = [
            n for n in graph.nodes.values()
            if n.node_type == NodeType.EXCEPTION
        ]
        if not error_nodes:
            return

        # Ensure a client/response node exists for dead-end errors
        response_id = "error_response"
        if response_id not in graph.nodes:
            graph.add_node(FlowNode(
                id=response_id,
                node_type=NodeType.ERROR_RESPONSE,
                name="Error Response",
                display_name="Error Response",
                description="Return an error response to the client.",
                confidence=Confidence.DEFINITE,
                level=0,
            ))

        for error_node in error_nodes:
            exc_class = error_node.name
            status_code = error_node.metadata.get("status_code")

            # Check if a custom exception handler is registered
            eh = handler_map.get(exc_class)
            if eh:
                eh_func_id = self.call_graph.resolve_function_id(
                    eh.handler_name, eh.file_path, eh.line,
                )
                if eh_func_id:
                    # Add the handler function to the graph if missing
                    if eh_func_id not in graph.nodes:
                        eh_nodes, eh_edges = self.call_graph.build_flow_from(
                            handler_name=eh.handler_name,
                            handler_file=eh.file_path,
                            line_number=eh.line,
                        )
                        self._merge_subgraph(graph, eh_nodes, eh_edges, edge_prefix="eh")
                    if eh_func_id in graph.nodes:
                        graph.nodes[eh_func_id].metadata["pipeline_phase"] = "exception_handler"
                        self._upsert_edge(
                            graph,
                            preferred_id=f"e_err_handler_{error_node.id}",
                            source_id=error_node.id,
                            target_id=eh_func_id,
                            edge_type=EdgeType.HANDLES,
                            confidence=Confidence.DEFINITE,
                            label=f"caught by {eh.handler_name}",
                            metadata={"error_path": True},
                        )
                        continue

            # Default: HTTPException or unhandled → error response
            label = f"HTTP {status_code}" if status_code else f"raises {exc_class}"
            self._upsert_edge(
                graph,
                preferred_id=f"e_err_response_{error_node.id}",
                source_id=error_node.id,
                target_id=response_id,
                edge_type=EdgeType.RAISES,
                confidence=Confidence.DEFINITE,
                label=label,
                metadata={"error_path": True, "status_code": status_code},
            )

    def _annotate_pipeline_phases(self, graph: FlowGraph) -> None:
        """Attach pipeline metadata without overloading structural levels."""
        self._annotate_node_phase(graph, "trigger", "trigger", 0)

        root_id = self._root_flow_node_id(graph)
        root_phase = "api" if graph.entrypoint.kind == "api" else "entrypoint"
        self._annotate_node_phase(graph, root_id, root_phase, 10)

        for index, middleware_id in enumerate(self._pipeline_nodes(graph, NodeType.MIDDLEWARE), start=1):
            self._annotate_node_phase(graph, middleware_id, "middleware", 20 + index)

        for index, dependency_id in enumerate(self._pipeline_nodes(graph, NodeType.DEPENDENCY), start=1):
            self._annotate_node_phase(graph, dependency_id, "dependency", 40 + index)
            # Also annotate the resolved function
            resolved_id = self._find_dependency_resolved_function(graph, dependency_id)
            if resolved_id:
                self._annotate_node_phase(graph, resolved_id, "dependency", 40 + index)

        handler_id = self._find_handler_node_id(graph, graph.entrypoint)
        if handler_id in graph.nodes:
            self._annotate_node_phase(graph, handler_id, "handler", 60)

        default_phase_map = {
            NodeType.ROUTER: ("handler", 60),
            NodeType.SERVICE: ("service", 70),
            NodeType.REPOSITORY: ("repository", 80),
            NodeType.DATABASE: ("database", 90),
            NodeType.EXTERNAL_API: ("external", 90),
        }
        for node in graph.nodes.values():
            if node.metadata.get("pipeline_phase"):
                continue
            phase = default_phase_map.get(node.node_type)
            if not phase:
                continue
            self._annotate_node_phase(graph, node.id, phase[0], phase[1])

    @staticmethod
    def _annotate_node_phase(graph: FlowGraph, node_id: str, phase: str, order: int) -> None:
        """Store pipeline metadata on a node."""
        node = graph.nodes.get(node_id)
        if not node:
            return
        node.metadata.setdefault("pipeline_phase", phase)
        node.metadata.setdefault("pipeline_order", order)

    @staticmethod
    def _find_dependency_resolved_function(
        graph: FlowGraph, dependency_node_id: str,
    ) -> str | None:
        """Find the L3 function that a dependency node resolves to."""
        for edge in graph.edges:
            if (edge.source_id == dependency_node_id
                    and edge.edge_type == EdgeType.DEPENDS_ON
                    and edge.target_id in graph.nodes):
                target = graph.nodes[edge.target_id]
                if target.level == 3:
                    return target.id
        return None

    @staticmethod
    def _dependency_injection_label(param_name: str | None, declared_type: str | None) -> str:
        """Human-readable label for a resolved dependency value."""
        if param_name and declared_type:
            return f"injects {param_name}: {declared_type}"
        if param_name:
            return f"injects {param_name}"
        if declared_type:
            return f"injects {declared_type}"
        return ""

    @staticmethod
    def _pipeline_nodes(graph: FlowGraph, node_type: NodeType) -> list[str]:
        """Return pipeline nodes in source order."""
        nodes = [
            node for node in graph.nodes.values()
            if node.node_type == node_type and node.level == 1
        ]
        nodes.sort(key=lambda node: (node.line_start or 0, node.display_name, node.id))
        return [node.id for node in nodes]

    @staticmethod
    def _dependency_node_id(func_name: str, file_path: str | None) -> str:
        """Create a stable dependency node ID without colliding across files."""
        if not file_path:
            return f"dep.{func_name}"
        return f"dep.{func_name}@{file_path}"

    def _merge_subgraph(
        self,
        graph: FlowGraph,
        nodes: dict[str, FlowNode],
        edges: list[FlowEdge],
        edge_prefix: str,
    ) -> None:
        """Merge a subgraph into the main graph, preserving distinct edge IDs."""
        for node_id, node in nodes.items():
            if node_id not in graph.nodes:
                graph.add_node(node)

        for edge in edges:
            graph.add_edge(FlowEdge(
                id=self._unique_edge_id(graph, f"{edge_prefix}.{edge.id}"),
                source_id=edge.source_id,
                target_id=edge.target_id,
                edge_type=edge.edge_type,
                label=edge.label,
                confidence=edge.confidence,
                evidence=list(edge.evidence),
                metadata=dict(edge.metadata),
                condition=edge.condition,
                is_error_path=edge.is_error_path,
            ))

    def _upsert_edge(
        self,
        graph: FlowGraph,
        preferred_id: str,
        source_id: str,
        target_id: str,
        edge_type: EdgeType,
        confidence: Confidence,
        label: str = "",
        metadata: dict | None = None,
    ) -> None:
        """Create or enrich an edge without duplicating the same relationship."""
        for edge in graph.edges:
            if (edge.source_id == source_id
                    and edge.target_id == target_id
                    and edge.edge_type == edge_type):
                if label and not edge.label:
                    edge.label = label
                if metadata:
                    edge.metadata.update(metadata)
                return

        graph.add_edge(FlowEdge(
            id=self._unique_edge_id(graph, preferred_id),
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            label=label,
            confidence=confidence,
            metadata=metadata or {},
        ))

    @staticmethod
    def _unique_edge_id(graph: FlowGraph, preferred_id: str) -> str:
        """Generate a graph-local unique edge ID."""
        existing = {edge.id for edge in graph.edges}
        if preferred_id not in existing:
            return preferred_id

        suffix = 1
        while f"{preferred_id}_{suffix}" in existing:
            suffix += 1
        return f"{preferred_id}_{suffix}"

    def _fill_missing_descriptions(self, graph: FlowGraph) -> None:
        """Populate descriptions for synthetic nodes after the graph is assembled."""
        for node in graph.nodes.values():
            if node.description:
                continue

            if node.node_type == NodeType.DEPENDENCY:
                node.description = self._describe_dependency_node(graph, node)
            elif node.node_type == NodeType.MIDDLEWARE:
                node.description = f"Run {node.display_name} before the route handler."
            elif node.node_type == NodeType.FILE:
                node.description = self._describe_file_node(graph, node.display_name, node.children)
            elif node.node_type == NodeType.MODULE:
                node.description = self._describe_layer_node(graph, node.name, node.children)
            elif node.node_type == NodeType.TRIGGER:
                node.description = self._describe_trigger(graph.entrypoint)
            elif node.node_type == NodeType.ENTRYPOINT:
                node.description = graph.entrypoint.description or self._describe_non_api_entrypoint(graph.entrypoint)
            elif node.node_type == NodeType.API:
                node.description = graph.entrypoint.description or f"Handle {graph.entrypoint.method} {graph.entrypoint.path}."
            elif node.node_type == NodeType.DATABASE:
                node.description = "Database touched by this request flow."
            elif node.node_type == NodeType.EXTERNAL_API:
                node.description = "External HTTP dependency touched by this request flow."

    def _describe_dependency_node(self, graph: FlowGraph, node: FlowNode) -> str:
        """Describe a Level 1 dependency injection node."""
        targets = [
            graph.nodes[edge.target_id]
            for edge in graph.edges
            if edge.source_id == node.id and edge.edge_type == EdgeType.DEPENDS_ON
            and edge.target_id in graph.nodes
        ]
        if targets:
            target = targets[0]
            return f"Resolve dependency `{node.name}` before the route runs. {target.description}"
        return f"Resolve dependency `{node.name}` before the route runs."

    def _describe_file_node(
        self,
        graph: FlowGraph,
        basename: str,
        child_ids: list[str],
    ) -> str:
        """Describe a file-level abstraction node."""
        child_names = self._child_summary(
            graph, child_ids, allowed_levels={3}, prefer_description=False,
        )
        if child_names:
            return f"File `{basename}` containing {child_names}."
        return f"Flow extracted from file `{basename}`."

    def _describe_layer_node(
        self,
        graph: FlowGraph,
        layer_name: str,
        file_node_ids: list[str],
    ) -> str:
        """Describe a Level 1 semantic layer node."""
        role_map = {
            "entrypoints": "Scripts and commands that start execution.",
            "routers": "Route handlers that receive HTTP requests.",
            "services": "Business logic executed after routing.",
            "repositories": "Persistence and database access operations.",
            "dependencies": "Dependency injection and request-scoped setup.",
            "middleware": "Cross-cutting request middleware.",
            "logic": "Shared helper logic.",
        }
        file_summary = self._child_summary(
            graph, file_node_ids, allowed_levels={2}, prefer_description=False,
        )
        role_text = role_map.get(layer_name, "Grouped request-flow layer.")
        if file_summary:
            return f"{role_text} Includes {file_summary}."
        return role_text

    @staticmethod
    def _child_summary(
        graph: FlowGraph,
        child_ids: list[str],
        allowed_levels: set[int],
        prefer_description: bool = False,
        limit: int = 3,
    ) -> str:
        """Summarize child nodes for file/layer descriptions."""
        candidates: list[FlowNode] = []
        priority = {
            NodeType.ROUTER: 0,
            NodeType.DEPENDENCY: 1,
            NodeType.SERVICE: 2,
            NodeType.REPOSITORY: 3,
            NodeType.FUNCTION: 4,
            NodeType.METHOD: 5,
            NodeType.EXCEPTION: 6,
        }
        for child_id in child_ids:
            child = graph.nodes.get(child_id)
            if child and child.level in allowed_levels:
                candidates.append(child)

        candidates.sort(key=lambda child: (
            priority.get(child.node_type, 99),
            child.line_start or 0,
            child.display_name,
        ))

        labels = [
            child.description if prefer_description and child.description else child.display_name
            for child in candidates
        ]
        if not labels:
            return ""

        unique_labels: list[str] = []
        for label in labels:
            if label not in unique_labels:
                unique_labels.append(label)

        summary = ", ".join(f"`{label}`" for label in unique_labels[:limit])
        remaining = len(unique_labels) - limit
        if remaining > 0:
            summary += f" and {remaining} more"
        return summary

    @staticmethod
    def _root_flow_node_id(graph: FlowGraph) -> str:
        """Return the L0 node that represents the main flow surface."""
        if "api" in graph.nodes:
            return "api"
        if "entrypoint" in graph.nodes:
            return "entrypoint"
        return "trigger"

    @staticmethod
    def _describe_trigger(entrypoint: EntryPoint) -> str:
        """Describe the execution trigger at Level 0."""
        if entrypoint.kind == "api":
            return f"Incoming HTTP request for {entrypoint.method} {entrypoint.path}."
        if entrypoint.kind == "script":
            return f"Run the script entrypoint `{entrypoint.label}`."
        if entrypoint.kind == "function":
            return f"Start tracing from `{entrypoint.handler_name}()`."
        return f"Start tracing from `{entrypoint.label or entrypoint.handler_name}`."

    @staticmethod
    def _describe_non_api_entrypoint(entrypoint: EntryPoint) -> str:
        """Describe a non-HTTP entrypoint node."""
        if entrypoint.kind == "script":
            return entrypoint.description or f"Execute script entrypoint `{entrypoint.label}`."
        if entrypoint.kind == "function":
            return entrypoint.description or f"Trace the function `{entrypoint.handler_name}()`."
        return entrypoint.description or f"Execute `{entrypoint.label or entrypoint.handler_name}`."
