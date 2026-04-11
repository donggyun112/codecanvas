"""Build ExecutionGraph directly from AST — the oracle path.

Walks the handler function's AST statement by statement, producing
ExecutionSteps and DataLinks without going through FlowGraph.
"""
from __future__ import annotations

import ast
import re
from typing import Any

from codecanvas.graph.execution import DataLink, ExecutionGraph, ExecutionStep
from codecanvas.parser.call_graph import CallGraphBuilder, FunctionDef


class ASTExecutionBuilder:
    """Walk AST → ExecutionGraph."""

    def __init__(self, call_graph: CallGraphBuilder):
        self.cg = call_graph
        self.cg.analyze_project()
        self._step_counter = 0
        self._link_counter = 0
        self._graph = ExecutionGraph()
        self._var_producers: dict[str, list[str]] = {}  # var_name → [step_ids]
        self._call_stack: list[str] = []  # call stack for cycle detection

    def build(
        self,
        handler_name: str,
        handler_file: str,
        handler_line: int | None = None,
        max_depth: int | None = None,
        flow_graph: Any = None,
    ) -> ExecutionGraph:
        func = self.cg._find_function(handler_name, handler_file, handler_line)
        if not func:
            return self._graph

        ast_node = self.cg.get_ast_node(func.qualified_name)
        if not ast_node or not isinstance(ast_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return self._graph

        # Adaptive max_depth when not explicitly provided
        if max_depth is None:
            max_depth = self._compute_adaptive_depth(func)

        # AST-based pipeline: derive pre-handler steps from entrypoint + handler signature
        entrypoint = flow_graph.entrypoint if flow_graph else None
        prev_id = self._add_ast_pipeline_steps(ast_node, func, entrypoint, max_depth)

        handler_tail = self._walk_body(
            ast_node.body,
            func=func,
            scope=func.qualified_name,
            phase="handler",
            depth=0,
            max_depth=max_depth,
            prev_id=prev_id,
        )

        # AST-based post-handler: serialization from response_model / return annotation
        self._add_ast_post_handler_steps(handler_tail, ast_node, func, entrypoint)

        return self._graph

    # ------------------------------------------------------------------
    # Adaptive depth computation
    # ------------------------------------------------------------------

    def _compute_adaptive_depth(self, root_func: FunctionDef) -> int:
        """Compute max expansion depth based on reachable call graph size.

        Small graphs (< 10 reachable) → depth 8
        Medium graphs (10-30) → depth 5
        Large graphs (> 30) → depth 4
        """
        visited: set[str] = set()
        frontier = [root_func]
        while frontier and len(visited) < 50:  # cap exploration
            f = frontier.pop()
            if f.qualified_name in visited:
                continue
            visited.add(f.qualified_name)
            for call in f.calls:
                target = self.cg._resolve_call(call, f)
                if target and target.qualified_name not in visited:
                    frontier.append(target)
        count = len(visited)
        if count < 10:
            return 8
        if count <= 30:
            return 5
        return 4

    # ------------------------------------------------------------------
    # AST-based pipeline steps (no FlowGraph dependency)
    # ------------------------------------------------------------------

    def _add_ast_pipeline_steps(
        self,
        ast_node: ast.FunctionDef | ast.AsyncFunctionDef,
        func: FunctionDef,
        entrypoint: Any | None,
        max_depth: int | None = None,
    ) -> str | None:
        """Build pre-handler pipeline steps from AST and entrypoint metadata.

        Derives trigger, middleware, dependency injection, and validation steps
        directly from the handler's decorators, parameters, and type annotations
        instead of relying on FlowGraph nodes.
        """
        prev_id: str | None = None

        # 1. Trigger step from entrypoint metadata
        if entrypoint:
            trigger_label = entrypoint.trigger or entrypoint.label or "Request"
            step = self._emit_step(
                label=trigger_label,
                operation="pipeline",
                phase="trigger",
                file_path=func.file_path,
                line_start=func.line_start,
                confidence="definite",
                evidence=f"Entrypoint: {entrypoint.kind} {entrypoint.method} {entrypoint.path}",
            )
            prev_id = step.id

        # 2. Middleware from decorators (e.g. @require_auth, @rate_limit("100/min"))
        # Match both simple names (get, post) and dotted (router.get, app.post)
        _ROUTE_DECORATORS = {"get", "post", "put", "delete", "patch", "head", "options", "trace",
                             "api_route", "route", "websocket"}
        for dec_node in ast_node.decorator_list:
            dec_name, dec_detail = self._extract_decorator_info(dec_node)
            if not dec_name:
                continue
            # Check last segment: "router.get" → "get", "app.post" → "post"
            last_segment = dec_name.rsplit(".", 1)[-1].lower()
            if last_segment in _ROUTE_DECORATORS:
                continue
            label = f"{dec_name}({dec_detail})" if dec_detail else dec_name
            step = self._emit_step(
                label=label,
                operation="pipeline",
                phase="middleware",
                file_path=func.file_path,
                line_start=dec_node.lineno if hasattr(dec_node, 'lineno') else func.line_start,
                confidence="definite",
                evidence=f"Decorator @{label} on handler",
                metadata={"decorator_args": dec_detail} if dec_detail else None,
            )
            if prev_id:
                self._link_seq(prev_id, step.id)
            prev_id = step.id

        # 3. Dependency injection from Depends() parameters
        #    Emit a pipeline step AND inline the dependency function's body
        #    so that its internal logic (DB queries, auth checks, branches)
        #    appears in the execution flow.
        dep_names = self._collect_depends_names(ast_node)
        for dep_name, dep_line in dep_names:
            dep_func = self.cg._resolve_by_name(dep_name, func.file_path)
            step = self._emit_step(
                label=f"Depends({dep_name})",
                operation="pipeline",
                phase="dependency",
                file_path=func.file_path,
                line_start=dep_line or func.line_start,
                callee_function=dep_func.qualified_name if dep_func else None,
                confidence="definite",
                evidence=f"FastAPI Depends() parameter",
            )
            if prev_id:
                self._link_seq(prev_id, step.id)
            prev_id = step.id

            # Inline the dependency's body so its logic is visible
            if dep_func and max_depth is not None:
                tail = self._inline_callee(
                    dep_func, step.id,
                    parent_scope=dep_func.qualified_name,
                    depth=1,
                    max_depth=max_depth,
                )
                if tail:
                    prev_id = tail

        # 4. Request body validation from type-annotated params (Pydantic models)
        request_body = entrypoint.request_body if entrypoint else None
        if request_body:
            step = self._emit_step(
                label=f"Validate {request_body}",
                operation="pipeline",
                phase="validation",
                file_path=func.file_path,
                line_start=func.line_start,
                confidence="definite",
                evidence=f"Request body type: {request_body}",
            )
            if prev_id:
                self._link_seq(prev_id, step.id)
            prev_id = step.id

        return prev_id

    def _add_ast_post_handler_steps(
        self,
        prev_id: str | None,
        ast_node: ast.FunctionDef | ast.AsyncFunctionDef,
        func: FunctionDef,
        entrypoint: Any | None,
    ) -> None:
        """Add post-handler steps derived from return annotation / response_model."""
        response_model = entrypoint.response_model if entrypoint else None
        return_ann = func.return_annotation

        model_name = response_model or return_ann
        if model_name and model_name not in ("None", "str", "dict", "list", "int", "float", "bool", "Any"):
            step = self._emit_step(
                label=f"Serialize → {model_name}",
                operation="pipeline",
                phase="serialization",
                file_path=func.file_path,
                line_start=ast_node.end_lineno or func.line_end,
                confidence="definite",
                evidence=f"Response model: {model_name}",
            )
            if prev_id:
                self._link_seq(prev_id, step.id)

    @staticmethod
    def _extract_decorator_info(node: ast.expr) -> tuple[str, str]:
        """Extract decorator name and argument summary.

        Returns (name, detail) where detail is the argument string for
        parameterized decorators like @limiter.limit("100/min").
        """
        if isinstance(node, ast.Name):
            return node.id, ""
        if isinstance(node, ast.Attribute):
            base = ast.unparse(node) if hasattr(ast, "unparse") else node.attr
            return base, ""
        if isinstance(node, ast.Call):
            # @decorator(args...)
            func_node = node.func
            if isinstance(func_node, ast.Name):
                name = func_node.id
            elif isinstance(func_node, ast.Attribute):
                name = ast.unparse(func_node) if hasattr(ast, "unparse") else func_node.attr
            else:
                return "", ""
            # Extract argument summary
            args_strs = []
            for arg in node.args[:3]:  # limit to first 3 args
                if hasattr(ast, "unparse"):
                    s = ast.unparse(arg)
                    args_strs.append(s[:40] if len(s) > 40 else s)
            for kw in node.keywords[:3]:
                if kw.arg and hasattr(ast, "unparse"):
                    s = ast.unparse(kw.value)
                    args_strs.append(f"{kw.arg}={s[:30]}")
            detail = ", ".join(args_strs)
            return name, detail
        return "", ""

    def _collect_depends_names(
        self,
        ast_node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> list[tuple[str, int | None]]:
        """Extract all Depends() function names from handler parameters.

        Returns (dep_name, line_number) tuples — combines both type-annotation
        based Depends and default-value based Depends, deduplicated.
        """
        result: list[tuple[str, int | None]] = []
        seen: set[str] = set()

        for arg in ast_node.args.args + ast_node.args.kwonlyargs:
            if not arg.annotation:
                continue
            name = self._extract_depends_name(arg.annotation)
            if name and name not in seen:
                seen.add(name)
                result.append((name, arg.lineno if hasattr(arg, 'lineno') else None))

        defaults = list(ast_node.args.defaults) + list(ast_node.args.kw_defaults)
        for default in defaults:
            if default is None:
                continue
            name = self._extract_depends_name(default)
            if name and name not in seen:
                seen.add(name)
                result.append((name, default.lineno if hasattr(default, 'lineno') else None))

        return result

    @staticmethod
    def _extract_depends_name(node: ast.expr) -> str | None:
        """Extract function name from Depends(func) AST node.

        Supports:
          - Depends(some_func)
          - Annotated[SomeType, Depends(some_func)]
          - Annotated[SomeType, Depends(some_func), ...]
        """
        # Direct Depends() call
        if isinstance(node, ast.Call):
            func_node = node.func
            func_name = None
            if isinstance(func_node, ast.Name):
                func_name = func_node.id
            elif isinstance(func_node, ast.Attribute):
                func_name = func_node.attr
            if func_name == "Depends" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Name):
                    return arg.id
                if isinstance(arg, ast.Attribute):
                    return arg.attr
                if hasattr(ast, "unparse"):
                    return ast.unparse(arg)[:40]

        # Annotated[Type, Depends(func)] — Subscript with Annotated
        if isinstance(node, ast.Subscript):
            # Check if it's Annotated[...]
            slice_val = node.slice
            annotated_name = None
            if isinstance(node.value, ast.Name):
                annotated_name = node.value.id
            elif isinstance(node.value, ast.Attribute):
                annotated_name = node.value.attr
            if annotated_name == "Annotated" and isinstance(slice_val, ast.Tuple):
                # Annotated[Type, Depends(func), ...] — search metadata args for Depends
                for elt in slice_val.elts[1:]:  # skip first (the actual type)
                    dep = ASTExecutionBuilder._extract_depends_name(elt)
                    if dep:
                        return dep

        return None

    # ------------------------------------------------------------------
    # Core AST walker
    # ------------------------------------------------------------------

    def _walk_body(
        self,
        stmts: list[ast.stmt],
        func: FunctionDef,
        scope: str,
        phase: str,
        depth: int,
        max_depth: int,
        branch_id: str | None = None,
        prev_id: str | None = None,
        first_link_kind: str | None = None,
    ) -> str | None:
        """Walk a list of statements, return the last step ID."""
        for stmt in stmts:
            # Override link kind for the first statement (e.g. branch fork)
            if first_link_kind:
                self._next_link_kind = first_link_kind
                first_link_kind = None
            prev_id = self._walk_stmt(
                stmt, func=func, scope=scope, phase=phase,
                depth=depth, max_depth=max_depth,
                branch_id=branch_id, prev_id=prev_id,
            )
        return prev_id

    def _walk_stmt(
        self,
        stmt: ast.stmt,
        func: FunctionDef,
        scope: str,
        phase: str,
        depth: int,
        max_depth: int,
        branch_id: str | None = None,
        prev_id: str | None = None,
    ) -> str | None:
        """Process one statement, return the step ID."""

        # --- Assignment ---
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            return self._handle_assign(stmt, func, scope, phase, depth, max_depth, branch_id, prev_id)

        # --- If/Else ---
        if isinstance(stmt, ast.If):
            return self._handle_if(stmt, func, scope, phase, depth, max_depth, branch_id, prev_id)

        # --- Try/Except ---
        _try_types = (ast.Try,) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())
        if isinstance(stmt, _try_types):
            return self._handle_try(stmt, func, scope, phase, depth, max_depth, branch_id, prev_id)

        # --- Return ---
        if isinstance(stmt, ast.Return):
            return self._handle_return(stmt, func, scope, phase, depth, branch_id, prev_id)

        # --- For / Async For ---
        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            return self._handle_for(stmt, func, scope, phase, depth, max_depth, branch_id, prev_id)

        # --- While ---
        if isinstance(stmt, ast.While):
            return self._handle_while(stmt, func, scope, phase, depth, max_depth, branch_id, prev_id)

        # --- With / Async With ---
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            return self._handle_with(stmt, func, scope, phase, depth, max_depth, branch_id, prev_id)

        # --- Match/Case (Python 3.10+) ---
        if hasattr(ast, "Match") and isinstance(stmt, ast.Match):
            return self._handle_match(stmt, func, scope, phase, depth, max_depth, branch_id, prev_id)

        # --- Assert (type narrowing) ---
        if isinstance(stmt, ast.Assert):
            return self._handle_assert(stmt, func, prev_id)

        # --- Nested function def ---
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Do NOT walk body — nested function is a separate scope.
            # It only executes when called, not when defined.
            return prev_id

        # --- Expression statement ---
        if isinstance(stmt, ast.Expr):
            if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                return prev_id  # docstring
            if isinstance(stmt.value, (ast.Yield, ast.YieldFrom)):
                return self._handle_yield(stmt, func, scope, phase, depth, branch_id, prev_id)
            return self._handle_expr(stmt, func, scope, phase, depth, max_depth, branch_id, prev_id)

        # --- Raise ---
        if isinstance(stmt, ast.Raise):
            return self._handle_raise(stmt, func, scope, phase, depth, branch_id, prev_id)

        # --- AugAssign: x += func() ---
        if isinstance(stmt, ast.AugAssign):
            return self._handle_augassign(stmt, func, scope, phase, depth, max_depth, branch_id, prev_id)

        return prev_id

    # ------------------------------------------------------------------
    # Statement handlers
    # ------------------------------------------------------------------

    def _handle_assign(self, stmt, func, scope, phase, depth, max_depth, branch_id, prev_id):
        if isinstance(stmt, ast.Assign):
            targets = [ast.unparse(t) for t in stmt.targets] if hasattr(ast, "unparse") else ["var"]
            target = ", ".join(targets)
            value_node = stmt.value
        else:  # AnnAssign
            target = ast.unparse(stmt.target) if hasattr(ast, "unparse") else "var"
            value_node = stmt.value

        inputs = self._extract_names(value_node) if value_node else []
        callee = self._resolve_call_in_expr(value_node, func) if value_node else None

        if not callee:
            # No function call — silent variable tracking, no visible step.
            if prev_id:
                self._var_producers.setdefault(target, []).append(prev_id)
            return prev_id

        # Nested function call: walk its body transparently (no step for the call itself)
        is_nested = callee.qualified_name.startswith(func.qualified_name + ".")
        if is_nested:
            ast_node = self.cg.get_ast_node(callee.qualified_name)
            if ast_node and isinstance(ast_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                tail = self._walk_body(
                    ast_node.body, func=callee, scope=callee.qualified_name,
                    phase=phase, depth=depth, max_depth=max_depth,
                    branch_id=branch_id, prev_id=prev_id,
                )
                return tail
            return prev_id

        is_io = self._is_io_func(callee)
        op = "query" if is_io else "transform"
        label = self._humanize(callee.name)
        step_meta = self._callee_review_signals(callee, is_io)

        # Propagate DB query detail if the callee has DB call sites
        if is_io and callee.calls:
            for cs in callee.calls:
                if cs.db_detail:
                    step_meta["db_query"] = cs.db_detail
                    break

        conf = self._callee_confidence(callee)
        step = self._emit_step(
            label=label, operation=op, phase=phase, scope=scope, depth=depth,
            inputs=inputs, output=target, output_type=callee.return_annotation if callee else None,
            callee_function=callee.qualified_name,
            branch_id=branch_id,
            file_path=func.file_path, line_start=stmt.lineno,
            confidence=conf,
            evidence=f"Static call to {callee.name} at line {stmt.lineno}",
            metadata=step_meta,
        )
        self._link_seq(prev_id, step.id)
        self._link_data(inputs, step.id, branch_id)
        self._var_producers[target] = [step.id]

        # Recurse into callee
        if depth < max_depth:
            tail = self._inline_callee(callee, step.id, scope, depth + 1, max_depth)
            return tail or step.id
        return step.id

    def _handle_if(self, stmt, func, scope, phase, depth, max_depth, branch_id, prev_id):
        condition = ast.unparse(stmt.test) if hasattr(ast, "unparse") else "condition"
        bid = f"br_{stmt.lineno}"
        saved_vars = {k: list(v) for k, v in self._var_producers.items()}

        # Type guard narrowing: isinstance() + "is not None" within if-body
        type_guard = self._extract_isinstance_guard(stmt.test)
        none_guard = self._extract_none_guard(stmt.test, func) if not type_guard else None
        has_narrowing = type_guard or none_guard
        saved_local_types = dict(func.local_types) if has_narrowing else None
        if type_guard:
            var_name, type_name = type_guard
            func.local_types[var_name] = type_name
        elif none_guard:
            var_name, type_name = none_guard
            func.local_types[var_name] = type_name

        # Collect body steps into temporary buffers to check if meaningful
        saved_steps = list(self._graph.steps)
        saved_links = list(self._graph.links)

        # Walk if body (with narrowed type if isinstance guard)
        self._graph.steps = []
        self._graph.links = []
        self._var_producers = {k: list(v) for k, v in saved_vars.items()}
        if_tail = self._walk_body(
            stmt.body, func=func, scope=scope, phase=phase,
            depth=depth, max_depth=max_depth,
            branch_id=f"{bid}:if", prev_id=None,
        )
        if_steps = list(self._graph.steps)
        if_links = list(self._graph.links)
        if_vars = dict(self._var_producers)

        # Restore original types before walking else
        if saved_local_types is not None:
            func.local_types = saved_local_types

        # Walk else body
        self._graph.steps = []
        self._graph.links = []
        self._var_producers = {k: list(v) for k, v in saved_vars.items()}
        else_tail = None
        if stmt.orelse:
            else_tail = self._walk_body(
                stmt.orelse, func=func, scope=scope, phase=phase,
                depth=depth, max_depth=max_depth,
                branch_id=f"{bid}:else", prev_id=None,
            )
        else_steps = list(self._graph.steps)
        else_links = list(self._graph.links)

        # Restore main graph
        self._graph.steps = saved_steps
        self._graph.links = saved_links

        # Restore original types after BOTH branches (narrowing must not leak)
        if saved_local_types is not None:
            func.local_types = dict(saved_local_types)

        # Merge vars (combine producer lists from both branches)
        else_vars = dict(self._var_producers)
        self._var_producers = {}
        for d in [saved_vars, if_vars, else_vars]:
            for var, producers in d.items():
                existing = self._var_producers.get(var, [])
                for p in producers:
                    if p not in existing:
                        existing.append(p)
                self._var_producers[var] = existing

        # If no meaningful steps — skip branch
        if not if_steps and not else_steps:
            return prev_id

        # Compute human-readable branch explanation
        branch_explanation = ""
        try:
            from codecanvas.graph.cfg import CFGBuilder
            branch_explanation = CFGBuilder._explain_branch(
                stmt.test, stmt.body, stmt.orelse,
            )
        except Exception:
            branch_explanation = ""  # Branch explanation failed — condition text used as fallback

        # Emit branch step FIRST (correct order)
        step_meta: dict[str, Any] = {}
        if branch_explanation:
            step_meta["branch_explanation"] = branch_explanation
        branch_step = self._emit_step(
            label=self._compact(condition), operation="branch", phase=phase,
            scope=scope, depth=depth, branch_condition=condition,
            file_path=func.file_path, line_start=stmt.lineno,
            line_end=getattr(stmt, 'end_lineno', None),
            metadata=step_meta if step_meta else None,
        )
        self._link_seq(prev_id, branch_step.id)

        # Append if-body steps and link from branch
        if if_steps:
            self._graph.links.append(DataLink(
                id=self._next_link_id(), source_step_id=branch_step.id,
                target_step_id=if_steps[0].id, kind="branch",
            ))
            self._graph.steps.extend(if_steps)
            self._graph.links.extend(if_links)

        # Append else-body steps and link from branch
        if else_steps:
            self._graph.links.append(DataLink(
                id=self._next_link_id(), source_step_id=branch_step.id,
                target_step_id=else_steps[0].id, kind="branch",
            ))
            self._graph.steps.extend(else_steps)
            self._graph.links.extend(else_links)

        # Determine continuation
        if_returns = self._ends_with_return(stmt.body)
        else_returns = self._ends_with_return(stmt.orelse) if stmt.orelse else False

        if if_returns and else_returns:
            return None
        elif if_returns:
            return else_tail or branch_step.id
        elif else_returns:
            return if_tail or branch_step.id
        else:
            tails = [t for t in [if_tail, else_tail] if t and t != branch_step.id]
            if not tails:
                return branch_step.id
            self._pending_merge = tails
            return None

    def _handle_try(self, stmt, func, scope, phase, depth, max_depth, branch_id, prev_id):
        # Try body is transparent
        tail = self._walk_body(
            stmt.body, func=func, scope=scope, phase=phase,
            depth=depth, max_depth=max_depth,
            branch_id=branch_id, prev_id=prev_id,
        )
        # Except handlers as validate/error
        for handler in stmt.handlers:
            exc = ast.unparse(handler.type) if handler.type and hasattr(ast, "unparse") else "Exception"
            # Check if handler body is just a raise
            is_reraise = (
                len(handler.body) == 1
                and isinstance(handler.body[0], ast.Raise)
                and handler.body[0].exc
            )
            if is_reraise:
                raise_node = handler.body[0].exc
                status = self._extract_status(raise_node) if isinstance(raise_node, ast.Call) else None
                label = f"Error {status}" if status else f"Error ({exc})"
                err_step = self._emit_step(
                    label=label, operation="error", phase=phase, scope=scope, depth=depth,
                    error_label=str(status) if status else exc,
                    file_path=func.file_path, line_start=handler.lineno,
                    confidence="definite", evidence=f"except {exc} handler",
                )
                self._link_seq(tail, err_step.id, kind="error")
                self._graph.links.append(DataLink(
                    id=self._next_link_id(),
                    source_step_id=tail or prev_id or "",
                    target_step_id=err_step.id,
                    kind="error",
                    label=f"except {exc}",
                    is_error_path=True,
                ))
        return tail

    def _handle_yield(self, stmt, func, scope, phase, depth, branch_id, prev_id):
        """Emit a step for yield/yield from expressions instead of skipping them."""
        yield_node = stmt.value
        if isinstance(yield_node, ast.Yield):
            value_str = ast.unparse(yield_node.value) if yield_node.value and hasattr(ast, "unparse") else ""
            label = f"yield {self._compact(value_str, 30)}" if value_str else "yield"
            inputs = self._extract_names(yield_node.value) if yield_node.value else []
        else:  # YieldFrom
            value_str = ast.unparse(yield_node.value) if yield_node.value and hasattr(ast, "unparse") else ""
            label = f"yield from {self._compact(value_str, 25)}" if value_str else "yield from"
            inputs = self._extract_names(yield_node.value) if yield_node.value else []

        step = self._emit_step(
            label=label,
            operation="respond",
            phase=phase,
            scope=scope,
            depth=depth,
            inputs=inputs,
            branch_id=branch_id,
            file_path=func.file_path,
            line_start=stmt.lineno,
            confidence="inferred",
            evidence="Generator yield — execution order depends on consumer",
            metadata={"is_generator_yield": True},
        )
        self._link_seq(prev_id, step.id)
        self._link_data(inputs, step.id, branch_id)
        return step.id

    def _handle_return(self, stmt, func, scope, phase, depth, branch_id, prev_id):
        value_str = ast.unparse(stmt.value) if stmt.value and hasattr(ast, "unparse") else ""

        # Check if return value contains a nested function call — inline its body
        if isinstance(stmt.value, ast.Call):
            for arg in [stmt.value] + list(stmt.value.args):
                if isinstance(arg, ast.Call):
                    inner_callee = self._resolve_call_in_expr(arg, func)
                    if inner_callee and inner_callee.qualified_name.startswith(func.qualified_name + "."):
                        inner_ast = self.cg.get_ast_node(inner_callee.qualified_name)
                        if inner_ast and isinstance(inner_ast, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            prev_id = self._walk_body(
                                inner_ast.body, func=inner_callee, scope=inner_callee.qualified_name,
                                phase=phase, depth=depth, max_depth=0,  # don't recurse deeper from inline
                                branch_id=branch_id, prev_id=prev_id,
                            )

        # Extract response type from constructor call
        resp_type = "response"
        if isinstance(stmt.value, ast.Call):
            func_name = self._get_call_name(stmt.value)
            if func_name:
                resp_type = func_name
        elif value_str:
            match = re.match(r"(\w+)\(", value_str)
            if match:
                resp_type = match.group(1)

        inputs = self._extract_names(stmt.value) if stmt.value else []

        # Collect transitive response origin chain
        origins = self._collect_response_origins(inputs)

        step = self._emit_step(
            label=resp_type, operation="respond", phase=phase, scope=scope, depth=depth,
            inputs=inputs, branch_id=branch_id,
            file_path=func.file_path, line_start=stmt.lineno,
            metadata={
                "return_expression": value_str[:120] if value_str else "",
                **({"response_origins": origins} if origins else {}),
            },
        )
        self._link_seq(prev_id, step.id)
        self._link_data(inputs, step.id, branch_id)
        return step.id

    def _handle_for(self, stmt, func, scope, phase, depth, max_depth, branch_id, prev_id):
        target = ast.unparse(stmt.target) if hasattr(ast, "unparse") else "item"
        iter_str = ast.unparse(stmt.iter) if hasattr(ast, "unparse") else "iterator"
        callee = self._resolve_call_in_expr(stmt.iter, func)
        inputs = self._extract_names(stmt.iter)

        # Propagate element type: `for item in items` where items: list[User] → item: User
        if isinstance(stmt.target, ast.Name):
            iter_var = None
            if isinstance(stmt.iter, ast.Name):
                iter_var = stmt.iter.id
            elif isinstance(stmt.iter, ast.Await) and isinstance(stmt.iter.value, ast.Name):
                iter_var = stmt.iter.value.id
            if iter_var:
                iter_type = func.local_types.get(iter_var)
                if iter_type:
                    elem_type = CallGraphBuilder._extract_element_type(iter_type)
                    if elem_type:
                        from codecanvas.parser.call_graph import CallGraphBuilder as _CG
                        normalized = _CG._normalize_type_name(elem_type)
                        if normalized and normalized[0].isupper():
                            func.local_types[stmt.target.id] = normalized

        label = self._humanize(callee.name) if callee else self._compact(f"for {target} in {iter_str}")
        op = "query" if callee and self._is_io_func(callee) else "process"

        conf = self._callee_confidence(callee) if callee else "definite"
        step = self._emit_step(
            label=label, operation=op, phase=phase, scope=scope, depth=depth,
            inputs=inputs, output=target,
            callee_function=callee.qualified_name if callee else None,
            branch_id=branch_id,
            file_path=func.file_path, line_start=stmt.lineno,
            line_end=getattr(stmt, 'end_lineno', None),
            confidence=conf,
        )
        self._link_seq(prev_id, step.id)
        self._link_data(inputs, step.id, branch_id)

        # Walk loop body only if we have depth budget
        if depth < max_depth:
            self._walk_body(
                stmt.body, func=func, scope=scope, phase=phase,
                depth=depth, max_depth=max_depth,
            branch_id=branch_id, prev_id=step.id,
        )

        if callee and depth < max_depth:
            tail = self._inline_callee(callee, step.id, scope, depth + 1, max_depth)
            last_id = tail or step.id
        else:
            last_id = step.id

        # for-else: walk the else clause (executes when loop completes normally)
        if stmt.orelse:
            else_tail = self._walk_body(
                stmt.orelse, func=func, scope=scope, phase=phase,
                depth=depth, max_depth=max_depth,
                branch_id=branch_id, prev_id=last_id,
            )
            return else_tail or last_id
        return last_id

    def _handle_expr(self, stmt, func, scope, phase, depth, max_depth, branch_id, prev_id):
        callee = self._resolve_call_in_expr(stmt.value, func)

        if not callee:
            # Emit inferred step for unresolved calls that look like side effects
            # (method calls, awaited calls) instead of silently skipping them.
            return self._handle_unresolved_expr(stmt, func, scope, phase, depth, branch_id, prev_id)

        inputs = self._extract_names(stmt.value)
        label = self._humanize(callee.name)
        is_io = self._is_io_func(callee)
        op = "query" if is_io else "side_effect"
        step_meta = self._callee_review_signals(callee, is_io)

        conf = self._callee_confidence(callee)
        step = self._emit_step(
            label=label, operation=op, phase=phase, scope=scope, depth=depth,
            inputs=inputs,
            callee_function=callee.qualified_name,
            branch_id=branch_id,
            file_path=func.file_path, line_start=stmt.lineno,
            confidence=conf,
            evidence=f"Expression call to {callee.name} at line {stmt.lineno}",
            metadata=step_meta,
        )
        self._link_seq(prev_id, step.id)
        return step.id

    def _handle_unresolved_expr(self, stmt, func, scope, phase, depth, branch_id, prev_id):
        """Emit an inferred-confidence step for unresolved bare expressions.

        Covers side-effect calls like logger.info(), cache.invalidate(),
        event_bus.emit(), etc. that can't be statically resolved but are
        semantically meaningful.
        """
        expr = stmt.value
        # Unwrap await
        if isinstance(expr, ast.Await):
            expr = expr.value

        if not isinstance(expr, ast.Call):
            return prev_id  # Non-call expressions (e.g. bare names) — truly skip

        # Extract a human-readable label from the call
        label = None
        if isinstance(expr.func, ast.Attribute):
            # e.g. logger.info(...), self.notify(...), cache.delete(...)
            attr = expr.func.attr
            # Skip truly low-signal methods
            _SKIP_METHODS = {
                "append", "extend", "insert", "pop", "remove", "clear",
                "copy", "sort", "reverse", "items", "keys", "values",
                "get", "setdefault", "split", "strip", "join",
                "lower", "upper", "startswith", "endswith", "replace",
                "format", "encode", "decode",
            }
            if attr in _SKIP_METHODS:
                return prev_id
            owner = self._get_call_name_full(expr.func.value)
            label = f"{owner}.{attr}" if owner else attr
        elif isinstance(expr.func, ast.Name):
            label = expr.func.id
        else:
            return prev_id

        inputs = self._extract_names(expr)

        step = self._emit_step(
            label=self._humanize(label.split(".")[-1]),
            operation="side_effect",
            phase=phase,
            scope=scope,
            depth=depth,
            inputs=inputs,
            branch_id=branch_id,
            file_path=func.file_path,
            line_start=stmt.lineno,
            confidence="inferred",
            evidence=f"Unresolved call: {label}",
            metadata={"unresolved_call": label},
        )
        self._link_seq(prev_id, step.id)
        return step.id

    @staticmethod
    def _get_call_name_full(node: ast.expr) -> str | None:
        """Get a readable name for the receiver of a method call."""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = ASTExecutionBuilder._get_call_name_full(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        return None

    def _handle_raise(self, stmt, func, scope, phase, depth, branch_id, prev_id):
        if isinstance(stmt.exc, ast.Call):
            func_name = self._get_call_name(stmt.exc)
            status = self._extract_status(stmt.exc)
            label = f"Error {status}" if status else f"raise {func_name or 'Exception'}"
        else:
            func_name = None
            status = None
            label = "raise"

        signals = ["raises"]
        if status and 400 <= status < 500:
            signals.append("raises_4xx")
        elif status and status >= 500:
            signals.append("raises_5xx")

        step = self._emit_step(
            label=label, operation="error", phase=phase, scope=scope, depth=depth,
            error_label=str(status) if status else None,
            branch_id=branch_id,
            file_path=func.file_path, line_start=stmt.lineno,
            metadata={"review_signals": signals},
        )
        self._link_seq(prev_id, step.id)
        return step.id

    # ------------------------------------------------------------------
    # With / Assert / AugAssign handlers
    # ------------------------------------------------------------------

    def _handle_with(self, stmt, func, scope, phase, depth, max_depth, branch_id, prev_id):
        """Handle with/async with — track context manager call and walk body."""
        for item in stmt.items:
            # Resolve the context manager expression (e.g. session.begin())
            callee = self._resolve_call_in_expr(item.context_expr, func)
            if callee:
                conf = self._callee_confidence(callee)
                label = self._humanize(callee.name)
                step = self._emit_step(
                    label=label, operation="transform", phase=phase, scope=scope, depth=depth,
                    callee_function=callee.qualified_name,
                    branch_id=branch_id,
                    file_path=func.file_path, line_start=stmt.lineno,
                    confidence=conf,
                    evidence=f"Context manager: {callee.name}",
                )
                self._link_seq(prev_id, step.id)
                prev_id = step.id

                # Track `as target` type from __enter__ return / callee return annotation
                if item.optional_vars and isinstance(item.optional_vars, ast.Name):
                    target_name = item.optional_vars.id
                    ret_type = callee.return_annotation
                    if ret_type:
                        from codecanvas.parser.call_graph import CallGraphBuilder
                        normalized = CallGraphBuilder._normalize_type_name(ret_type)
                        if normalized and normalized[0].isupper():
                            func.local_types[target_name] = normalized
                    self._var_producers[target_name] = [step.id]

        # Walk with body
        return self._walk_body(
            stmt.body, func=func, scope=scope, phase=phase,
            depth=depth, max_depth=max_depth,
            branch_id=branch_id, prev_id=prev_id,
        )

    def _handle_assert(self, stmt, func, prev_id):
        """Handle assert isinstance(x, Type) — narrow type for subsequent statements."""
        if isinstance(stmt.test, ast.Call):
            guard = self._extract_isinstance_guard(stmt.test)
            if guard:
                var_name, type_name = guard
                func.local_types[var_name] = type_name
        return prev_id

    def _handle_augassign(self, stmt, func, scope, phase, depth, max_depth, branch_id, prev_id):
        """Handle augmented assignment: x += func() — track the call."""
        callee = self._resolve_call_in_expr(stmt.value, func)
        if not callee:
            return prev_id
        target = ast.unparse(stmt.target) if hasattr(ast, "unparse") else "var"
        conf = self._callee_confidence(callee)
        step = self._emit_step(
            label=self._humanize(callee.name), operation="transform",
            phase=phase, scope=scope, depth=depth,
            inputs=self._extract_names(stmt.value),
            output=target,
            callee_function=callee.qualified_name,
            branch_id=branch_id,
            file_path=func.file_path, line_start=stmt.lineno,
            confidence=conf,
        )
        self._link_seq(prev_id, step.id)
        return step.id

    def _handle_while(self, stmt, func, scope, phase, depth, max_depth, branch_id, prev_id):
        """Handle while loops — walk body, then else clause."""
        condition = ast.unparse(stmt.test) if hasattr(ast, "unparse") else "condition"
        callee = self._resolve_call_in_expr(stmt.test, func)

        label = self._humanize(callee.name) if callee else f"while {self._compact(condition, 30)}"
        step = self._emit_step(
            label=label, operation="branch", phase=phase, scope=scope, depth=depth,
            branch_condition=condition,
            branch_id=branch_id,
            file_path=func.file_path, line_start=stmt.lineno,
        )
        self._link_seq(prev_id, step.id)

        # Walk body
        if depth < max_depth:
            self._walk_body(
                stmt.body, func=func, scope=scope, phase=phase,
                depth=depth, max_depth=max_depth,
                branch_id=branch_id, prev_id=step.id,
            )

        # while-else
        if stmt.orelse:
            else_tail = self._walk_body(
                stmt.orelse, func=func, scope=scope, phase=phase,
                depth=depth, max_depth=max_depth,
                branch_id=branch_id, prev_id=step.id,
            )
            return else_tail or step.id
        return step.id

    def _handle_match(self, stmt, func, scope, phase, depth, max_depth, branch_id, prev_id):
        """Handle match/case (Python 3.10+) — walk each case body."""
        subject = ast.unparse(stmt.subject) if hasattr(ast, "unparse") else "subject"
        branch_step = self._emit_step(
            label=f"match {self._compact(subject, 30)}",
            operation="branch", phase=phase, scope=scope, depth=depth,
            branch_condition=subject,
            file_path=func.file_path, line_start=stmt.lineno,
        )
        self._link_seq(prev_id, branch_step.id)

        tails: list[str] = []
        for case in stmt.cases:
            tail = self._walk_body(
                case.body, func=func, scope=scope, phase=phase,
                depth=depth, max_depth=max_depth,
                branch_id=f"match_{stmt.lineno}",
                prev_id=branch_step.id,
            )
            if tail:
                tails.append(tail)

        if tails:
            self._pending_merge = tails
            return None
        return branch_step.id

    # ------------------------------------------------------------------
    # Callee inlining
    # ------------------------------------------------------------------

    def _inline_callee(self, callee: FunctionDef, parent_step_id: str, parent_scope: str, depth: int, max_depth: int) -> str | None:
        if depth > max_depth:
            return None
        # Call-stack cycle detection: if callee is already on the stack, it's recursive
        if callee.qualified_name in self._call_stack:
            return None
        self._call_stack.append(callee.qualified_name)

        ast_node = self.cg.get_ast_node(callee.qualified_name)
        if not ast_node or not isinstance(ast_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._call_stack.pop()
            return None

        result = self._walk_body(
            ast_node.body,
            func=callee,
            scope=callee.qualified_name,
            phase="callee",
            depth=depth,
            max_depth=max_depth,
            prev_id=parent_step_id,
        )
        self._call_stack.pop()
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _emit_step(self, **kwargs) -> ExecutionStep:
        self._step_counter += 1
        if "source_node_ids" not in kwargs or not kwargs["source_node_ids"]:
            callee = kwargs.get("callee_function", "")
            kwargs["source_node_ids"] = [callee] if callee else []
        # Generate semantic explanation
        why = self._generate_why(kwargs)
        if why:
            meta = kwargs.get("metadata") or {}
            meta["why"] = why
            kwargs["metadata"] = meta
        step = ExecutionStep(id=f"es.{self._step_counter}", **kwargs)
        self._graph.steps.append(step)
        return step

    def _link_seq(self, src: str | None, tgt: str, kind: str = "sequence") -> None:
        # Override kind if set by _walk_body for branch forks
        if hasattr(self, '_next_link_kind') and self._next_link_kind:
            kind = self._next_link_kind
            self._next_link_kind = None
        if not src:
            if hasattr(self, '_pending_merge') and self._pending_merge:
                for merge_src in self._pending_merge:
                    self._graph.links.append(DataLink(
                        id=self._next_link_id(),
                        source_step_id=merge_src,
                        target_step_id=tgt,
                        kind=kind,
                    ))
                self._pending_merge = []
                return
            return
        self._graph.links.append(DataLink(
            id=self._next_link_id(), source_step_id=src, target_step_id=tgt, kind=kind,
        ))

    def _link_data(self, inputs: list[str], tgt_id: str, branch_id: str | None) -> None:
        for inp in inputs:
            for producer_id in self._var_producers.get(inp, []):
                if producer_id:
                    self._graph.links.append(DataLink(
                        id=self._next_link_id(),
                        source_step_id=producer_id,
                        target_step_id=tgt_id,
                        kind="data",
                        variable=inp,
                        label=inp,
                        confidence="definite",
                        evidence=f"Variable '{inp}' assigned then referenced",
                    ))

    def _next_link_id(self) -> str:
        self._link_counter += 1
        return f"dl.{self._link_counter}"

    def _resolve_call_in_expr(self, node: ast.AST | None, func: FunctionDef) -> FunctionDef | None:
        """Find the outermost call in an expression and resolve it.

        Uses _get_call_target to extract full attribute chain (owner_parts,
        is_attribute_call) so that _resolve_call can do proper type-aware
        dispatch instead of falling back to name-only heuristics.

        Also handles:
          - IfExp (ternary): resolves the if-true branch call
          - NamedExpr (walrus): resolves the value expression
        """
        if node is None:
            return None
        if isinstance(node, ast.Await):
            return self._resolve_call_in_expr(node.value, func)
        # Ternary: foo() if cond else bar() → resolve the if-true branch
        if isinstance(node, ast.IfExp):
            return (self._resolve_call_in_expr(node.body, func)
                    or self._resolve_call_in_expr(node.orelse, func))
        # Walrus: (x := expr) → resolve expr
        if hasattr(ast, "NamedExpr") and isinstance(node, ast.NamedExpr):
            result = self._resolve_call_in_expr(node.value, func)
            # Track the walrus variable as a producer
            if isinstance(node.target, ast.Name) and result:
                pass  # assignment handled by _handle_assign caller
            return result
        if isinstance(node, ast.Call):
            from codecanvas.parser.call_graph import CallSite
            func_name, owner_parts, is_attribute_call = CallGraphBuilder._get_call_target(node)
            if not func_name:
                return None
            call = CallSite(
                func_name=func_name,
                line=node.lineno,
                owner_parts=owner_parts,
                is_attribute_call=is_attribute_call,
            )
            target = self.cg._resolve_call(call, func)
            if target and target.definition_type not in ("class", "schema"):
                return target  # includes nested functions — caller decides to inline
        return None

    def _get_call_name(self, node: ast.Call) -> str | None:
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        return None

    def _extract_names(self, node: ast.AST | None) -> list[str]:
        """Extract variable names referenced in an expression."""
        if node is None:
            return []
        names: list[str] = []
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                if child.id not in {"None", "True", "False", "self"}:
                    names.append(child.id)
        return list(dict.fromkeys(names))[:5]  # dedupe, limit

    def _extract_status(self, call_node: ast.Call) -> int | None:
        for kw in call_node.keywords:
            if kw.arg == "status_code" and isinstance(kw.value, ast.Constant):
                val = kw.value.value
                if isinstance(val, int):
                    return val
        if call_node.args and isinstance(call_node.args[0], ast.Constant):
            val = call_node.args[0].value
            if isinstance(val, int):
                return val
        return None

    def _generate_why(self, kwargs: dict) -> str:
        """Generate a semantic 'why' explanation for an execution step."""
        op = kwargs.get("operation", "")
        label = kwargs.get("label", "")
        callee_qn = kwargs.get("callee_function", "")
        phase = kwargs.get("phase", "")
        inputs = kwargs.get("inputs", [])
        output = kwargs.get("output", "")
        error_label = kwargs.get("error_label", "")
        branch_condition = kwargs.get("branch_condition", "")

        # Pipeline steps
        if op == "pipeline":
            if phase == "trigger":
                return "Entry point: where the request arrives"
            if phase == "middleware":
                return "Middleware: pre-processing before handler"
            if phase == "dependency":
                return f"Resolve dependency: prepare {label} for injection"
            if phase == "validation":
                return "Validate: parse and validate request body"
            if phase == "handler":
                return "Handler: main request processing logic"
            if phase == "serialization":
                return "Serialize: format response for client"
            return f"Pipeline: {phase} stage"

        # Error steps
        if op == "error":
            if error_label:
                return f"Error response: return HTTP {error_label} to client"
            return "Error: abort with exception"

        # Branch steps — use semantic explanation when available
        if op == "branch":
            if branch_condition:
                try:
                    test_ast = ast.parse(branch_condition, mode="eval").body
                    from codecanvas.graph.cfg import CFGBuilder
                    explanation = CFGBuilder._explain_branch(test_ast, [], [])
                    if explanation:
                        return explanation
                except Exception:
                    pass  # Falls through to raw condition text below
                return f"Decision point: {self._compact(branch_condition, 60)}"
            return "Decision point"

        # Respond steps
        if op == "respond":
            if output:
                return f"Build response from {output}"
            return "Build and return response to client"

        # Query / I/O steps — use db_query detail if available, then callee docstring
        if op == "query":
            db_query = kwargs.get("metadata", {}).get("db_query")
            if db_query:
                return self._describe_db_query(db_query, output)
            if callee_qn:
                callee = self.cg._functions.get(callee_qn)
                if callee:
                    doc = callee.docstring
                    if doc:
                        first_line = doc.strip().split("\n")[0].strip().rstrip(".")
                        if len(first_line) <= 80:
                            return first_line
                    # Infer from name
                    name = callee.name.lower()
                    if any(w in name for w in ("get", "find", "fetch", "load", "query", "list", "search")):
                        target = output or callee.name
                        return f"Fetch: retrieve {target} from data source"
                    if any(w in name for w in ("create", "save", "store", "insert", "add")):
                        return f"Write: persist {output or 'data'} to data source"
                    if any(w in name for w in ("update", "patch", "modify")):
                        return f"Update: modify {output or 'data'} in data source"
                    if any(w in name for w in ("delete", "remove", "revoke")):
                        return f"Delete: remove {output or 'data'} from data source"
                    if any(w in name for w in ("send", "post", "request", "call")):
                        return f"External call: invoke {callee.name}"

        # Transform steps
        if op == "transform" and callee_qn:
            callee = self.cg._functions.get(callee_qn)
            if callee and callee.docstring:
                first_line = callee.docstring.strip().split("\n")[0].strip().rstrip(".")
                if len(first_line) <= 80:
                    return first_line
            if output:
                return f"Transform: compute {output}"

        # Side effect
        if op == "side_effect":
            return f"Side effect: {label}"

        return ""

    @staticmethod
    def _describe_db_query(db_query: dict, output: str = "") -> str:
        """Build a human-readable description from db_query metadata."""
        model = db_query.get("model") or db_query.get("table") or ""
        op = db_query.get("operation", "")

        # If we have parsed SQL, use that
        sql_parsed = db_query.get("sql_parsed")
        if sql_parsed:
            table = sql_parsed.get("table", model)
            sql_op = sql_parsed.get("operation", "").upper()
            where = sql_parsed.get("where", "")
            if sql_op == "SELECT":
                desc = f"Fetch {table}"
                if where:
                    desc += f" where {where[:40]}"
                return desc
            if sql_op == "INSERT":
                return f"Insert into {table}"
            if sql_op in ("UPDATE", "DELETE"):
                desc = f"{sql_op.capitalize()} {table}"
                if where:
                    desc += f" where {where[:40]}"
                return desc

        # ORM-based description
        filters = db_query.get("filters", [])
        order_by = db_query.get("order_by", [])
        joins = db_query.get("joins", [])

        read_ops = {"query", "select", "get", "first", "one", "one_or_none",
                     "all", "scalar", "scalars", "fetch", "fetchone", "fetchall"}
        write_ops = {"add", "insert", "merge", "flush", "commit"}
        update_ops = {"update"}
        delete_ops = {"delete"}

        if op in read_ops or any(c in read_ops for c in db_query.get("chain", [])):
            desc = f"Fetch {model or output or 'data'}"
        elif op in write_ops:
            desc = f"Write {model or output or 'data'}"
        elif op in update_ops:
            desc = f"Update {model or output or 'data'}"
        elif op in delete_ops:
            desc = f"Delete {model or output or 'data'}"
        else:
            desc = f"DB {op} {model or output or ''}"

        parts: list[str] = []
        if filters:
            filter_strs = []
            for f in filters[:2]:
                if "column" in f:
                    filter_strs.append(f["column"])
                elif "expr" in f:
                    filter_strs.append(f["expr"][:20])
            if filter_strs:
                parts.append(f"where {', '.join(filter_strs)}")
        if joins:
            parts.append(f"join {', '.join(joins[:2])}")
        if order_by:
            parts.append(f"order {', '.join(order_by[:1])}")

        if parts:
            desc += " | " + " | ".join(parts)
        return desc

    def _collect_response_origins(self, inputs: list[str], max_depth: int = 6) -> list[dict]:
        """Walk _var_producers transitively to build the response provenance chain.

        Returns list of {stepId, variable, label, operation} ordered from
        closest producer to furthest ancestor.
        """
        step_index: dict[str, ExecutionStep] = {s.id: s for s in self._graph.steps}
        origins: list[dict] = []
        visited: set[str] = set()
        frontier = list(inputs)

        for _ in range(max_depth):
            next_frontier: list[str] = []
            for var_name in frontier:
                for producer_id in self._var_producers.get(var_name, []):
                    if not producer_id or producer_id in visited:
                        continue
                    visited.add(producer_id)
                    step = step_index.get(producer_id)
                    if not step:
                        continue
                    origins.append({
                        "stepId": producer_id,
                        "variable": var_name,
                        "label": step.label,
                        "operation": step.operation,
                    })
                    # Continue tracing through this step's inputs
                    if step.inputs:
                        next_frontier.extend(step.inputs)
            if not next_frontier:
                break
            frontier = next_frontier

        return origins

    def _callee_review_signals(self, callee: FunctionDef, is_io: bool) -> dict:
        """Build metadata with review signals for a callee step."""
        signals = CallGraphBuilder._aggregate_review_signals(callee)
        # If classified as I/O but no specific db/http signal, add generic one
        if is_io and not any(s.startswith("db_") or s == "http_call" for s in signals):
            signals.append("io")
        if not signals:
            return {}
        return {"review_signals": signals}

    @staticmethod
    def _extract_isinstance_guard(test: ast.expr) -> tuple[str, str] | None:
        """Extract (var_name, type_name) from isinstance(var, Type) guards.

        Supports:
          - isinstance(x, Foo)
          - isinstance(x, (Foo, Bar)) → takes first type
        """
        if not isinstance(test, ast.Call):
            return None
        if not (isinstance(test.func, ast.Name) and test.func.id == "isinstance"):
            return None
        if len(test.args) < 2:
            return None
        var_node = test.args[0]
        type_node = test.args[1]

        var_name = None
        if isinstance(var_node, ast.Name):
            var_name = var_node.id
        elif isinstance(var_node, ast.Attribute) and isinstance(var_node.value, ast.Name):
            var_name = var_node.value.id  # isinstance(self.x, ...) → narrow self.x isn't simple
            return None  # Only support simple names for now

        if var_name is None:
            return None

        type_name = None
        if isinstance(type_node, ast.Name):
            type_name = type_node.id
        elif isinstance(type_node, ast.Attribute):
            type_name = type_node.attr
        elif isinstance(type_node, ast.Tuple) and type_node.elts:
            first = type_node.elts[0]
            if isinstance(first, ast.Name):
                type_name = first.id
            elif isinstance(first, ast.Attribute):
                type_name = first.attr

        if type_name and type_name[0].isupper():
            return (var_name, type_name)
        return None

    @staticmethod
    def _extract_none_guard(test: ast.expr, func: FunctionDef) -> tuple[str, str] | None:
        """Extract type narrowing from `if x is not None` patterns.

        When a variable has an Optional[X] type annotation and we see
        `if x is not None`, narrow to X within the if-body.
        Also handles `if x:` for variables with known optional types.
        """
        # Pattern: x is not None  (ast.Compare with Is Not / IsNot)
        if isinstance(test, ast.Compare) and len(test.ops) == 1 and len(test.comparators) == 1:
            op = test.ops[0]
            comparator = test.comparators[0]
            is_none_check = (
                isinstance(comparator, ast.Constant) and comparator.value is None
            )
            if is_none_check and isinstance(op, ast.IsNot):
                if isinstance(test.left, ast.Name):
                    var_name = test.left.id
                    # Check if we have an Optional[X] type for this var
                    existing_type = func.local_types.get(var_name)
                    if existing_type:
                        # Already have a concrete type — no need to narrow
                        return None
                    # Variable is used after None check — mark as non-None
                    # Can't infer specific type without annotation, skip
                    return None

        # Pattern: `if x:` where x has Optional[Type] annotation
        if isinstance(test, ast.Name):
            existing_type = func.local_types.get(test.id)
            if existing_type and existing_type.startswith("Optional"):
                # Strip Optional wrapper
                import re as _re
                inner = _re.search(r"Optional\[(\w+)\]", existing_type)
                if inner:
                    return (test.id, inner.group(1))

        return None

    def _callee_confidence(self, callee: FunctionDef) -> str:
        """Read the resolution confidence from the last _resolve_call invocation.

        Uses CallGraphBuilder._last_resolve_confidence (per-resolution, not
        per-FunctionDef) to avoid stale confidence on shared singletons.
        """
        return getattr(self.cg, "_last_resolve_confidence", None) or "definite"

    def _is_io_func(self, func: FunctionDef) -> bool:
        from codecanvas.graph.models import NodeType
        node_type = self.cg._classify_function(func)
        if node_type in (NodeType.REPOSITORY, NodeType.DATABASE, NodeType.EXTERNAL_API):
            return True
        name = func.name.lower()
        io_hints = {"get", "fetch", "find", "list", "query", "search", "load",
                    "save", "store", "create", "update", "delete", "insert",
                    "send", "post", "put", "process", "execute", "init"}
        return bool(set(name.split("_")) & io_hints)

    @staticmethod
    def _ends_with_return(stmts: list[ast.stmt]) -> bool:
        if not stmts:
            return False
        last = stmts[-1]
        if isinstance(last, ast.Return):
            return True
        if isinstance(last, ast.Raise):
            return True
        return False

    @staticmethod
    def _humanize(name: str) -> str:
        name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
        name = name.lstrip("_").replace("_", " ")
        return name.strip().capitalize() if name else "Process"

    @staticmethod
    def _compact(s: str, max_len: int = 45) -> str:
        s = " ".join(s.split())
        return s[:max_len] + "..." if len(s) > max_len else s
