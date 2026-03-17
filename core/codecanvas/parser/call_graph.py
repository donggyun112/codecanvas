"""Build call graphs from Python source using AST analysis.

Traces function calls from a given entry point (route handler)
through service layers, repositories, and external calls.
"""
from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codecanvas.graph.models import (
    Confidence,
    EdgeType,
    Evidence,
    FlowEdge,
    FlowNode,
    NodeType,
)


@dataclass
class FunctionDef:
    """A discovered function definition."""
    name: str
    qualified_name: str        # module.Class.method
    file_path: str
    line_start: int
    line_end: int
    is_async: bool = False
    class_name: str | None = None
    decorators: list[str] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)
    docstring: str = ""
    params: list[str] = field(default_factory=list)
    return_annotation: str | None = None
    definition_type: str = "function"  # function | class
    class_qname: str | None = None
    local_types: dict[str, str] = field(default_factory=dict)
    logic_steps: list[LogicStep] = field(default_factory=list)


@dataclass
class CallSite:
    """A function call found inside a function body."""
    func_name: str
    line: int
    is_await: bool = False
    in_branch: str | None = None    # "if", "elif", "else", "try", "except"
    branch_condition: str | None = None
    is_db_call: bool = False
    is_http_call: bool = False
    is_raise: bool = False          # raise SomeException(...)
    raise_status: int | None = None # HTTP status code if HTTPException
    owner_parts: tuple[str, ...] = ()
    is_attribute_call: bool = False


@dataclass
class LogicStep:
    """A summarized top-level logic step inside a function body."""
    node_type: NodeType
    display_name: str
    description: str
    line: int
    line_end: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# Heuristic patterns for detecting DB and external API calls
DB_PATTERNS = {
    "execute", "query", "fetch", "fetchone", "fetchall", "fetchmany",
    "commit", "rollback", "add", "delete", "merge", "flush", "refresh",
    "scalar", "scalars", "all", "first", "one", "one_or_none", "get",
    "filter", "filter_by", "where", "select", "insert", "update",
}
DB_OBJECT_HINTS = {"session", "db", "database", "conn", "connection", "cursor", "engine"}

HTTP_PATTERNS = {
    "get", "post", "put", "delete", "patch", "head", "options",
    "request", "fetch", "send",
}
HTTP_OBJECT_HINTS = {"client", "http", "httpx", "requests", "aiohttp", "session"}
LOW_SIGNAL_METHODS = {
    "append", "extend", "insert", "isoformat", "get",
    "items", "keys", "values", "split", "strip",
    "lower", "upper", "startswith", "endswith",
}

# Calls to filter out (framework internals, builtins, noise)
IGNORE_CALLS = {
    # FastAPI/Starlette framework
    "router.get", "router.post", "router.put", "router.delete", "router.patch",
    "app.get", "app.post", "app.put", "app.delete", "app.patch",
    "router.options", "router.head", "router.trace",
    "app.options", "app.head", "app.trace",
    "app.include_router", "app.add_middleware", "app.exception_handler",
    # Dependency injection
    "Depends",
    # Pydantic / response models
    "BaseModel", "Field", "model_dump", "model_validate",
    # Python builtins
    "print", "len", "str", "int", "float", "bool", "list", "dict", "set",
    "tuple", "type", "isinstance", "issubclass", "hasattr", "getattr", "setattr",
    "range", "enumerate", "zip", "map", "filter", "sorted", "reversed",
    "super", "property", "staticmethod", "classmethod",
    # Common constructors that aren't meaningful in flow (NOT HTTPException — we need error paths)
    "ValueError", "TypeError", "KeyError", "RuntimeError",
    "Exception",
}
IGNORE_PREFIXES = {"router.", "app.", "response.", "request."}


