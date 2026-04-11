"""Build complete FlowGraph for a given entry point.

Combines entry-point extraction with call graph analysis
to produce a multi-level flow graph.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any

from codecanvas.graph.models import (
    Confidence,
    DataFlowStep,
    EdgeType,
    EntryPoint,
    Evidence,
    FlowEdge,
    FlowGraph,
    FlowNode,
    NodeType,
)
from codecanvas.parser.call_graph import CallGraphBuilder, FunctionDef
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

        Uses a disk cache so subsequent loads of an unchanged project
        skip re-parsing entirely. Falls back to a full scan on miss.
        """
        if self._entrypoints is None:
            self._entrypoints = self._load_entrypoint_cache()
            if self._entrypoints is None:
                self._entrypoints = self.entrypoint_extractor.analyze()
                self._save_entrypoint_cache(self._entrypoints)
        return self._entrypoints

    # ------------------------------------------------------------------
    # Entrypoint disk cache
    # ------------------------------------------------------------------

    def _ep_cache_path(self) -> Path:
        return Path(self.project_root) / ".codecanvas" / "entrypoints.json"

    def _load_entrypoint_cache(self) -> list[EntryPoint] | None:
        from codecanvas.parser.call_graph import (
            _iter_project_python_files,
            _files_signature,
        )
        cache_path = self._ep_cache_path()
        if not cache_path.exists():
            return None
        try:
            with cache_path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

        if not isinstance(payload, dict) or payload.get("version") != 1:
            return None
        sig = _files_signature(
            _iter_project_python_files(Path(self.project_root)),
        )
        if payload.get("signature") != sig:
            return None
        try:
            return [_ep_from_dict(d) for d in payload["entrypoints"]]
        except (KeyError, TypeError, ValueError):
            return None

    def _save_entrypoint_cache(self, eps: list[EntryPoint]) -> None:
        from codecanvas.parser.call_graph import (
            _iter_project_python_files,
            _files_signature,
        )
        cache_path = self._ep_cache_path()
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "signature": _files_signature(
                    _iter_project_python_files(Path(self.project_root)),
                ),
                "entrypoints": [_ep_to_dict(ep) for ep in eps],
            }
            tmp = cache_path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            tmp.replace(cache_path)
        except OSError:
            pass

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
        graph = FlowGraph(entrypoint=entrypoint, _call_graph=self.call_graph)
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

        # Schema nodes (request body / response model)
        if entrypoint.kind == "api":
            self._add_schema_nodes(graph, entrypoint)

        # Build abstraction levels
        self._build_level_hierarchy(graph)
        self._rewrite_execution_pipeline(graph)
        self._connect_error_paths(graph)
        if hasattr(graph, "_layer_node_map"):
            self._lift_edges(graph, graph._layer_node_map)
        self._annotate_pipeline_phases(graph)
        self._propagate_review_signals(graph)
        self._compute_risk_scores(graph)
        self._generate_review_summary(graph)
        self._fill_missing_descriptions(graph)
        self._generate_data_flow(graph)

        return graph

    def _add_schema_nodes(self, graph: FlowGraph, entrypoint: EntryPoint) -> None:
        """Add request body and response schema nodes to the flow."""
        handler_id = self._find_handler_node_id(graph, entrypoint)

        if entrypoint.request_body:
            body_id = f"schema.request.{entrypoint.request_body}"
            if body_id not in graph.nodes:
                graph.add_node(FlowNode(
                    id=body_id,
                    node_type=NodeType.SCHEMA,
                    name=entrypoint.request_body,
                    display_name=f"Body: {entrypoint.request_body}",
                    description=f"Request body validated as `{entrypoint.request_body}` (Pydantic model).",
                    confidence=Confidence.DEFINITE,
                    level=0,
                    metadata={
                        "schema_direction": "request",
                        "schema_type": entrypoint.request_body,
                    },
                ))
            graph.add_edge(FlowEdge(
                id=self._unique_edge_id(graph, "e_schema_req"),
                source_id=body_id,
                target_id=handler_id,
                edge_type=EdgeType.CALLS,
                label=f"body: {entrypoint.request_body}",
                confidence=Confidence.DEFINITE,
                metadata={"schema_edge": True, "direction": "request"},
            ))

        response_model = entrypoint.response_model
        if response_model:
            resp_id = f"schema.response.{response_model}"
            if resp_id not in graph.nodes:
                graph.add_node(FlowNode(
                    id=resp_id,
                    node_type=NodeType.SCHEMA,
                    name=response_model,
                    display_name=f"Response: {response_model}",
                    description=f"Response serialized as `{response_model}`.",
                    confidence=Confidence.DEFINITE,
                    level=0,
                    metadata={
                        "schema_direction": "response",
                        "schema_type": response_model,
                    },
                ))
            graph.add_edge(FlowEdge(
                id=self._unique_edge_id(graph, "e_schema_resp"),
                source_id=handler_id,
                target_id=resp_id,
                edge_type=EdgeType.RETURNS,
                label=f"→ {response_model}",
                confidence=Confidence.DEFINITE,
                metadata={"schema_edge": True, "direction": "response"},
            ))

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
                self._connect_dependency_binding(
                    graph,
                    dependency_node_id=dep_id,
                    dependency_root_id=dep_root_id,
                    contract_type=effective_type,
                    from_file=dep_file,
                )

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
            ast_node = self.call_graph.get_ast_node(func_id)
            func_def = self.call_graph.get_function(func_id)
            if ast_node and func_def:
                return self.extractor.extract_depends(ast_node, func_def.file_path)
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
        func_def = self.call_graph.get_function(func_id)
        if func_def and func_def.return_annotation:
            return func_def.return_annotation
        return None

    def _connect_dependency_binding(
        self,
        graph: FlowGraph,
        *,
        dependency_node_id: str,
        dependency_root_id: str,
        contract_type: str | None,
        from_file: str | None,
    ) -> None:
        """Attach contract -> implementation binding edges for DIP-style providers."""
        if not contract_type:
            return

        provider_func = self.call_graph.get_function(dependency_root_id)
        contract_def = self.call_graph.resolve_type_definition(contract_type, from_file=from_file)
        impl_def = self.call_graph.resolve_bound_implementation(
            contract_type,
            provider_func,
            from_file=from_file,
        )
        if not contract_def or not impl_def:
            return
        if contract_def.qualified_name == impl_def.qualified_name:
            return

        dep_node = graph.nodes.get(dependency_node_id)
        if dep_node:
            dep_node.metadata["contract_type"] = contract_def.name
            dep_node.metadata["bound_implementation"] = impl_def.name
            if contract_def.is_protocol:
                dep_node.metadata["contract_kind"] = "protocol"
            elif contract_def.is_abstract:
                dep_node.metadata["contract_kind"] = "abstract"

        contract_node_id = self._ensure_function_like_node(graph, contract_def)
        impl_node_id = self._ensure_function_like_node(graph, impl_def)

        self._upsert_edge(
            graph,
            preferred_id=f"e_bind_{contract_node_id}_{impl_node_id}",
            source_id=contract_node_id,
            target_id=impl_node_id,
            edge_type=EdgeType.BINDS,
            label="bound to",
            confidence=Confidence.HIGH,
            metadata={"binding": True},
        )

        self._connect_method_bindings(graph, contract_def, impl_def, from_file=from_file)

    def _connect_method_bindings(
        self,
        graph: FlowGraph,
        contract_def: FunctionDef,
        impl_def: FunctionDef,
        *,
        from_file: str | None,
    ) -> None:
        """Bind contract methods already present in the graph to implementation methods."""
        prefix = contract_def.qualified_name + "."
        contract_method_ids = [
            node_id for node_id in graph.nodes
            if node_id.startswith(prefix)
        ]
        for contract_method_id in contract_method_ids:
            contract_method = self.call_graph.get_function(contract_method_id)
            if contract_method is None:
                continue
            impl_method = self.call_graph.resolve_method_on_type_name(
                impl_def.name,
                contract_method.name,
                from_file=from_file,
            )
            if impl_method is None:
                continue
            impl_method_id = self._ensure_function_like_node(graph, impl_method)
            self._upsert_edge(
                graph,
                preferred_id=f"e_bind_{contract_method_id}_{impl_method_id}",
                source_id=contract_method_id,
                target_id=impl_method_id,
                edge_type=EdgeType.BINDS,
                label="implemented by",
                confidence=Confidence.HIGH,
                metadata={"binding": True, "method_binding": True},
            )

    def _ensure_function_like_node(self, graph: FlowGraph, func: FunctionDef) -> str:
        """Materialize a function/class definition as an L3 node if absent."""
        if func.qualified_name in graph.nodes:
            return func.qualified_name
        graph.add_node(FlowNode(
            id=func.qualified_name,
            node_type=self.call_graph.classify_function(func),
            name=func.name,
            display_name=func.name,
            description=self.call_graph.describe_function(func),
            file_path=func.file_path,
            line_start=func.line_start,
            line_end=func.line_end,
            confidence=Confidence.DEFINITE,
            evidence=[Evidence(
                source="static_analysis",
                file_path=func.file_path,
                line_number=func.line_start,
                detail=f"Function definition at line {func.line_start}",
            )],
            level=3,
            metadata={
                "is_async": func.is_async,
                "params": func.params,
                "return_type": func.return_annotation,
                "class": func.class_name,
                "bases": func.bases,
                "is_protocol": func.is_protocol,
                "is_abstract": func.is_abstract,
            },
        ))
        return func.qualified_name

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
                file_path=mw.resolved_file_path or mw.file_path,
                line_start=mw.resolved_line or mw.line,
                confidence=Confidence.DEFINITE,
                level=1,
                metadata={
                    "middleware_registration_file": mw.file_path,
                    "middleware_registration_line": mw.line,
                },
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

        # --- Lift L3/L4 edges to L2 and L1 ---
        graph._layer_node_map = layer_node_map
        self._lift_edges(graph, layer_node_map)

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

    def _lift_edges(
        self,
        graph: FlowGraph,
        layer_node_map: dict[str, str],
    ) -> None:
        """Lift L3/L4 edges to L2 (file→file) and L2 to L1 (layer→layer)."""
        existing_l2 = {
            (e.source_id, e.target_id)
            for e in graph.edges
            if e.source_id.startswith("file.") and e.target_id.startswith("file.")
        }
        seen_l2: set[tuple[str, str]] = set(existing_l2)

        def _file_node_of(node: FlowNode) -> str | None:
            if node.level == 3:
                return node.parent_id
            if node.level == 4:
                parent = graph.nodes.get(node.parent_id) if node.parent_id else None
                if parent and parent.level == 3:
                    return parent.parent_id
                return node.parent_id
            return None

        for edge in list(graph.edges):
            # display_only edges (step_call) should not be lifted
            if edge.metadata.get("display_only"):
                continue
            src = graph.nodes.get(edge.source_id)
            tgt = graph.nodes.get(edge.target_id)
            if not src or not tgt:
                continue
            if src.level not in (3, 4) or tgt.level not in (3, 4):
                continue
            src_file = _file_node_of(src)
            tgt_file = _file_node_of(tgt)
            if src_file and tgt_file and src_file != tgt_file:
                key = (src_file, tgt_file)
                if key not in seen_l2:
                    seen_l2.add(key)
                    graph.add_edge(FlowEdge(
                        id=self._unique_edge_id(graph, f"e_l2_{src_file}_{tgt_file}"),
                        source_id=src_file,
                        target_id=tgt_file,
                        edge_type=edge.edge_type,
                        confidence=edge.confidence,
                    ))

        existing_l1 = {
            (e.source_id, e.target_id)
            for e in graph.edges
            if e.source_id.startswith("layer.") and e.target_id.startswith("layer.")
        }
        seen_l1: set[tuple[str, str]] = set(existing_l1)
        for edge in list(graph.edges):
            src = graph.nodes.get(edge.source_id)
            tgt = graph.nodes.get(edge.target_id)
            if not src or not tgt or src.level != 2 or tgt.level != 2:
                continue
            src_layer = layer_node_map.get(src.id)
            tgt_layer = layer_node_map.get(tgt.id)
            if src_layer and tgt_layer and src_layer != tgt_layer:
                key = (src_layer, tgt_layer)
                if key not in seen_l1:
                    seen_l1.add(key)
                    graph.add_edge(FlowEdge(
                        id=self._unique_edge_id(graph, f"e_l1_{src_layer}_{tgt_layer}"),
                        source_id=src_layer,
                        target_id=tgt_layer,
                        edge_type=edge.edge_type,
                        confidence=edge.confidence,
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
                # Also lift to the resolved function's file/layer if different
                if dep_resolved_id:
                    resolved_node = graph.nodes.get(dep_resolved_id)
                    if resolved_node and resolved_node.parent_id:
                        dep_file_id = resolved_node.parent_id
                        if dep_file_id != handler_file_id and dep_file_id in graph.nodes:
                            self._upsert_edge(
                                graph,
                                preferred_id=f"e_dep_l2_resolved_{dependency_id}",
                                source_id=dependency_id,
                                target_id=dep_file_id,
                                edge_type=EdgeType.DEPENDS_ON,
                                confidence=Confidence.DEFINITE,
                                metadata={
                                    "pipeline_edge": True,
                                    "pipeline_phase": "dependency",
                                    "structural_lift": True,
                                },
                            )
                            dep_file_node = graph.nodes.get(dep_file_id)
                            if dep_file_node and dep_file_node.parent_id:
                                dep_layer_id = dep_file_node.parent_id
                                if dep_layer_id != handler_layer_id and dep_layer_id in graph.nodes:
                                    self._upsert_edge(
                                        graph,
                                        preferred_id=f"e_dep_l1_resolved_{dependency_id}",
                                        source_id=dependency_id,
                                        target_id=dep_layer_id,
                                        edge_type=EdgeType.DEPENDS_ON,
                                        confidence=Confidence.DEFINITE,
                                        metadata={
                                            "pipeline_edge": True,
                                            "pipeline_phase": "dependency",
                                            "structural_lift": True,
                                        },
                                    )
            last_dep = dependency_ids[-1]
            self._add_validation_serialization_nodes(
                graph, handler_id, pipeline_source_id=last_dep,
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
        self._add_validation_serialization_nodes(
            graph, handler_id, pipeline_source_id=source_id,
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

    def _add_validation_serialization_nodes(
        self, graph: FlowGraph, handler_id: str,
        pipeline_source_id: str | None = None,
    ) -> None:
        """Add Body Validation (→422) and Response Serialization pipeline steps."""
        ep = graph.entrypoint

        # Body Validation: if the endpoint accepts a request body,
        # FastAPI validates it against the Pydantic model BEFORE the handler.
        if ep.request_body:
            validation_id = "pipeline.body_validation"
            graph.add_node(FlowNode(
                id=validation_id,
                node_type=NodeType.VALIDATION,
                name="Body Validation",
                display_name=f"Validate {ep.request_body}",
                description=(
                    f"Parse and validate the request body against `{ep.request_body}`. "
                    f"Returns HTTP 422 if validation fails."
                ),
                confidence=Confidence.DEFINITE,
                level=1,
                metadata={
                    "pipeline_phase": "validation",
                    "schema_type": ep.request_body,
                },
            ))

            # Pipeline source → validation (incoming edge)
            if pipeline_source_id:
                self._upsert_edge(
                    graph,
                    preferred_id="e_pipeline_validation",
                    source_id=pipeline_source_id,
                    target_id=validation_id,
                    edge_type=EdgeType.CALLS,
                    confidence=Confidence.DEFINITE,
                    metadata={"pipeline_edge": True, "pipeline_phase": "validation"},
                )

            # Schema → validation (incoming edge for schema node)
            body_id = f"schema.request.{ep.request_body}"
            if body_id in graph.nodes:
                root_id = self._root_flow_node_id(graph)
                self._upsert_edge(
                    graph,
                    preferred_id="e_api_schema_req",
                    source_id=root_id,
                    target_id=body_id,
                    edge_type=EdgeType.CALLS,
                    confidence=Confidence.DEFINITE,
                    metadata={"pipeline_edge": True, "schema_edge": True},
                )

            # Validation → handler
            self._upsert_edge(
                graph,
                preferred_id="e_validation_handler",
                source_id=validation_id,
                target_id=handler_id,
                edge_type=EdgeType.CALLS,
                label=f"valid {ep.request_body}",
                confidence=Confidence.DEFINITE,
                metadata={"pipeline_edge": True, "pipeline_phase": "validation"},
            )

            # Validation → 422 error
            error_422_id = "error.validation_422"
            if error_422_id not in graph.nodes:
                graph.add_node(FlowNode(
                    id=error_422_id,
                    node_type=NodeType.EXCEPTION,
                    name="ValidationError",
                    display_name="422 Validation Error",
                    description="Request body failed Pydantic validation.",
                    confidence=Confidence.DEFINITE,
                    level=4,
                    metadata={"status_code": 422},
                ))
            self._upsert_edge(
                graph,
                preferred_id="e_validation_error",
                source_id=validation_id,
                target_id=error_422_id,
                edge_type=EdgeType.RAISES,
                label="invalid body → 422",
                confidence=Confidence.DEFINITE,
                metadata={
                    "pipeline_edge": True,
                    "error_path": True,
                    "pipeline_phase": "validation",
                },
            )

        # Response Serialization: if response_model is set,
        # FastAPI validates/filters the return value.
        if ep.response_model:
            serialization_id = "pipeline.response_serialization"
            graph.add_node(FlowNode(
                id=serialization_id,
                node_type=NodeType.SERIALIZATION,
                name="Response Serialization",
                display_name=f"Serialize → {ep.response_model}",
                description=(
                    f"Validate and serialize the handler return value "
                    f"against `{ep.response_model}`."
                ),
                confidence=Confidence.DEFINITE,
                level=1,
                metadata={
                    "pipeline_phase": "serialization",
                    "schema_type": ep.response_model,
                },
            ))

            # Handler → serialization
            self._upsert_edge(
                graph,
                preferred_id="e_handler_serialize",
                source_id=handler_id,
                target_id=serialization_id,
                edge_type=EdgeType.RETURNS,
                label=f"→ {ep.response_model}",
                confidence=Confidence.DEFINITE,
                metadata={"pipeline_edge": True, "pipeline_phase": "serialization"},
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
    def _propagate_review_signals(graph: FlowGraph) -> None:
        """Propagate and enrich review signals across the graph.

        - Mark handler with 'auth' if it has Security() dependency injections.
        - Propagate callee signals to callers (shallow, 1-hop) so that
          a handler calling a repo with db_write also shows db_write.
        """
        # 1. Mark auth on handler if Security deps exist
        handler_id = None
        for node in graph.nodes.values():
            if node.metadata.get("pipeline_phase") == "handler" and node.level == 3:
                handler_id = node.id
                break
        if handler_id:
            for edge in graph.edges:
                if edge.target_id == handler_id and edge.edge_type == EdgeType.INJECTS:
                    dep_node = graph.nodes.get(edge.source_id)
                    if dep_node and dep_node.metadata.get("scopes"):
                        signals = graph.nodes[handler_id].metadata.setdefault("review_signals", [])
                        if "auth" not in signals:
                            signals.append("auth")

        # 2. Propagate callee signals to direct callers (1-hop)
        PROPAGATED = {"db_write", "db_read", "http_call"}
        for edge in graph.edges:
            if edge.edge_type not in (EdgeType.CALLS, EdgeType.QUERIES, EdgeType.REQUESTS):
                continue
            src = graph.nodes.get(edge.source_id)
            tgt = graph.nodes.get(edge.target_id)
            if not src or not tgt or src.level != 3 or tgt.level != 3:
                continue
            tgt_signals = set(tgt.metadata.get("review_signals", []))
            propagate = tgt_signals & PROPAGATED
            if propagate:
                src_signals = src.metadata.setdefault("review_signals", [])
                for sig in propagate:
                    if sig not in src_signals:
                        src_signals.append(sig)

    @staticmethod
    def _compute_risk_scores(graph: FlowGraph) -> None:
        """Compute risk scores for L3 function nodes and the endpoint.

        Score is based on review signals, error paths, call complexity,
        and pipeline phase exposure.
        """
        SIGNAL_POINTS = {
            "db_write": 3, "db_read": 1, "http_call": 3,
            "raises_5xx": 4, "raises_4xx": 2, "raises": 1,
            "auth": 2, "io": 1,
        }
        PHASE_MULTIPLIER = {
            "handler": 1.5, "repository": 1.3, "service": 1.0,
            "dependency": 0.8, "middleware": 0.7,
        }

        # Pre-compute outgoing edges per node
        outgoing_calls: dict[str, int] = {}
        error_edges_from: dict[str, int] = {}
        for edge in graph.edges:
            if edge.edge_type == EdgeType.CALLS:
                outgoing_calls[edge.source_id] = outgoing_calls.get(edge.source_id, 0) + 1
            if edge.is_error_path:
                error_edges_from[edge.source_id] = error_edges_from.get(edge.source_id, 0) + 1

        endpoint_total = 0
        endpoint_factors: list[dict] = []

        for node in graph.nodes.values():
            if node.level != 3:
                continue
            if node.node_type.value in ("class",):
                continue

            factors: list[dict] = []
            raw_score = 0

            # 1. Review signal points
            signals = node.metadata.get("review_signals", [])
            seen_raises = False
            for sig in signals:
                # Skip generic 'raises' if specific status exists
                if sig == "raises" and ("raises_4xx" in signals or "raises_5xx" in signals):
                    continue
                pts = SIGNAL_POINTS.get(sig, 0)
                if pts > 0:
                    raw_score += pts
                    factors.append({"factor": sig, "points": pts})

            # 2. Error path edges from this node
            err_count = error_edges_from.get(node.id, 0)
            if err_count > 0:
                pts = err_count
                raw_score += pts
                factors.append({"factor": "error_paths", "points": pts, "detail": f"{err_count} error edges"})

            # 3. Call complexity (callees > 3)
            call_count = outgoing_calls.get(node.id, 0)
            if call_count > 3:
                pts = call_count - 3
                raw_score += pts
                factors.append({"factor": "complexity", "points": pts, "detail": f"{call_count} callees"})

            # 4. Phase multiplier
            phase = node.metadata.get("pipeline_phase", "")
            multiplier = PHASE_MULTIPLIER.get(phase, 1.0)
            score = round(raw_score * multiplier, 1)

            if multiplier != 1.0 and raw_score > 0:
                factors.append({"factor": f"phase:{phase}", "points": 0, "detail": f"x{multiplier}"})

            if score > 0:
                # Determine risk level
                if score >= 10:
                    level = "critical"
                elif score >= 6:
                    level = "high"
                elif score >= 3:
                    level = "medium"
                else:
                    level = "low"

                node.metadata["risk_score"] = score
                node.metadata["risk_level"] = level
                node.metadata["risk_factors"] = factors
                endpoint_total += score
                endpoint_factors.append({"node": node.name, "score": score, "level": level})

        # Endpoint-level aggregate
        if endpoint_total > 0:
            if endpoint_total >= 15:
                ep_level = "critical"
            elif endpoint_total >= 8:
                ep_level = "high"
            elif endpoint_total >= 4:
                ep_level = "medium"
            else:
                ep_level = "low"
            graph.entrypoint.metadata["risk_score"] = endpoint_total
            graph.entrypoint.metadata["risk_level"] = ep_level
            graph.entrypoint.metadata["risk_breakdown"] = endpoint_factors

    @staticmethod
    def _generate_review_summary(graph: FlowGraph) -> None:
        """Generate a structured review summary for "review without reading code".

        Aggregates: risk, review signals, error paths, auth, response complexity.
        """
        SIGNAL_LABELS = {
            "db_write": "Database write operations",
            "db_read": "Database read operations",
            "http_call": "External HTTP calls",
            "raises_4xx": "Client error responses (4xx)",
            "raises_5xx": "Server error responses (5xx)",
            "raises": "Exception throwing",
            "auth": "Authentication/authorization logic",
        }

        concerns: list[dict] = []
        all_signals: set[str] = set()
        error_edge_count = 0
        total_l3 = 0
        functions_with_risk: list[dict] = []

        for node in graph.nodes.values():
            if node.level != 3 or node.node_type.value == "class":
                continue
            total_l3 += 1
            signals = node.metadata.get("review_signals", [])
            all_signals.update(signals)
            risk = node.metadata.get("risk_score", 0)
            if risk >= 3:
                functions_with_risk.append({
                    "name": node.name,
                    "score": risk,
                    "level": node.metadata.get("risk_level", "low"),
                    "phase": node.metadata.get("pipeline_phase", ""),
                })

        for edge in graph.edges:
            if edge.is_error_path:
                error_edge_count += 1

        # Build concerns list
        for sig in ["auth", "db_write", "http_call", "raises_5xx", "raises_4xx"]:
            if sig in all_signals:
                concerns.append({
                    "signal": sig,
                    "label": SIGNAL_LABELS.get(sig, sig),
                    "severity": "high" if sig in ("auth", "db_write", "raises_5xx") else "medium",
                })

        if error_edge_count > 0:
            concerns.append({
                "signal": "error_paths",
                "label": f"{error_edge_count} error path(s) in flow",
                "severity": "medium",
            })

        # Focus areas: functions with highest risk
        functions_with_risk.sort(key=lambda f: -f["score"])
        focus_areas = functions_with_risk[:5]

        summary = {
            "concerns": concerns,
            "focusAreas": focus_areas,
            "totalFunctions": total_l3,
            "errorPaths": error_edge_count,
            "signalCoverage": sorted(all_signals),
        }
        graph.entrypoint.metadata["review_summary"] = summary

        # Generate flow narrative
        narrative = FlowGraphBuilder._generate_flow_narrative(graph, concerns, error_edge_count)
        if narrative:
            graph.entrypoint.metadata["flow_narrative"] = narrative

    @staticmethod
    def _generate_flow_narrative(
        graph: FlowGraph, concerns: list[dict], error_edge_count: int,
    ) -> str:
        """Synthesize a natural language summary of the endpoint's logic."""
        ep = graph.entrypoint
        parts: list[str] = []

        # Opening: what is this endpoint
        if ep.kind == "api":
            parts.append(f"{ep.method} {ep.path}")
            if ep.description:
                parts[0] += f": {ep.description.rstrip('.')}"
        else:
            parts.append(ep.label or ep.handler_name)

        # Dependencies
        deps = [n for n in graph.nodes.values()
                if n.metadata.get("pipeline_phase") == "dependency" and n.level <= 1]
        if deps:
            dep_names = [n.name for n in deps]
            parts.append(f"Requires {', '.join(dep_names)}.")

        # Main flow: describe the handler's key steps
        handler = None
        for n in graph.nodes.values():
            if n.metadata.get("pipeline_phase") == "handler" and n.level == 3:
                handler = n
                break
        if handler and handler.description:
            parts.append(handler.description.rstrip(".") + ".")

        # Branches
        branch_nodes = [n for n in graph.nodes.values()
                        if n.node_type.value == "branch" and n.level == 4
                        and n.metadata.get("function_id") == (handler.id if handler else "")]
        if branch_nodes:
            branch_desc = [n.display_name or n.name for n in branch_nodes[:3]]
            parts.append(f"Branches on: {'; '.join(branch_desc)}.")

        # Error paths
        if error_edge_count > 0:
            error_nodes = [n for n in graph.nodes.values()
                           if n.node_type.value in ("exception", "error_response")]
            statuses = []
            for en in error_nodes:
                status = en.metadata.get("status_code")
                if status:
                    statuses.append(str(status))
            if statuses:
                parts.append(f"May return error: HTTP {', '.join(sorted(set(statuses)))}.")
            else:
                parts.append(f"Has {error_edge_count} error path(s).")

        # Response
        if ep.response_model:
            parts.append(f"Returns {ep.response_model}.")

        return " ".join(parts)

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

    # ------------------------------------------------------------------
    # Data-flow step generation
    # ------------------------------------------------------------------

    def _generate_data_flow(self, graph: FlowGraph) -> None:
        """Generate DataFlowSteps for L3 functions that have L4 logic children.

        Groups raw L4 steps into high-level data transformation steps:
        code statements → "what happens to the data".
        """
        for node in list(graph.nodes.values()):
            if node.level != 3:
                continue
            l4_nodes = [
                n for n in graph.nodes.values()
                if n.level == 4 and n.metadata.get("function_id") == node.id
            ]
            if not l4_nodes:
                continue

            l4_nodes.sort(key=lambda n: (n.line_start or 0, n.id))
            steps = self._build_data_flow_steps(graph, node, l4_nodes)
            if steps:
                # Propagate branch_id/branch_path from source L4 nodes
                for s in steps:
                    for src_id in s.source_step_ids:
                        src_node = graph.nodes.get(src_id)
                        if src_node and src_node.metadata.get("branch_id"):
                            path = src_node.metadata.get("branch_path", "")
                            s.branch_id = f'{src_node.metadata["branch_id"]}:{path}' if path else src_node.metadata["branch_id"]
                            break

                node.metadata["data_flow_steps"] = [
                    {
                        "id": s.id,
                        "label": s.label,
                        "operation": s.operation,
                        "inputs": s.inputs,
                        "output": s.output,
                        "outputType": s.output_type,
                        "errorLabel": s.error_label,
                        "branchCondition": s.branch_condition,
                        "branchId": s.branch_id,
                        "branchPaths": s.branch_paths,
                        "sourceStepIds": s.source_step_ids,
                        "calleeId": s.callee_id,
                    }
                    for s in steps
                ]

    def _build_data_flow_steps(
        self,
        graph: FlowGraph,
        func_node: FlowNode,
        l4_nodes: list[FlowNode],
    ) -> list[DataFlowStep]:
        """Convert L4 logic nodes into data-flow steps.

        Merging rules:
        - try body + except raise → one "validate" step
        - simple assignment before a meaningful step → absorbed
        - exception nodes → collected into error paths on validate steps
        - branch → one step with branch_paths
        """
        raw: list[tuple[FlowNode, str, dict]] = []  # (node, operation, extra)

        # Pre-index: which L4 steps have step_call edges to L3 callees
        step_callees: dict[str, list[str]] = {}  # l4_id -> [callee_qname]
        for edge in graph.edges:
            if edge.metadata.get("step_call") and edge.source_id in {n.id for n in l4_nodes}:
                step_callees.setdefault(edge.source_id, []).append(edge.target_id)

        # Classify each L4 node
        for n in l4_nodes:
            nt = n.node_type
            meta = n.metadata
            callees = step_callees.get(n.id, [])

            if nt == NodeType.EXCEPTION:
                status = meta.get("status_code", "")
                raw.append((n, "error", {"status": status}))

            elif nt == NodeType.BRANCH:
                condition = meta.get("condition", "")
                if meta.get("is_exception_handler"):
                    raw.append((n, "except_handler", {"condition": condition}))
                else:
                    raw.append((n, "branch", {"condition": condition}))

            elif nt == NodeType.RETURN:
                raw.append((n, "respond", {}))

            elif nt == NodeType.LOOP:
                callee_info = self._callee_info(graph, callees)
                # Fallback: use iterator_call name if no resolved callees
                if not callee_info and meta.get("iterator_call"):
                    iter_name = meta["iterator_call"].split(".")[-1]
                    callee_info = {"name": iter_name, "is_io": True}
                raw.append((n, "query" if callee_info.get("is_io") else "process", {
                    "callee": callee_info,
                }))

            elif nt in (NodeType.ASSIGNMENT, NodeType.STEP):
                target = meta.get("target", "")
                value = meta.get("value", "")

                if not callees and not target:
                    # Bare expression with no callee — side effect
                    raw.append((n, "side_effect", {}))
                elif not callees:
                    # Simple assignment (no function call) — may be absorbed
                    raw.append((n, "assign", {"target": target, "value": value}))
                else:
                    callee_info = self._callee_info(graph, callees)
                    if callee_info.get("is_io"):
                        raw.append((n, "query", {"target": target, "callee": callee_info}))
                    else:
                        raw.append((n, "transform", {"target": target, "callee": callee_info}))
            else:
                raw.append((n, "side_effect", {}))

        # Merge pass: group validate patterns, absorb simple assigns, deduplicate
        return self._merge_data_flow_steps(func_node, raw)

    def _callee_info(self, graph: FlowGraph, callee_ids: list[str]) -> dict:
        """Extract callee metadata for data-flow classification."""
        if not callee_ids:
            return {}
        # I/O hint names: functions whose name suggests data fetching/storing
        _IO_NAME_HINTS = {
            "get", "fetch", "find", "list", "query", "search", "load",
            "save", "store", "create", "update", "delete", "insert",
            "init", "send", "post", "put", "patch", "process", "invoke",
            "execute", "run", "call", "request",
        }

        def _name_suggests_io(name: str) -> bool:
            parts = name.lstrip("_").lower().split("_")
            return bool(set(parts) & _IO_NAME_HINTS)

        # Use the first non-schema callee
        for cid in callee_ids:
            callee_node = graph.nodes.get(cid)
            if not callee_node:
                callee_func = self.call_graph.get_function(cid)
                if callee_func:
                    return {
                        "name": callee_func.name,
                        "return_type": callee_func.return_annotation,
                        "is_io": _name_suggests_io(callee_func.name),
                        "id": cid,
                    }
                continue
            is_io = callee_node.node_type in (
                NodeType.REPOSITORY, NodeType.DATABASE, NodeType.EXTERNAL_API,
            ) or callee_node.metadata.get("pipeline_phase") in ("repository", "database", "external")
            if not is_io:
                is_io = _name_suggests_io(callee_node.name)
            return {
                "name": callee_node.name,
                "display_name": callee_node.display_name,
                "return_type": callee_node.metadata.get("return_type"),
                "is_io": is_io,
                "id": cid,
            }
        return {}

    def _merge_data_flow_steps(
        self,
        func_node: FlowNode,
        raw: list[tuple[FlowNode, str, dict]],
    ) -> list[DataFlowStep]:
        """Merge classified L4 steps into data-flow steps.

        - try body step + except handler → validate step
        - consecutive simple assigns before a meaningful step → absorbed
        - standalone errors → attached to preceding validate/process step
        - duplicate steps (from if/else flatten) → deduplicated
        """
        merged: list[DataFlowStep] = []
        seen_source_ids: set[str] = set()  # Track L4 node IDs to skip true duplicates (from flatten)
        pending_assigns: list[tuple[FlowNode, dict]] = []
        pending_errors: list[tuple[FlowNode, dict]] = []
        step_counter = 0

        def flush_assigns():
            """Pending simple assignments are absorbed — not emitted."""
            pending_assigns.clear()

        def make_id():
            nonlocal step_counter
            step_counter += 1
            return f"{func_node.id}.df.{step_counter}"

        def humanize_name(display_name: str) -> str:
            import re as _re
            func_match = _re.search(r'\.?(\w+)\s*\(', display_name)
            if func_match:
                return humanize_callee({"name": func_match.group(1)})
            return display_name

        def humanize_callee(callee: dict) -> str:
            name = callee.get("display_name") or callee.get("name", "")
            # CamelCase → words
            import re
            name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
            # snake_case → words, strip leading _
            name = name.lstrip("_").replace("_", " ")
            return name.strip().capitalize() if name else "Process"

        def infer_inputs(n: FlowNode) -> list[str]:
            """Extract input variable names from the step's value expression."""
            value = n.metadata.get("value", "")
            # Simple heuristic: find identifiers that look like variables
            import re
            # Match word chars that aren't part of function calls or strings
            candidates = re.findall(r'\b([a-z_][a-z0-9_]*)\b', value)
            # Filter out common noise
            noise = {"await", "self", "None", "True", "False", "not", "and", "or",
                     "in", "is", "if", "else", "for", "async", "return", "str", "int",
                     "float", "bool", "list", "dict", "set", "json", "f", "get"}
            return [c for c in candidates if c not in noise and len(c) > 1][:5]

        i = 0
        while i < len(raw):
            node, op, extra = raw[i]

            # Skip true duplicates (same L4 node from if/else flatten)
            if node.id in seen_source_ids:
                i += 1
                continue
            seen_source_ids.add(node.id)

            # --- Validate pattern: step + except_handler/error ---
            if op in ("query", "transform", "process", "side_effect"):
                j = i + 1
                error_labels = []
                except_ids = []
                while j < len(raw):
                    _, next_op, next_extra = raw[j]
                    if next_op == "except_handler":
                        except_ids.append(raw[j][0].id)
                        seen_source_ids.add(raw[j][0].id)
                        j += 1
                        continue
                    if next_op == "error":
                        error_labels.append(str(next_extra.get("status", "")))
                        except_ids.append(raw[j][0].id)
                        seen_source_ids.add(raw[j][0].id)
                        j += 1
                        continue
                    break

                if error_labels:
                    callee = extra.get("callee", {})
                    label = humanize_callee(callee) if callee else humanize_name(node.display_name)
                    error_str = "/".join(f for f in error_labels if f) or "error"
                    merged.append(DataFlowStep(
                        id=make_id(), label=label, operation="validate",
                        inputs=infer_inputs(node), output=extra.get("target"),
                        output_type=callee.get("return_type"), error_label=error_str,
                        source_step_ids=[node.id] + except_ids, callee_id=callee.get("id"),
                    ))
                    i = j
                    continue

            if op == "assign":
                i += 1
                continue
            if op == "error":
                pending_errors.append((node, extra))
                i += 1
                continue
            if op == "except_handler":
                i += 1
                continue

            if op == "branch":
                merged.append(DataFlowStep(
                    id=make_id(), label=extra.get("condition", ""),
                    operation="branch", branch_condition=extra.get("condition", ""),
                    source_step_ids=[node.id],
                ))
                i += 1
                continue

            if op == "query":
                callee = extra.get("callee", {})
                merged.append(DataFlowStep(
                    id=make_id(), label=humanize_callee(callee) if callee else node.display_name,
                    operation="query", inputs=infer_inputs(node),
                    output=extra.get("target") or None, output_type=callee.get("return_type"),
                    source_step_ids=[node.id], callee_id=callee.get("id"),
                ))
                i += 1
                continue

            if op == "transform":
                callee = extra.get("callee", {})
                merged.append(DataFlowStep(
                    id=make_id(), label=humanize_callee(callee) if callee else node.display_name,
                    operation="transform", inputs=infer_inputs(node),
                    output=extra.get("target") or None, output_type=callee.get("return_type"),
                    source_step_ids=[node.id], callee_id=callee.get("id"),
                ))
                i += 1
                continue

            if op == "respond":
                import re as _re
                value = node.metadata.get("value", "")
                match = _re.match(r"(\w+)\(", value)
                merged.append(DataFlowStep(
                    id=make_id(), label=match.group(1) if match else "response",
                    operation="respond", inputs=infer_inputs(node),
                    source_step_ids=[node.id],
                ))
                i += 1
                continue

            if op in ("side_effect", "process"):
                callee = extra.get("callee", {})
                label = humanize_callee(callee) if callee else humanize_name(node.display_name)
                if label.startswith("(yield"):
                    label = "Stream events"
                merged.append(DataFlowStep(
                    id=make_id(), label=label, operation=op,
                    source_step_ids=[node.id], callee_id=callee.get("id"),
                ))
                i += 1
                continue

            i += 1

        # Collapse consecutive "Stream events"
        collapsed: list[DataFlowStep] = []
        for step in merged:
            if (step.label == "Stream events" and collapsed
                    and collapsed[-1].label == "Stream events"):
                collapsed[-1].source_step_ids.extend(step.source_step_ids)
            else:
                collapsed.append(step)

        # Attach buffered errors to last validate step
        for err_node, err_extra in pending_errors:
            status = str(err_extra.get("status", ""))
            for step in reversed(collapsed):
                if step.operation == "validate":
                    if status and step.error_label and status not in step.error_label:
                        step.error_label += f"/{status}"
                    elif status and not step.error_label:
                        step.error_label = status
                    step.source_step_ids.append(err_node.id)
                    break

        return collapsed


# ------------------------------------------------------------------
# EntryPoint JSON helpers (for disk cache)
# ------------------------------------------------------------------

def _ep_to_dict(ep: EntryPoint) -> dict[str, Any]:
    return {
        "kind": ep.kind,
        "id": ep.id,
        "group": ep.group,
        "label": ep.label,
        "trigger": ep.trigger,
        "method": ep.method,
        "path": ep.path,
        "handler_name": ep.handler_name,
        "handler_file": ep.handler_file,
        "handler_line": ep.handler_line,
        "dependencies": list(ep.dependencies),
        "tags": list(ep.tags),
        "response_model": ep.response_model,
        "request_body": ep.request_body,
        "return_type": ep.return_type,
        "description": ep.description,
        "metadata": ep.metadata,
    }


def _ep_from_dict(d: dict[str, Any]) -> EntryPoint:
    return EntryPoint(
        kind=d.get("kind", "api"),
        id=d.get("id", ""),
        group=d.get("group", ""),
        label=d.get("label", ""),
        trigger=d.get("trigger", ""),
        method=d.get("method", ""),
        path=d.get("path", ""),
        handler_name=d.get("handler_name", ""),
        handler_file=d.get("handler_file", ""),
        handler_line=int(d.get("handler_line", 0)),
        dependencies=list(d.get("dependencies", [])),
        tags=list(d.get("tags", [])),
        response_model=d.get("response_model"),
        request_body=d.get("request_body"),
        return_type=d.get("return_type"),
        description=d.get("description", ""),
        metadata=dict(d.get("metadata", {})),
    )