class CallGraphBuilder:
    """Build a call graph starting from a specific function."""

    def __init__(self, project_root: str):
        self.project_root = Path(project_root)
        self._functions: dict[str, FunctionDef] = {}  # qualified_name -> FunctionDef
        self._name_index: dict[str, list[str]] = {}   # simple name -> [qualified_names]
        self._file_asts: dict[str, ast.Module] = {}
        self._module_map: dict[str, str] = {}          # file_path -> module name
        self._class_attr_types: dict[str, dict[str, str]] = {}
        self._analyzed = False

    def analyze_project(self) -> None:
        """Analyze all Python files in the project."""
        if self._analyzed:
            return
        for root, dirs, files in os.walk(self.project_root):
            dirs[:] = [d for d in dirs
                       if d not in {".venv", "venv", "__pycache__", ".git",
                                    "node_modules", "migrations"}]
            for f in files:
                if f.endswith(".py"):
                    fpath = os.path.join(root, f)
                    self._analyze_file(fpath)
        self._analyzed = True

    def _analyze_file(self, file_path: str) -> None:
        """Parse one file and extract all function definitions."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source, filename=file_path)
        except (SyntaxError, UnicodeDecodeError):
            return

        self._file_asts[file_path] = tree
        rel_path = os.path.relpath(file_path, self.project_root)
        module_name = rel_path.replace(os.sep, ".").removesuffix(".py").removesuffix(".__init__")
        self._module_map[file_path] = module_name

        self._visit_definitions(tree, module_name, file_path)

    def _visit_definitions(
        self,
        tree: ast.AST,
        namespace: str,
        file_path: str,
        class_name: str | None = None,
        class_qname: str | None = None,
    ) -> None:
        """Recursively visit function/class definitions."""
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qname = f"{namespace}.{node.name}"
                local_types, self_attr_types = self._extract_assignment_types(node)
                func_def = FunctionDef(
                    name=node.name,
                    qualified_name=qname,
                    file_path=file_path,
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                    is_async=isinstance(node, ast.AsyncFunctionDef),
                    class_name=class_name,
                    decorators=[self._decorator_name(d) for d in node.decorator_list],
                    calls=self._extract_calls(node),
                    docstring=ast.get_docstring(node) or "",
                    params=[a.arg for a in node.args.args if a.arg != "self"],
                    return_annotation=self._annotation_str(node.returns),
                    class_qname=class_qname,
                    local_types=local_types,
                    logic_steps=self._extract_logic_steps(node),
                )
                self._functions[qname] = func_def
                self._name_index.setdefault(node.name, []).append(qname)
                if node.name == "__init__" and class_qname and self_attr_types:
                    self._class_attr_types.setdefault(class_qname, {}).update(self_attr_types)
                # Index nested functions inside this function scope as lexical children.
                self._visit_definitions(node, qname, file_path, class_name, class_qname)

            elif isinstance(node, ast.ClassDef):
                class_qname = f"{namespace}.{node.name}"
                init_node = next(
                    (
                        child for child in node.body
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and child.name == "__init__"
                    ),
                    None,
                )
                class_def = FunctionDef(
                    name=node.name,
                    qualified_name=class_qname,
                    file_path=file_path,
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                    is_async=False,
                    class_name=None,
                    decorators=[self._decorator_name(d) for d in node.decorator_list],
                    calls=self._extract_calls(init_node) if init_node else [],
                    docstring=ast.get_docstring(node) or "",
                    params=[a.arg for a in init_node.args.args if a.arg != "self"] if init_node else [],
                    return_annotation=node.name,
                    definition_type="class",
                    class_qname=class_qname,
                )
                self._functions[class_qname] = class_def
                self._name_index.setdefault(node.name, []).append(class_qname)
                self._visit_definitions(node, class_qname, file_path, node.name, class_qname)

    def _extract_calls(self, func_node: ast.AST) -> list[CallSite]:
        """Extract all function calls within a function body using recursive traversal.

        Uses proper tree recursion so branch context is pushed/popped correctly
        instead of ast.walk() which flattens the tree.
        """
        calls: list[CallSite] = []
        body = getattr(func_node, "body", None)
        if isinstance(body, list):
            for child in body:
                self._visit_calls(child, calls, branch_ctx=None)
        else:
            self._visit_calls(func_node, calls, branch_ctx=None)
        return calls

    def _visit_calls(
        self,
        node: ast.AST,
        calls: list[CallSite],
        branch_ctx: tuple[str, str | None] | None,
    ) -> None:
        """Recursively visit AST nodes, tracking branch context with proper push/pop."""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            # Nested scopes are indexed separately; do not flatten them into the parent flow.
            return

        # Determine branch context for children of control-flow nodes
        if isinstance(node, ast.If):
            cond = ast.unparse(node.test) if hasattr(ast, "unparse") else ""
            ctx = ("if", cond)
            # Visit the test expression itself — calls in conditions matter
            # (e.g. `if self._check_password(...)`)
            self._visit_calls(node.test, calls, branch_ctx)
            for child in node.body:
                self._visit_calls(child, calls, ctx)
            for child in node.orelse:
                self._visit_calls(child, calls, ("else", cond))
            return
        if isinstance(node, ast.Try) or (hasattr(ast, "TryStar") and isinstance(node, ast.TryStar)):
            for child in node.body:
                self._visit_calls(child, calls, ("try", None))
            for handler in node.handlers:
                exc = self._get_name(handler.type) if handler.type else "Exception"
                for child in handler.body:
                    self._visit_calls(child, calls, ("except", exc))
            for child in getattr(node, "orelse", []):
                self._visit_calls(child, calls, branch_ctx)
            for child in getattr(node, "finalbody", []):
                self._visit_calls(child, calls, branch_ctx)
            return

        # Handle raise statements — capture error response paths
        if isinstance(node, ast.Raise) and node.exc:
            if isinstance(node.exc, ast.Call):
                func_name, _, _ = self._get_call_target(node.exc)
                if func_name:
                    status_code = self._extract_status_code(node.exc)
                    calls.append(CallSite(
                        func_name=func_name,
                        line=node.lineno,
                        is_raise=True,
                        raise_status=status_code,
                        in_branch=branch_ctx[0] if branch_ctx else None,
                        branch_condition=branch_ctx[1] if branch_ctx else None,
                    ))
            return

        # Extract calls from this node
        if isinstance(node, ast.Call):
            func_name, owner_parts, is_attribute_call = self._get_call_target(node)
            is_db = self._is_db_call(node)
            is_http = self._is_http_call(node)
            if func_name and not self._should_ignore_call(func_name) and not self._is_low_signal_call(node, is_db, is_http):
                calls.append(CallSite(
                    func_name=func_name,
                    line=node.lineno,
                    is_await=False,
                    in_branch=branch_ctx[0] if branch_ctx else None,
                    branch_condition=branch_ctx[1] if branch_ctx else None,
                    is_db_call=is_db,
                    is_http_call=is_http,
                    owner_parts=owner_parts,
                    is_attribute_call=is_attribute_call,
                ))

        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            if calls and calls[-1].line == node.value.lineno:
                calls[-1].is_await = True

        # Recurse into children
        for child in ast.iter_child_nodes(node):
            self._visit_calls(child, calls, branch_ctx)

    @staticmethod
    def _extract_status_code(call_node: ast.Call) -> int | None:
        """Extract status_code from HTTPException(status_code=401) or similar."""
        for kw in call_node.keywords:
            if kw.arg == "status_code" and isinstance(kw.value, ast.Constant):
                val = kw.value.value
                if isinstance(val, int):
                    return val
        # Also check first positional arg
        if call_node.args and isinstance(call_node.args[0], ast.Constant):
            val = call_node.args[0].value
            if isinstance(val, int):
                return val
        return None

    def build_flow_from(
        self,
        handler_name: str,
        handler_file: str,
        line_number: int | None = None,
        max_depth: int = 10,
    ) -> tuple[dict[str, FlowNode], list[FlowEdge]]:
        """Build flow nodes and edges from a handler function, following calls."""
        self.analyze_project()

        nodes: dict[str, FlowNode] = {}
        edges: list[FlowEdge] = []
        visited: set[str] = set()
        edge_counter = 0

        # Find the handler function
        handler_func = self._find_function(handler_name, handler_file, line_number)
        if not handler_func:
            return nodes, edges

        def traverse(func: FunctionDef, depth: int, parent_id: str | None = None) -> str:
            nonlocal edge_counter
            if depth > max_depth or func.qualified_name in visited:
                return func.qualified_name
            visited.add(func.qualified_name)

            # Create node for this function
            node = FlowNode(
                id=func.qualified_name,
                node_type=self._classify_function(func),
                name=func.name,
                display_name=func.name,
                description=self._describe_function(func),
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
                level=3,  # Function level
                metadata={
                    "is_async": func.is_async,
                    "params": func.params,
                    "return_type": func.return_annotation,
                    "class": func.class_name,
                },
            )
            nodes[node.id] = node
            edge_counter = self._add_logic_nodes(func, node.id, nodes, edges, edge_counter)

            # Process each call inside this function
            for call in func.calls:
                # Handle raise statements → error response nodes
                if call.is_raise:
                    status = call.raise_status
                    detail_str = f"{status} " if status else ""
                    exc_id = f"error.{call.func_name}.{call.line}"
                    display = f"raise {call.func_name}({detail_str}...)"
                    nodes[exc_id] = FlowNode(
                        id=exc_id,
                        node_type=NodeType.EXCEPTION,
                        name=call.func_name,
                        display_name=display,
                        description=self._describe_exception(call),
                        file_path=func.file_path,
                        line_start=call.line,
                        confidence=Confidence.DEFINITE,
                        evidence=[Evidence(
                            source="static_analysis",
                            file_path=func.file_path,
                            line_number=call.line,
                            detail=display,
                        )],
                        level=4,
                        metadata={"status_code": status} if status else {},
                    )
                    edge_counter += 1
                    edges.append(FlowEdge(
                        id=f"e{edge_counter}",
                        source_id=node.id,
                        target_id=exc_id,
                        edge_type=EdgeType.RAISES,
                        label=f"HTTP {status}" if status else "raise",
                        confidence=Confidence.DEFINITE,
                        evidence=[Evidence(
                            source="static_analysis",
                            file_path=func.file_path,
                            line_number=call.line,
                            detail=display,
                        )],
                        condition=call.branch_condition,
                        is_error_path=True,
                    ))
                    continue

                target_func = self._resolve_call(call, func)

                if target_func:
                    # Definite connection
                    child_id = traverse(target_func, depth + 1, node.id)
                    edge_counter += 1
                    edge_type = EdgeType.CALLS
                    if call.is_db_call:
                        edge_type = EdgeType.QUERIES
                    elif call.is_http_call:
                        edge_type = EdgeType.REQUESTS

                    edges.append(FlowEdge(
                        id=f"e{edge_counter}",
                        source_id=node.id,
                        target_id=child_id,
                        edge_type=edge_type,
                        confidence=Confidence.DEFINITE,
                        evidence=[Evidence(
                            source="static_analysis",
                            file_path=func.file_path,
                            line_number=call.line,
                            detail=f"Call to {call.func_name} at line {call.line}",
                        )],
                        condition=call.branch_condition,
                        is_error_path=call.in_branch in ("except",),
                    ))
                else:
                    # Create stub node for unresolved call
                    stub_id = f"unresolved.{call.func_name}"
                    if stub_id not in nodes:
                        stub_type = NodeType.DATABASE if call.is_db_call \
                            else NodeType.EXTERNAL_API if call.is_http_call \
                            else NodeType.FUNCTION

                        nodes[stub_id] = FlowNode(
                            id=stub_id,
                            node_type=stub_type,
                            name=call.func_name,
                            description=self._describe_unresolved_call(call),
                            confidence=Confidence.INFERRED,
                            evidence=[Evidence(
                                source="static_analysis",
                                file_path=func.file_path,
                                line_number=call.line,
                                detail=f"Unresolved call to {call.func_name}",
                            )],
                            level=3,
                        )
                    edge_counter += 1
                    edges.append(FlowEdge(
                        id=f"e{edge_counter}",
                        source_id=node.id,
                        target_id=stub_id,
                        edge_type=(
                            EdgeType.QUERIES if call.is_db_call
                            else EdgeType.REQUESTS if call.is_http_call
                            else EdgeType.CALLS
                        ),
                        confidence=Confidence.INFERRED,
                        evidence=[Evidence(
                            source="static_analysis",
                            file_path=func.file_path,
                            line_number=call.line,
                            detail=f"Unresolved: {call.func_name}",
                        )],
                        condition=call.branch_condition,
                        is_error_path=call.in_branch in ("except",),
                    ))

            return node.id

        traverse(handler_func, 0)
        return nodes, edges

    def _add_logic_nodes(
        self,
        func: FunctionDef,
        function_node_id: str,
        nodes: dict[str, FlowNode],
        edges: list[FlowEdge],
        edge_counter: int,
    ) -> int:
        """Attach Level 4 statement summaries to a function node."""
        previous_id = function_node_id
        for index, step in enumerate(func.logic_steps):
            node_id = f"{function_node_id}.logic.{index}"
            nodes[node_id] = FlowNode(
                id=node_id,
                node_type=step.node_type,
                name=step.display_name,
                display_name=step.display_name,
                description=step.description,
                file_path=func.file_path,
                line_start=step.line,
                line_end=step.line_end,
                confidence=Confidence.DEFINITE,
                evidence=[Evidence(
                    source="static_analysis",
                    file_path=func.file_path,
                    line_number=step.line,
                    detail=step.display_name,
                )],
                metadata={"function_id": function_node_id, **step.metadata},
                level=4,
            )
            edge_counter += 1
            edge_type = EdgeType.RETURNS if step.node_type == NodeType.RETURN else EdgeType.CALLS
            edges.append(FlowEdge(
                id=f"e{edge_counter}",
                source_id=previous_id,
                target_id=node_id,
                edge_type=edge_type,
                confidence=Confidence.DEFINITE,
                evidence=[Evidence(
                    source="static_analysis",
                    file_path=func.file_path,
                    line_number=step.line,
                    detail=step.display_name,
                )],
                condition=step.metadata.get("condition"),
            ))
            previous_id = node_id
        return edge_counter

    def resolve_function_id(
        self,
        name: str,
        file_path: str,
        line_number: int | None = None,
    ) -> str | None:
        """Resolve a function and return its qualified node ID."""
        self.analyze_project()
        func = self._find_function(name, file_path, line_number)
        return func.qualified_name if func else None

    def _find_function(
        self,
        name: str,
        file_path: str,
        line_number: int | None = None,
    ) -> FunctionDef | None:
        """Find a function by name, preferring the given file."""
        candidate_ids = self._name_index.get(name, [])
        candidates = [self._functions[qname] for qname in candidate_ids]

        if line_number is not None:
            exact_match = next(
                (
                    func for func in candidates
                    if func.file_path == file_path and func.line_start == line_number
                ),
                None,
            )
            if exact_match:
                return exact_match

        # Prefer match in the same file
        same_file = [func for func in candidates if func.file_path == file_path]
        if same_file:
            if line_number is not None:
                return min(same_file, key=lambda func: abs(func.line_start - line_number))
            return same_file[0]

        if line_number is not None and candidates:
            return min(candidates, key=lambda func: abs(func.line_start - line_number))

        # Fallback to first match
        if candidates:
            return candidates[0]
        return None

    def _resolve_call(self, call: CallSite, caller: FunctionDef) -> FunctionDef | None:
        """Try to resolve a call to a known function definition."""
        if call.is_attribute_call:
            resolved = self._resolve_attribute_call(call, caller)
            if resolved is not None:
                return resolved

            # Avoid binding object/client method chains like
            # `client.table(...).execute()` to unrelated project methods
            # purely because the last segment shares a name.
            return None

        name = call.func_name

        candidates = self._name_index.get(name, [])
        if not candidates:
            return None
        resolved_candidates = [self._functions[qname] for qname in candidates]

        local_nested = [
            func for func in resolved_candidates
            if func.file_path == caller.file_path
            and func.qualified_name.startswith(caller.qualified_name + ".")
        ]
        if local_nested:
            return min(local_nested, key=lambda func: abs(func.line_start - call.line))

        if name[:1].isupper():
            class_candidates = [func for func in resolved_candidates if func.definition_type == "class"]
            preferred_class = self._prefer_same_module(class_candidates, caller)
            if preferred_class:
                return preferred_class

        # Prefer same module
        preferred = self._prefer_same_module(resolved_candidates, caller)
        if preferred:
            return preferred

        # Fallback: first candidate
        return resolved_candidates[0]

    def _resolve_attribute_call(self, call: CallSite, caller: FunctionDef) -> FunctionDef | None:
        """Resolve attribute/member calls only when the receiver type is known."""
        method_name = call.func_name.split(".")[-1]
        owner_parts = call.owner_parts
        if not owner_parts:
            return None

        root = owner_parts[0]
        if root == "self" and caller.class_name:
            if len(owner_parts) == 1:
                return self._resolve_method_on_class(caller.class_name, method_name, caller)

            if caller.class_qname:
                attr_name = owner_parts[1]
                attr_type = self._class_attr_types.get(caller.class_qname, {}).get(attr_name)
                if attr_type:
                    return self._resolve_method_on_class(attr_type, method_name, caller)
            return None

        local_type = caller.local_types.get(root)
        if local_type:
            return self._resolve_method_on_class(local_type, method_name, caller)

        if root[:1].isupper():
            return self._resolve_method_on_class(root, method_name, caller)

        return None

    def _resolve_method_on_class(
        self,
        class_name: str,
        method_name: str,
        caller: FunctionDef,
    ) -> FunctionDef | None:
        """Resolve a method on a specific class name."""
        candidates = [
            self._functions[qname]
            for qname in self._name_index.get(method_name, [])
            if self._functions[qname].class_name == class_name
        ]
        if not candidates:
            return None
        preferred = self._prefer_same_module(candidates, caller)
        if preferred:
            return preferred
        return candidates[0]

    def _extract_logic_steps(
        self,
        func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> list[LogicStep]:
        """Summarize top-level statements inside a function body."""
        steps: list[LogicStep] = []
        for stmt in func_node.body:
            step = self._logic_step_from_statement(stmt)
            if step is not None:
                steps.append(step)
        return steps

    def _logic_step_from_statement(self, stmt: ast.stmt) -> LogicStep | None:
        """Build a logic-step summary for a statement."""
        if isinstance(stmt, ast.Assign):
            targets = ", ".join(
                ast.unparse(target) if hasattr(ast, "unparse") else self._get_name(target)
                for target in stmt.targets
            )
            value = self._expr_summary(stmt.value)
            return LogicStep(
                node_type=NodeType.ASSIGNMENT,
                display_name=f"{targets} = {self._compact(value)}",
                description=f"Assign `{targets}` from {value}.",
                line=stmt.lineno,
                line_end=stmt.end_lineno,
                metadata={"target": targets, "value": value},
            )

        if isinstance(stmt, ast.AnnAssign):
            target = ast.unparse(stmt.target) if hasattr(ast, "unparse") else self._get_name(stmt.target)
            value = self._expr_summary(stmt.value) if stmt.value else "annotated value"
            return LogicStep(
                node_type=NodeType.ASSIGNMENT,
                display_name=f"{target} = {self._compact(value)}",
                description=f"Assign `{target}` from {value}.",
                line=stmt.lineno,
                line_end=stmt.end_lineno,
                metadata={"target": target, "value": value},
            )

        if isinstance(stmt, ast.AugAssign):
            target = ast.unparse(stmt.target) if hasattr(ast, "unparse") else self._get_name(stmt.target)
            op = self._operator_symbol(stmt.op)
            value = self._expr_summary(stmt.value)
            return LogicStep(
                node_type=NodeType.ASSIGNMENT,
                display_name=f"{target} {op}= {self._compact(value)}",
                description=f"Update `{target}` with `{op}=` using {value}.",
                line=stmt.lineno,
                line_end=stmt.end_lineno,
                metadata={"target": target, "value": value},
            )

        if isinstance(stmt, ast.If):
            condition = ast.unparse(stmt.test) if hasattr(ast, "unparse") else "condition"
            body_summary = self._summarize_block(stmt.body)
            else_summary = self._summarize_block(stmt.orelse)
            description = f"If `{condition}`, {body_summary}."
            if else_summary:
                description += f" Otherwise, {else_summary}."
            return LogicStep(
                node_type=NodeType.BRANCH,
                display_name=f"if {self._compact(condition)}",
                description=description,
                line=stmt.lineno,
                line_end=stmt.end_lineno,
                metadata={"condition": condition},
            )

        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            target = ast.unparse(stmt.target) if hasattr(ast, "unparse") else self._get_name(stmt.target)
            iterator = self._expr_summary(stmt.iter)
            body_summary = self._summarize_block(stmt.body)
            return LogicStep(
                node_type=NodeType.LOOP,
                display_name=f"for {target} in {self._compact(iterator)}",
                description=f"Loop over {iterator} as `{target}` and {body_summary}.",
                line=stmt.lineno,
                line_end=stmt.end_lineno,
                metadata={"target": target, "iterator": iterator},
            )

        if isinstance(stmt, ast.While):
            condition = ast.unparse(stmt.test) if hasattr(ast, "unparse") else "condition"
            body_summary = self._summarize_block(stmt.body)
            return LogicStep(
                node_type=NodeType.LOOP,
                display_name=f"while {self._compact(condition)}",
                description=f"Repeat while `{condition}` and {body_summary}.",
                line=stmt.lineno,
                line_end=stmt.end_lineno,
                metadata={"condition": condition},
            )

        if isinstance(stmt, ast.Return):
            value = self._expr_summary(stmt.value)
            return LogicStep(
                node_type=NodeType.RETURN,
                display_name=f"return {self._compact(value)}",
                description=f"Return {value}.",
                line=stmt.lineno,
                line_end=stmt.end_lineno,
                metadata={"value": value},
            )

        if isinstance(stmt, ast.Expr):
            if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                return None
            value = self._expr_summary(stmt.value)
            if not value:
                return None
            return LogicStep(
                node_type=NodeType.STEP,
                display_name=self._compact(value),
                description=f"Execute {value}.",
                line=stmt.lineno,
                line_end=stmt.end_lineno,
                metadata={"value": value},
            )

        return None

    def _classify_function(self, func: FunctionDef) -> NodeType:
        """Classify a function into a semantic node type."""
        if func.definition_type == "class":
            return NodeType.CLASS

        name_lower = func.name.lower()
        path_lower = func.file_path.lower()
        class_lower = (func.class_name or "").lower()

        # Check class name first (more reliable than file path)
        if class_lower:
            if any(p in class_lower for p in ("service",)):
                return NodeType.SERVICE
            if any(p in class_lower for p in ("repo", "repository", "crud", "dao")):
                return NodeType.REPOSITORY
            if any(p in class_lower for p in ("middleware",)):
                return NodeType.MIDDLEWARE

        # Dependency injection functions
        if any(p in name_lower for p in ("get_db", "get_session", "get_current", "get_redis")):
            return NodeType.DEPENDENCY

        # File path based classification
        if any(p in path_lower for p in ("route", "router", "endpoint", "view")):
            if "service" not in path_lower and "repo" not in path_lower:
                return NodeType.ROUTER
        if any(p in path_lower for p in ("service", "usecase", "logic")):
            return NodeType.SERVICE
        if any(p in path_lower for p in ("repo", "repository", "dal", "crud", "dao")):
            return NodeType.REPOSITORY
        if any(p in path_lower for p in ("middleware",)):
            return NodeType.MIDDLEWARE

        if func.class_name:
            return NodeType.METHOD
        return NodeType.FUNCTION

    def _describe_function(self, func: FunctionDef) -> str:
        """Build a human-readable description for a function node."""
        docstring = self._normalize_text(func.docstring)
        if docstring:
            return docstring

        if func.definition_type == "class":
            human_name = self._humanize_identifier(func.name)
            return f"Construct {human_name}."

        simple_name = func.name.lstrip("_")
        special_cases = {
            "__init__": f"Initialize {func.class_name or 'the object'}.",
            "execute": "Execute a database command.",
            "fetchone": "Fetch one row from the database result.",
            "fetchall": "Fetch all rows from the database result.",
            "commit": "Commit the current database transaction.",
            "close": "Close the active database or network resource.",
            "check_password": "Check whether the provided password is valid.",
            "create_jwt": "Create a JWT token payload for the response.",
            "decode_jwt": "Decode a JWT token and extract its payload.",
        }
        if simple_name in special_cases:
            return special_cases[simple_name]

        human_name = self._humanize_identifier(func.name)
        node_type = self._classify_function(func)
        prefix_map = {
            NodeType.ROUTER: "Handle request",
            NodeType.DEPENDENCY: "Resolve dependency",
            NodeType.SERVICE: "Run service step",
            NodeType.REPOSITORY: "Run repository step",
            NodeType.METHOD: "Run method",
            NodeType.FUNCTION: "Run function",
        }
        prefix = prefix_map.get(node_type, "Run step")
        return f"{prefix}: {human_name}."

    def _summarize_block(self, statements: list[ast.stmt]) -> str:
        """Summarize a block of statements into one review-friendly phrase."""
        parts: list[str] = []
        for stmt in statements[:3]:
            summary = self._statement_summary(stmt)
            if summary:
                parts.append(summary)
        if not parts:
            return "continue execution"
        text = ", then ".join(parts)
        remaining = len(statements) - len(parts)
        if remaining > 0:
            text += f", and {remaining} more step{'s' if remaining != 1 else ''}"
        return text

    def _statement_summary(self, stmt: ast.stmt) -> str:
        """Summarize one statement for branch/loop descriptions."""
        if isinstance(stmt, ast.Assign):
            targets = ", ".join(
                ast.unparse(target) if hasattr(ast, "unparse") else self._get_name(target)
                for target in stmt.targets
            )
            return f"set `{targets}`"
        if isinstance(stmt, ast.AnnAssign):
            target = ast.unparse(stmt.target) if hasattr(ast, "unparse") else self._get_name(stmt.target)
            return f"set `{target}`"
        if isinstance(stmt, ast.AugAssign):
            target = ast.unparse(stmt.target) if hasattr(ast, "unparse") else self._get_name(stmt.target)
            return f"update `{target}`"
        if isinstance(stmt, ast.Return):
            return f"return {self._expr_summary(stmt.value)}"
        if isinstance(stmt, ast.Raise):
            if isinstance(stmt.exc, ast.Call):
                func_name, _, _ = self._get_call_target(stmt.exc)
                return f"raise `{func_name}`"
            return "raise an exception"
        if isinstance(stmt, ast.Expr):
            if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                return ""
            value = self._expr_summary(stmt.value)
            return f"execute {value}" if value else ""
        if isinstance(stmt, ast.If):
            condition = ast.unparse(stmt.test) if hasattr(ast, "unparse") else "condition"
            return f"branch on `{condition}`"
        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            target = ast.unparse(stmt.target) if hasattr(ast, "unparse") else self._get_name(stmt.target)
            return f"iterate `{target}`"
        if isinstance(stmt, ast.While):
            condition = ast.unparse(stmt.test) if hasattr(ast, "unparse") else "condition"
            return f"loop while `{condition}`"
        return ""

    def _expr_summary(self, expr: ast.expr | None) -> str:
        """Summarize an expression in a compact but readable form."""
        if expr is None:
            return "no value"
        if hasattr(ast, "unparse"):
            return self._compact(ast.unparse(expr))
        if isinstance(expr, ast.Name):
            return expr.id
        if isinstance(expr, ast.Constant):
            return repr(expr.value)
        return "expression"

    @staticmethod
    def _compact(text: str, limit: int = 80) -> str:
        """Normalize and truncate source snippets for node labels."""
        compact = " ".join(text.split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3] + "..."

    @staticmethod
    def _operator_symbol(operator: ast.operator) -> str:
        """Render an assignment operator symbol for AugAssign."""
        mapping: dict[type[ast.operator], str] = {
            ast.Add: "+",
            ast.Sub: "-",
            ast.Mult: "*",
            ast.Div: "/",
            ast.Mod: "%",
            ast.Pow: "**",
            ast.FloorDiv: "//",
            ast.BitAnd: "&",
            ast.BitOr: "|",
            ast.BitXor: "^",
            ast.LShift: "<<",
            ast.RShift: ">>",
            ast.MatMult: "@",
        }
        return mapping.get(type(operator), "?")

    def _describe_unresolved_call(self, call: CallSite) -> str:
        """Describe a call target we could not resolve statically."""
        human_name = self._humanize_identifier(call.func_name)
        simple_name = call.func_name.split(".")[-1]

        if call.is_db_call:
            return f"Possible database operation: {human_name}."
        if call.is_http_call:
            return f"Possible external HTTP call: {human_name}."
        if simple_name[:1].isupper():
            return f"Instantiate or invoke {human_name}; definition could not be resolved statically."
        return f"Call {human_name}; definition could not be resolved statically."

    def _prefer_same_module(
        self,
        candidates: list[FunctionDef],
        caller: FunctionDef,
    ) -> FunctionDef | None:
        """Prefer candidates that live in the same module as the caller."""
        caller_module = self._module_map.get(caller.file_path, "")
        for func in candidates:
            func_module = self._module_map.get(func.file_path, "")
            if func_module == caller_module:
                return func
        return None

    def _extract_assignment_types(
        self,
        func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Infer simple local/self attribute types from constructor-style assignments."""
        local_types: dict[str, str] = {}
        self_attr_types: dict[str, str] = {}
        body = getattr(func_node, "body", None)
        if not isinstance(body, list):
            return local_types, self_attr_types
        for child in body:
            self._visit_type_assignments(child, local_types, self_attr_types)
        return local_types, self_attr_types

    def _visit_type_assignments(
        self,
        node: ast.AST,
        local_types: dict[str, str],
        self_attr_types: dict[str, str],
    ) -> None:
        """Collect constructor-style type hints while skipping nested scopes."""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return

        if isinstance(node, ast.Assign):
            inferred_type = self._infer_assigned_type(node.value)
            if inferred_type:
                for target in node.targets:
                    self._record_assignment_type(target, inferred_type, local_types, self_attr_types)
        elif isinstance(node, ast.AnnAssign):
            inferred_type = self._infer_assigned_type(node.value) if node.value else None
            if inferred_type:
                self._record_assignment_type(node.target, inferred_type, local_types, self_attr_types)

        for child in ast.iter_child_nodes(node):
            self._visit_type_assignments(child, local_types, self_attr_types)

    def _record_assignment_type(
        self,
        target: ast.expr,
        inferred_type: str,
        local_types: dict[str, str],
        self_attr_types: dict[str, str],
    ) -> None:
        """Store inferred types for simple locals and `self.<attr>` assignments."""
        if isinstance(target, ast.Name):
            local_types[target.id] = inferred_type
            return

        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        ):
            self_attr_types[target.attr] = inferred_type

    def _infer_assigned_type(self, value: ast.expr | None) -> str | None:
        """Infer a class name from constructor-style calls like `Foo(...)`."""
        if not isinstance(value, ast.Call):
            return None

        call_name, _, _ = self._get_call_target(value)
        simple_name = call_name.split(".")[-1]
        if simple_name[:1].isupper():
            return simple_name
        return None

    @staticmethod
    def _describe_exception(call: CallSite) -> str:
        """Describe an exception node."""
        if call.raise_status:
            return f"Raise an HTTP {call.raise_status} error response."
        return f"Raise {call.func_name}."

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Collapse docstring whitespace into a single readable line."""
        return " ".join(text.split())

    @staticmethod
    def _humanize_identifier(name: str) -> str:
        """Convert code identifiers like get_current_user into readable text."""
        simple_name = name.split(".")[-1]
        spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", simple_name)
        spaced = spaced.replace("_", " ")
        spaced = re.sub(r"\s+", " ", spaced).strip()
        if not spaced:
            return simple_name
        return spaced[0].upper() + spaced[1:]

    @staticmethod
    def _should_ignore_call(func_name: str) -> bool:
        """Check if a call should be filtered out (framework noise)."""
        if func_name in IGNORE_CALLS:
            return True
        # Check simple name (last part)
        simple = func_name.split(".")[-1] if "." in func_name else func_name
        if simple in IGNORE_CALLS:
            return True
        # Check prefixes
        for prefix in IGNORE_PREFIXES:
            if func_name.startswith(prefix):
                return True
        return False

    @staticmethod
    def _is_low_signal_call(node: ast.Call, is_db: bool, is_http: bool) -> bool:
        """Collapse low-signal value/object helper calls out of the main flow."""
        if is_db or is_http:
            return False
        if not isinstance(node.func, ast.Attribute):
            return False
        if node.func.attr not in LOW_SIGNAL_METHODS:
            return False
        base = node.func.value
        if isinstance(base, ast.Name) and base.id == "self":
            return False
        return True

    @staticmethod
    def _is_db_call(node: ast.Call) -> bool:
        """Heuristic: is this call likely a database operation?"""
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in DB_PATTERNS:
                if isinstance(node.func.value, ast.Name):
                    return node.func.value.id.lower() in DB_OBJECT_HINTS
                return True
        return False

    @staticmethod
    def _is_http_call(node: ast.Call) -> bool:
        """Heuristic: is this call likely an external HTTP request?"""
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in HTTP_PATTERNS:
                if isinstance(node.func.value, ast.Name):
                    return node.func.value.id.lower() in HTTP_OBJECT_HINTS
                return True
        return False

    @classmethod
    def _get_call_target(cls, node: ast.Call) -> tuple[str, tuple[str, ...], bool]:
        """Extract target name plus receiver metadata from a Call node."""
        if isinstance(node.func, ast.Name):
            return node.func.id, (), False
        if isinstance(node.func, ast.Attribute):
            owner_parts = cls._extract_owner_parts(node.func.value)
            func_name = ".".join((*owner_parts, node.func.attr)) if owner_parts else node.func.attr
            return func_name, owner_parts, True
        return "", (), False

    @classmethod
    def _extract_owner_parts(cls, node: ast.expr) -> tuple[str, ...]:
        """Extract the receiver path for attribute and call chains."""
        if isinstance(node, ast.Name):
            return (node.id,)
        if isinstance(node, ast.Attribute):
            parent = cls._extract_owner_parts(node.value)
            if not parent:
                return ()
            return (*parent, node.attr)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                return ()
            if isinstance(node.func, ast.Attribute):
                parent = cls._extract_owner_parts(node.func.value)
                if not parent:
                    return ()
                return (*parent, node.func.attr)
        return ()

    @staticmethod
    def _get_name(node: ast.expr | None) -> str:
        if node is None:
            return ""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""

    @staticmethod
    def _decorator_name(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                return node.func.id
            if isinstance(node.func, ast.Attribute):
                return node.func.attr
        return ""

    @staticmethod
    def _annotation_str(node: ast.expr | None) -> str | None:
        if node is None:
            return None
        try:
            return ast.unparse(node)
        except Exception:
            return None
