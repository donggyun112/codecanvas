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
    references: list[ReferenceSite] = field(default_factory=list)
    docstring: str = ""
    params: list[str] = field(default_factory=list)
    return_annotation: str | None = None
    definition_type: str = "function"  # function | class
    class_qname: str | None = None
    local_types: dict[str, str] = field(default_factory=dict)
    logic_steps: list[LogicStep] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)
    is_protocol: bool = False
    is_abstract: bool = False


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
    db_detail: dict[str, Any] | None = None     # model, operation, chain
    http_detail: dict[str, Any] | None = None   # method, url, etc.
    is_raise: bool = False          # raise SomeException(...)
    raise_status: int | None = None # HTTP status code if HTTPException
    owner_parts: tuple[str, ...] = ()
    is_attribute_call: bool = False
    iteration_kind: str | None = None   # "for" | "async_for"
    loop_target: str | None = None
    loop_iterator: str | None = None


@dataclass
class ReferenceSite:
    """A function reference passed as a value (callback/registration)."""
    func_name: str
    line: int
    container_name: str = ""
    owner_parts: tuple[str, ...] = ()
    is_attribute_ref: bool = False


@dataclass
class CallerReference:
    """A resolved reverse-call relationship into a target function."""
    caller_qualified_name: str
    line: int
    relation: str = "call"
    label: str = ""
    condition: str | None = None
    is_error_path: bool = False


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
def _chain_root_name(node: ast.expr) -> str | None:
    """Walk a method chain to find the root variable name.

    ``self.client.get(...)`` → ``self``
    ``db.query(User).filter_by(...)`` → ``db``
    ``some_call().get(...)`` → None (dynamic root)
    """
    current = node
    while True:
        if isinstance(current, ast.Name):
            return current.id
        if isinstance(current, ast.Attribute):
            current = current.value
            continue
        if isinstance(current, ast.Call):
            if isinstance(current.func, (ast.Name, ast.Attribute)):
                current = current.func
                continue
        return None


def _chain_has_any(node: ast.expr, hints: set[str]) -> bool:
    """Check if any attribute in a method chain matches the hint set."""
    current = node
    while True:
        if isinstance(current, ast.Attribute):
            if current.attr in hints:
                return True
            current = current.value
            continue
        if isinstance(current, ast.Call):
            current = current.func
            continue
        return False


# Methods that strongly indicate a DB call even without a DB root variable.
_DB_CHAIN_HINTS = {
    "table", "query", "select", "insert", "update", "delete",
    "filter", "filter_by", "where", "join", "outerjoin",
    "scalar", "scalars", "from_", "values",
}

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
        self._caller_index: dict[str, list[CallerReference]] | None = None
        self._ast_nodes: dict[str, ast.AST] = {}  # qualified_name -> AST node
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
        self._enrich_logic_step_calls()
        self._analyzed = True

    def get_function(self, qualified_name: str) -> FunctionDef | None:
        """Public accessor for a function definition by qualified name."""
        return self._functions.get(qualified_name)

    def get_ast_node(self, qualified_name: str) -> ast.AST | None:
        """Public accessor for a function's AST node."""
        return self._ast_nodes.get(qualified_name)

    def classify_function(self, func: FunctionDef) -> NodeType:
        """Public accessor for function type classification."""
        return self._classify_function(func)

    def describe_function(self, func: FunctionDef) -> str:
        """Public accessor for function description."""
        return self._describe_function(func)

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
                    references=self._extract_references(node),
                    docstring=ast.get_docstring(node) or "",
                    params=[a.arg for a in node.args.args if a.arg != "self"],
                    return_annotation=self._annotation_str(node.returns),
                    class_qname=class_qname,
                    local_types=local_types,
                    logic_steps=self._extract_logic_steps(node),
                )
                self._functions[qname] = func_def
                self._ast_nodes[qname] = node
                self._name_index.setdefault(node.name, []).append(qname)
                if node.name == "__init__" and class_qname and self_attr_types:
                    self._class_attr_types.setdefault(class_qname, {}).update(self_attr_types)
                # Recurse into function bodies to index nested functions.
                # They may serve as entrypoints (e.g. LangGraph node callbacks),
                # but calls TO them from the parent are filtered in traverse().
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
                # Detect data-object / schema classes (Pydantic, TypedDict, …)
                # whose instantiation is not an architectural flow step.
                _SCHEMA_BASES = {
                    "BaseModel", "TypedDict", "BaseSettings",
                    "NamedTuple", "Schema", "SQLModel",
                    "pydantic.BaseModel", "pydantic.BaseSettings",
                }
                base_names = {
                    ast.unparse(b) if hasattr(ast, "unparse") else getattr(b, "id", "")
                    for b in node.bases
                }
                is_schema = bool(base_names & _SCHEMA_BASES)
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
                    definition_type="schema" if is_schema else "class",
                    class_qname=class_qname,
                    bases=[self._annotation_str(base) or self._get_name(base) for base in node.bases],
                    is_protocol=self._is_protocol_class(node),
                    is_abstract=self._is_abstract_class(node),
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

    def _extract_references(self, func_node: ast.AST) -> list[ReferenceSite]:
        """Extract function references passed as values within a function body."""
        refs: list[ReferenceSite] = []
        body = getattr(func_node, "body", None)
        if isinstance(body, list):
            for child in body:
                self._visit_references(child, refs)
        else:
            self._visit_references(func_node, refs)

        deduped: list[ReferenceSite] = []
        seen: set[tuple[str, int, str]] = set()
        for ref in refs:
            key = (ref.func_name, ref.line, ref.container_name)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(ref)
        return deduped

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

        if isinstance(node, (ast.For, ast.AsyncFor)):
            iter_call = self._iterator_call_signature(node.iter)
            self._visit_calls(node.iter, calls, branch_ctx)
            if iter_call:
                self._annotate_iteration_call(
                    calls=calls,
                    func_name=iter_call[0],
                    line=iter_call[1],
                    kind="async_for" if isinstance(node, ast.AsyncFor) else "for",
                    target=ast.unparse(node.target) if hasattr(ast, "unparse") else self._get_name(node.target),
                    iterator=self._expr_summary(node.iter),
                )
            for child in node.body:
                self._visit_calls(child, calls, branch_ctx)
            for child in getattr(node, "orelse", []):
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
                    db_detail=self._extract_db_detail(node) if is_db else None,
                    http_detail=self._extract_http_detail(node) if is_http else None,
                    owner_parts=owner_parts,
                    is_attribute_call=is_attribute_call,
                ))

        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            if calls and calls[-1].line == node.value.lineno:
                calls[-1].is_await = True

        # Recurse into children
        for child in ast.iter_child_nodes(node):
            self._visit_calls(child, calls, branch_ctx)

    def _iterator_call_signature(self, expr: ast.AST) -> tuple[str, int] | None:
        """Return the outer iterator call in a for/async-for expression."""
        if isinstance(expr, ast.Await):
            return self._iterator_call_signature(expr.value)
        if isinstance(expr, ast.Call):
            func_name, _, _ = self._get_call_target(expr)
            if func_name:
                return func_name, expr.lineno
        return None

    @staticmethod
    def _annotate_iteration_call(
        calls: list[CallSite],
        func_name: str,
        line: int,
        kind: str,
        target: str,
        iterator: str,
    ) -> None:
        """Tag the iterator source call for a for/async-for loop."""
        for call in reversed(calls):
            if call.line != line or call.func_name != func_name:
                continue
            call.iteration_kind = kind
            call.loop_target = target
            call.loop_iterator = iterator
            return

    def _visit_references(self, node: ast.AST, refs: list[ReferenceSite]) -> None:
        """Collect callback/reference-style function usages while skipping nested scopes."""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return

        if isinstance(node, ast.Call):
            container_name, _, _ = self._get_call_target(node)
            for arg in node.args:
                self._collect_reference_expr(arg, refs, node.lineno, container_name)
            for kw in node.keywords:
                self._collect_reference_expr(kw.value, refs, node.lineno, container_name)
            self._visit_references(node.func, refs)
            return

        for child in ast.iter_child_nodes(node):
            self._visit_references(child, refs)

    def _collect_reference_expr(
        self,
        expr: ast.AST | None,
        refs: list[ReferenceSite],
        line: int,
        container_name: str,
    ) -> None:
        """Collect bare callable references nested in an expression tree."""
        if expr is None:
            return
        if isinstance(expr, ast.Name):
            refs.append(ReferenceSite(
                func_name=expr.id,
                line=line,
                container_name=container_name,
            ))
            return
        if isinstance(expr, ast.Attribute):
            owner_parts = self._extract_owner_parts(expr.value)
            func_name = ".".join((*owner_parts, expr.attr)) if owner_parts else expr.attr
            refs.append(ReferenceSite(
                func_name=func_name,
                line=line,
                container_name=container_name,
                owner_parts=owner_parts,
                is_attribute_ref=True,
            ))
            return
        if isinstance(expr, ast.Call):
            for arg in expr.args:
                self._collect_reference_expr(arg, refs, line, container_name)
            for kw in expr.keywords:
                self._collect_reference_expr(kw.value, refs, line, container_name)
            return
        if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
            for item in expr.elts:
                self._collect_reference_expr(item, refs, line, container_name)
            return
        if isinstance(expr, ast.Dict):
            for key in expr.keys:
                self._collect_reference_expr(key, refs, line, container_name)
            for value in expr.values:
                self._collect_reference_expr(value, refs, line, container_name)
            return

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
        caller_depth: int = 0,
        mark_context_root: bool = False,
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

        def ensure_function_node(func: FunctionDef) -> str:
            nonlocal edge_counter
            if func.qualified_name in nodes:
                return func.qualified_name

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
            )
            nodes[node.id] = node
            edge_counter = self._add_logic_nodes(func, node.id, nodes, edges, edge_counter)
            return node.id

        def traverse(func: FunctionDef, depth: int, parent_id: str | None = None) -> str:
            nonlocal edge_counter
            ensure_function_node(func)
            if depth > max_depth or func.qualified_name in visited:
                return func.qualified_name
            visited.add(func.qualified_name)
            node = nodes[func.qualified_name]
            existing_depth = node.metadata.get("downstream_distance")
            if existing_depth is None or depth < existing_depth:
                node.metadata["downstream_distance"] = depth

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
                        metadata={
                            "function_id": node.id,
                            **({"status_code": status} if status else {}),
                        },
                    )
                    edge_counter += 1
                    edges.append(FlowEdge(
                        id=f"e{edge_counter}",
                        source_id=self._find_matching_logic_node(call, node.id, nodes) or node.id,
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
                    # Skip schema/DTO constructors — data object assembly is not
                    # an architectural flow step (e.g. User(...), UserIdentity(...)).
                    if target_func.definition_type == "schema":
                        continue
                    # Skip nested functions of the current function — they are
                    # closures / helpers that are implementation details.
                    if target_func.qualified_name.startswith(func.qualified_name + "."):
                        continue
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
                        label=self._call_edge_label(call, target_func),
                        confidence=Confidence.DEFINITE,
                        evidence=[Evidence(
                            source="static_analysis",
                            file_path=func.file_path,
                            line_number=call.line,
                            detail=f"Call to {call.func_name} at line {call.line}",
                        )],
                        metadata=self._call_edge_metadata(call, target_func),
                        condition=call.branch_condition,
                        is_error_path=call.in_branch in ("except",),
                    ))
                else:
                    # Only create stub nodes for semantically meaningful unresolved
                    # calls (database / HTTP).  Plain external library calls like
                    # workflow.add_node or llm.invoke produce noise without value.
                    if not call.is_db_call and not call.is_http_call:
                        continue

                    stub_id = f"unresolved.{call.func_name}"
                    stub_type = NodeType.DATABASE if call.is_db_call \
                        else NodeType.EXTERNAL_API

                    detail = call.db_detail or call.http_detail or {}
                    stub_display = self._stub_display_name(call)
                    stub_label = self._stub_edge_label(call)

                    if stub_id not in nodes:
                        nodes[stub_id] = FlowNode(
                            id=stub_id,
                            node_type=stub_type,
                            name=call.func_name,
                            display_name=stub_display,
                            description=self._describe_unresolved_call(call),
                            confidence=Confidence.INFERRED,
                            evidence=[Evidence(
                                source="static_analysis",
                                file_path=func.file_path,
                                line_number=call.line,
                                detail=f"Unresolved call to {call.func_name}",
                            )],
                            level=3,
                            metadata=detail,
                        )
                    edge_counter += 1
                    edges.append(FlowEdge(
                        id=f"e{edge_counter}",
                        source_id=node.id,
                        target_id=stub_id,
                        edge_type=(
                            EdgeType.QUERIES if call.is_db_call
                            else EdgeType.REQUESTS
                        ),
                        label=stub_label,
                        confidence=Confidence.INFERRED,
                        evidence=[Evidence(
                            source="static_analysis",
                            file_path=func.file_path,
                            line_number=call.line,
                            detail=f"Unresolved: {call.func_name}",
                        )],
                        metadata={**self._call_edge_metadata(call), **detail},
                        condition=call.branch_condition,
                        is_error_path=call.in_branch in ("except",),
                    ))

            return node.id

        traverse(handler_func, 0)
        if mark_context_root:
            nodes[handler_func.qualified_name].metadata["context_root"] = True

        if caller_depth > 0:
            def ensure_caller_node(func: FunctionDef) -> str:
                """Like ensure_function_node but skips L4 logic steps.

                Callers are shown as context (who calls me?) so their
                internal logic steps are irrelevant and add visual noise.
                """
                if func.qualified_name in nodes:
                    return func.qualified_name
                ensure_function_node(func)
                # Remove any logic nodes that were just added for this caller.
                to_remove = [
                    nid for nid, n in nodes.items()
                    if n.level == 4
                    and n.metadata.get("function_id") == func.qualified_name
                ]
                for nid in to_remove:
                    del nodes[nid]
                # Drop edges that referenced those logic nodes.
                edges[:] = [
                    e for e in edges
                    if e.source_id not in to_remove and e.target_id not in to_remove
                ]
                return func.qualified_name

            edge_counter = self._add_caller_context(
                handler_func=handler_func,
                remaining_depth=caller_depth,
                nodes=nodes,
                edges=edges,
                edge_counter=edge_counter,
                ensure_function_node=ensure_caller_node,
                seen={handler_func.qualified_name},
                distance=1,
            )
        return nodes, edges

    def _add_caller_context(
        self,
        handler_func: FunctionDef,
        remaining_depth: int,
        nodes: dict[str, FlowNode],
        edges: list[FlowEdge],
        edge_counter: int,
        ensure_function_node,
        seen: set[str],
        distance: int,
    ) -> int:
        """Attach a bounded upstream caller context above a selected function."""
        if remaining_depth <= 0:
            return edge_counter

        for caller, call in self._get_callers(handler_func):
            ensure_function_node(caller)
            caller_node = nodes[caller.qualified_name]
            existing_distance = caller_node.metadata.get("upstream_distance")
            if existing_distance is None or distance < existing_distance:
                caller_node.metadata["upstream_distance"] = distance
            caller_node.metadata["context_direction"] = "upstream"

            if not any(
                edge.source_id == caller.qualified_name
                and edge.target_id == handler_func.qualified_name
                and edge.metadata.get("upstream_edge")
                for edge in edges
            ):
                edge_counter += 1
                edges.append(FlowEdge(
                    id=f"e{edge_counter}",
                    source_id=caller.qualified_name,
                    target_id=handler_func.qualified_name,
                    edge_type=EdgeType.CALLS,
                    confidence=Confidence.DEFINITE,
                    evidence=[Evidence(
                        source="static_analysis",
                        file_path=caller.file_path,
                        line_number=call.line,
                        detail=(
                            f"Reference to {handler_func.name} at line {call.line}"
                            if call.relation == "reference"
                            else f"Call to {handler_func.name} at line {call.line}"
                        ),
                    )],
                    label=call.label,
                    condition=call.condition,
                    is_error_path=call.is_error_path,
                    metadata={
                        "upstream_edge": True,
                        "call_line": call.line,
                        "upstream_relation": call.relation,
                    },
                ))

            if caller.qualified_name in seen:
                continue
            seen.add(caller.qualified_name)
            edge_counter = self._add_caller_context(
                handler_func=caller,
                remaining_depth=remaining_depth - 1,
                nodes=nodes,
                edges=edges,
                edge_counter=edge_counter,
                ensure_function_node=ensure_function_node,
                seen=seen,
                distance=distance + 1,
            )
        return edge_counter

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

    def _find_matching_logic_node(
        self,
        call: "CallInfo",
        function_node_id: str,
        nodes: dict[str, "FlowNode"],
    ) -> str | None:
        """Find the L4 branch node whose condition matches call.branch_condition.

        Returns the node id if found, or None to fall back to the function node.
        """
        if not call.branch_condition:
            return None
        candidates = [
            node for node in nodes.values()
            if node.level == 4
            and node.node_type == NodeType.BRANCH
            and node.metadata.get("function_id") == function_node_id
            and node.metadata.get("condition") == call.branch_condition
        ]
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0].id
        # Multiple branches with the same condition — pick the one whose
        # line range contains the raise statement.
        for node in candidates:
            line_start = node.line_start or 0
            line_end = node.line_end or line_start
            if line_start <= call.line <= line_end:
                return node.id
        # Ambiguous — fall back to function node rather than misrouting
        return None

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

    def _get_callers(self, target: FunctionDef) -> list[tuple[FunctionDef, CallerReference]]:
        """Return resolved callers of ``target``, one representative call per caller."""
        self._build_caller_index()
        refs = self._caller_index.get(target.qualified_name, []) if self._caller_index else []
        callers: list[tuple[FunctionDef, CallerReference]] = []
        for ref in refs:
            caller = self._functions.get(ref.caller_qualified_name)
            if caller is None:
                continue
            callers.append((caller, ref))
        callers.sort(key=lambda item: (item[0].file_path, item[1].line, item[0].qualified_name))
        return callers

    def _build_caller_index(self) -> None:
        """Build a reverse call index after all functions are known."""
        if self._caller_index is not None:
            return

        index: dict[str, dict[str, CallerReference]] = {}
        for caller in self._functions.values():
            if caller.definition_type == "class":
                continue
            for call in caller.calls:
                target = self._resolve_call(call, caller)
                if target is None:
                    continue
                per_target = index.setdefault(target.qualified_name, {})
                existing = per_target.get(caller.qualified_name)
                if existing is None or call.line < existing.line:
                    per_target[caller.qualified_name] = CallerReference(
                        caller_qualified_name=caller.qualified_name,
                        line=call.line,
                        relation="call",
                        condition=call.branch_condition,
                        is_error_path=call.in_branch in ("except",),
                    )
            for ref in caller.references:
                target = self._resolve_reference(ref, caller)
                if target is None:
                    continue
                per_target = index.setdefault(target.qualified_name, {})
                existing = per_target.get(caller.qualified_name)
                if existing is None or ref.line < existing.line:
                    per_target[caller.qualified_name] = CallerReference(
                        caller_qualified_name=caller.qualified_name,
                        line=ref.line,
                        relation="reference",
                        label=f"via {ref.container_name}" if ref.container_name else "function reference",
                    )

        self._caller_index = {
            target_qname: sorted(
                refs.values(),
                key=lambda ref: (ref.line, ref.caller_qualified_name),
            )
            for target_qname, refs in index.items()
        }

    def _resolve_reference(self, ref: ReferenceSite, caller: FunctionDef) -> FunctionDef | None:
        """Resolve a function reference used as a value, not a direct call."""
        synthetic = CallSite(
            func_name=ref.func_name,
            line=ref.line,
            owner_parts=ref.owner_parts,
            is_attribute_call=ref.is_attribute_ref,
        )
        return self._resolve_call(synthetic, caller)

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

    def resolve_type_definition(self, type_name: str, from_file: str | None = None) -> FunctionDef | None:
        """Resolve a class / protocol / abstract type name to its definition."""
        self.analyze_project()
        simple_name = self._normalize_type_name(type_name)
        if not simple_name:
            return None

        candidates = [
            self._functions[qname]
            for qname in self._name_index.get(simple_name, [])
            if self._functions[qname].definition_type in {"class", "schema"}
        ]
        if not candidates:
            return None
        if from_file:
            same_file = [func for func in candidates if func.file_path == from_file]
            if same_file:
                return same_file[0]
        return candidates[0]

    def resolve_bound_implementation(
        self,
        contract_type: str,
        provider_func: FunctionDef | None,
        from_file: str | None = None,
    ) -> FunctionDef | None:
        """Resolve the concrete implementation bound to a contract/provider pair."""
        self.analyze_project()

        inferred = self._infer_provider_return_type(provider_func)
        if inferred:
            resolved = self.resolve_type_definition(inferred, from_file=from_file)
            if resolved and self._normalize_type_name(resolved.name) != self._normalize_type_name(contract_type):
                return resolved

        contract_name = self._normalize_type_name(contract_type)
        if not contract_name:
            return None

        implementations = self.find_implementations(contract_name, from_file=from_file)
        if len(implementations) == 1:
            return implementations[0]
        return None

    def find_implementations(
        self,
        contract_type: str,
        from_file: str | None = None,
    ) -> list[FunctionDef]:
        """Return classes that explicitly inherit from the given contract."""
        self.analyze_project()
        contract_name = self._normalize_type_name(contract_type)
        if not contract_name:
            return []

        implementations = [
            func for func in self._functions.values()
            if func.definition_type == "class"
            and self._normalize_type_name(func.name) != contract_name
            and any(self._normalize_type_name(base) == contract_name for base in func.bases)
        ]
        if from_file:
            same_file = [func for func in implementations if func.file_path == from_file]
            if same_file:
                return same_file
        return implementations

    def resolve_method_on_type_name(
        self,
        type_name: str,
        method_name: str,
        from_file: str | None = None,
    ) -> FunctionDef | None:
        """Resolve a method on an explicit type name without a caller context."""
        self.analyze_project()
        normalized = self._normalize_type_name(type_name)
        if not normalized:
            return None
        candidates = [
            self._functions[qname]
            for qname in self._name_index.get(method_name, [])
            if self._normalize_type_name(self._functions[qname].class_name or "") == normalized
        ]
        if not candidates:
            return None
        if from_file:
            same_file = [func for func in candidates if func.file_path == from_file]
            if same_file:
                return same_file[0]
        return candidates[0]

    @staticmethod
    def _normalize_type_name(type_name: str | None) -> str:
        """Reduce annotations like Optional[RepoPort] to RepoPort."""
        if not type_name:
            return ""
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", type_name):
            if token and token[0].isupper() and token not in {"Optional", "Annotated", "Union"}:
                return token
        return type_name.split(".")[-1]

    def _infer_provider_return_type(self, provider_func: FunctionDef | None) -> str | None:
        """Infer a concrete class returned by a dependency/provider function."""
        if provider_func is None:
            return None
        node = self._ast_nodes.get(provider_func.qualified_name)
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return None

        for child in ast.walk(node):
            if not isinstance(child, ast.Return) or child.value is None:
                continue
            value = child.value
            if isinstance(value, ast.Name):
                inferred = provider_func.local_types.get(value.id)
                if inferred:
                    return inferred
            if isinstance(value, ast.Call):
                func_name, _, _ = self._get_call_target(value)
                if not func_name:
                    continue
                resolved = self.resolve_type_definition(func_name, from_file=provider_func.file_path)
                if resolved and resolved.definition_type in {"class", "schema"}:
                    return resolved.name
                normalized = self._normalize_type_name(func_name)
                if normalized and normalized[:1].isupper():
                    return normalized
        return None

    def _extract_logic_steps(
        self,
        func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> list[LogicStep]:
        """Summarize top-level statements inside a function body."""
        steps: list[LogicStep] = []
        for stmt in func_node.body:
            steps.extend(self._flatten_stmt(stmt))
        return steps

    def _enrich_logic_step_calls(self) -> None:
        """Attach resolved call targets to each summarized logic step."""
        for func in self._functions.values():
            if not func.logic_steps or not func.calls:
                continue
            for step in func.logic_steps:
                targets = self._logic_step_call_targets(func, step)
                if targets:
                    step.metadata["call_targets"] = targets

    def _logic_step_call_targets(self, func: FunctionDef, step: LogicStep) -> list[dict[str, Any]]:
        """Return resolved project call targets that belong to one logic step."""
        step_start = step.line
        step_end = step.line_end or step.line
        targets: list[dict[str, Any]] = []
        seen: set[str] = set()

        for call in func.calls:
            if call.line < step_start or call.line > step_end:
                continue
            target = self._resolve_call(call, func)
            if target is None or target.definition_type == "class":
                continue
            if target.qualified_name in seen:
                continue
            seen.add(target.qualified_name)
            targets.append({
                "label": call.func_name,
                "qualified_name": target.qualified_name,
                "file_path": target.file_path,
                "line_start": target.line_start,
                "node_type": self._classify_function(target).value,
                "call_kind": self._call_edge_metadata(call, target).get("call_kind"),
                "is_await": call.is_await,
            })

        return targets

    def _flatten_stmt(self, stmt: ast.stmt) -> list[LogicStep]:
        """Return LogicStep(s) for a statement.

        try/except blocks are transparent — their body is flattened into the
        parent list so inner branches and assignments remain visible.  Each
        except handler is represented as a BRANCH step.
        """
        _try_types = (ast.Try,) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())
        if isinstance(stmt, ast.If):
            return self._expand_if_chain(stmt)
        if isinstance(stmt, _try_types):
            return self._expand_try_block(stmt)
        step = self._logic_step_from_statement(stmt)
        return [step] if step is not None else []

    def _expand_try_block(self, stmt: ast.Try) -> list[LogicStep]:
        """Flatten try body + represent except handlers as BRANCH steps."""
        steps: list[LogicStep] = []
        for s in stmt.body:
            steps.extend(self._flatten_stmt(s))
        for handler in stmt.handlers:
            exc_name = self._get_name(handler.type) if handler.type else "Exception"
            body_summary = self._summarize_block(handler.body)
            steps.append(LogicStep(
                node_type=NodeType.BRANCH,
                display_name=f"except {exc_name}",
                description=f"If {exc_name} is raised, {body_summary}.",
                line=handler.lineno,
                line_end=getattr(handler, "end_lineno", handler.lineno),
                metadata={
                    "condition": exc_name,
                    "flow_direction": "branch",
                    "is_exception_handler": True,
                },
            ))
        return steps

    def _expand_if_chain(self, stmt: ast.If) -> list[LogicStep]:
        """Walk an if/elif/else chain and emit one BRANCH LogicStep per clause.

        Each elif becomes its own node with the correct condition, so that
        raise statements inside elif bodies can be rerouted to the right node.
        """
        steps: list[LogicStep] = []
        current: ast.If = stmt
        keyword = "if"
        while True:
            condition = ast.unparse(current.test) if hasattr(ast, "unparse") else "condition"
            body_summary = self._summarize_block(current.body)
            orelse = current.orelse
            is_elif_next = len(orelse) == 1 and isinstance(orelse[0], ast.If)
            # Span only this clause's body so line-range tiebreaking works
            line_end = current.body[-1].end_lineno if current.body else current.lineno
            if is_elif_next:
                description = f"If `{condition}`, {body_summary}."
            else:
                else_summary = self._summarize_block(orelse)
                description = f"If `{condition}`, {body_summary}."
                if else_summary:
                    description += f" Otherwise, {else_summary}."
            steps.append(LogicStep(
                node_type=NodeType.BRANCH,
                display_name=f"{keyword} {self._compact(condition)}",
                description=description,
                line=current.lineno,
                line_end=line_end,
                metadata={"condition": condition, "flow_direction": "branch"},
            ))
            if is_elif_next:
                current = orelse[0]
                keyword = "elif"
            else:
                break
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
                metadata={"target": targets, "value": value, "flow_direction": "sequential"},
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
                metadata={"target": target, "value": value, "flow_direction": "sequential"},
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
                metadata={"target": target, "value": value, "flow_direction": "sequential"},
            )

        if isinstance(stmt, ast.AsyncFor):
            target = ast.unparse(stmt.target) if hasattr(ast, "unparse") else self._get_name(stmt.target)
            iterator = self._expr_summary(stmt.iter)
            iterator_call = self._iterator_call_signature(stmt.iter)
            body_summary = self._summarize_block(stmt.body)
            description = f"Consume async stream from {iterator} as `{target}` and {body_summary}."
            if iterator_call:
                description = (
                    f"Consume async stream from `{iterator_call[0]}` as `{target}` and {body_summary}."
                )
            return LogicStep(
                node_type=NodeType.LOOP,
                display_name=f"async for {target} in {self._compact(iterator)}",
                description=description,
                line=stmt.lineno,
                line_end=stmt.end_lineno,
                metadata={
                    "target": target,
                    "iterator": iterator,
                    "iterator_call": iterator_call[0] if iterator_call else None,
                    "loop_kind": "async_for",
                    "flow_direction": "sequential",
                },
            )

        if isinstance(stmt, ast.For):
            target = ast.unparse(stmt.target) if hasattr(ast, "unparse") else self._get_name(stmt.target)
            iterator = self._expr_summary(stmt.iter)
            iterator_call = self._iterator_call_signature(stmt.iter)
            body_summary = self._summarize_block(stmt.body)
            return LogicStep(
                node_type=NodeType.LOOP,
                display_name=f"for {target} in {self._compact(iterator)}",
                description=f"Loop over {iterator} as `{target}` and {body_summary}.",
                line=stmt.lineno,
                line_end=stmt.end_lineno,
                metadata={
                    "target": target,
                    "iterator": iterator,
                    "iterator_call": iterator_call[0] if iterator_call else None,
                    "loop_kind": "for",
                    "flow_direction": "sequential",
                },
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
                metadata={"condition": condition, "flow_direction": "sequential"},
            )

        if isinstance(stmt, ast.Return):
            value = self._expr_summary(stmt.value)
            return LogicStep(
                node_type=NodeType.RETURN,
                display_name=f"return {self._compact(value)}",
                description=f"Return {value}.",
                line=stmt.lineno,
                line_end=stmt.end_lineno,
                metadata={"value": value, "flow_direction": "sequential"},
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

    @staticmethod
    def _is_protocol_class(node: ast.ClassDef) -> bool:
        base_names = {
            ast.unparse(base) if hasattr(ast, "unparse") else getattr(base, "id", "")
            for base in node.bases
        }
        return any(name.endswith("Protocol") or name == "Protocol" for name in base_names)

    @staticmethod
    def _is_abstract_class(node: ast.ClassDef) -> bool:
        base_names = {
            ast.unparse(base) if hasattr(ast, "unparse") else getattr(base, "id", "")
            for base in node.bases
        }
        if any(name.endswith("ABC") or name == "ABC" for name in base_names):
            return True
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorator_names = {
                ast.unparse(dec) if hasattr(ast, "unparse") else getattr(dec, "id", "")
                for dec in item.decorator_list
            }
            if any(name == "abstractmethod" or name.endswith(".abstractmethod") for name in decorator_names):
                return True
        return False

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

    @staticmethod
    def _stub_display_name(call: CallSite) -> str:
        """Readable display name for a DB/HTTP stub node."""
        if call.db_detail:
            model = call.db_detail.get("model", "")
            op = call.db_detail.get("operation", "")
            table = call.db_detail.get("table", "")
            if model and op:
                return f"{op}({model})"
            if table:
                return f"{op}(\"{table}\")" if op else table
            if op:
                return op
        if call.http_detail:
            method = call.http_detail.get("method", "")
            url = call.http_detail.get("url", call.http_detail.get("url_var", ""))
            if method and url:
                return f"{method} {url}"
            if method:
                return method
        return call.func_name.split(".")[-1]

    @staticmethod
    def _stub_edge_label(call: CallSite) -> str:
        """Short label for a DB/HTTP edge."""
        if call.db_detail:
            model = call.db_detail.get("model")
            op = call.db_detail.get("operation", "")
            if model:
                return f"{op} {model}"
            return op
        if call.http_detail:
            method = call.http_detail.get("method", "")
            url = call.http_detail.get("url", "")
            if method and url:
                return f"{method} {url}"
            return method
        return ""

    def _describe_unresolved_call(self, call: CallSite) -> str:
        """Describe a call target we could not resolve statically."""
        human_name = self._humanize_identifier(call.func_name)
        simple_name = call.func_name.split(".")[-1]

        if call.iteration_kind == "async_for":
            return f"Consume async stream from {human_name}; definition could not be resolved statically."
        if call.is_db_call:
            d = call.db_detail or {}
            model = d.get("model", "")
            op = d.get("operation", simple_name)
            table = d.get("table", "")
            if model:
                return f"Database {op} on {model}."
            if table:
                return f"Database {op} on table \"{table}\"."
            return f"Database operation: {op}."
        if call.is_http_call:
            d = call.http_detail or {}
            method = d.get("method", "")
            url = d.get("url", d.get("url_var", ""))
            if method and url:
                return f"External HTTP {method} request to {url}."
            if method:
                return f"External HTTP {method} request."
            return f"External HTTP call: {human_name}."
        if simple_name[:1].isupper():
            return f"Instantiate or invoke {human_name}; definition could not be resolved statically."
        return f"Call {human_name}; definition could not be resolved statically."

    @staticmethod
    def _call_edge_label(call: CallSite, target_func: FunctionDef | None = None) -> str:
        """Provide a short label for special call semantics."""
        if call.iteration_kind == "async_for":
            return "async stream"
        if call.iteration_kind == "for":
            return "iterator"
        if target_func and target_func.definition_type == "class":
            return "constructs"
        return ""

    @staticmethod
    def _call_edge_metadata(call: CallSite, target_func: FunctionDef | None = None) -> dict[str, Any]:
        """Attach structured metadata for special call semantics."""
        metadata: dict[str, Any] = {}
        if call.iteration_kind:
            metadata["iteration_kind"] = call.iteration_kind
            metadata["loop_target"] = call.loop_target
            metadata["loop_iterator"] = call.loop_iterator
            metadata["call_kind"] = "async_stream" if call.iteration_kind == "async_for" else "iterator"
        elif target_func and target_func.definition_type == "class":
            metadata["call_kind"] = "constructor"
            metadata["constructed_type"] = target_func.name
        return metadata

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

        # Collect parameter type annotations so calls like supervisor.method()
        # can be resolved when the parameter is declared as supervisor: Supervisor.
        all_args = (
            func_node.args.posonlyargs
            + func_node.args.args
            + func_node.args.kwonlyargs
        )
        for arg in all_args:
            if arg.arg == "self" or not arg.annotation:
                continue
            ann = self._annotation_str(arg.annotation)
            if ann and ann[0].isupper():
                local_types[arg.arg] = ann

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
                root = _chain_root_name(node.func.value)
                if root is not None and root.lower() in DB_OBJECT_HINTS:
                    return True
                # Chain calls like client.table().insert().execute():
                # if intermediate methods are DB-specific, it's a DB call.
                if _chain_has_any(node.func.value, _DB_CHAIN_HINTS):
                    return True
        return False

    @staticmethod
    def _is_http_call(node: ast.Call) -> bool:
        """Heuristic: is this call likely an external HTTP request?"""
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in HTTP_PATTERNS:
                root = _chain_root_name(node.func.value)
                if root is not None and root.lower() in HTTP_OBJECT_HINTS:
                    return True
        return False

    @staticmethod
    def _extract_db_detail(node: ast.Call) -> dict[str, Any]:
        """Extract model name, operation, and chain from a DB call.

        Patterns:
        - session.query(User).filter_by(...).first()  → model=User, op=first
        - select(User).where(...)                     → model=User, op=select
        - db.add(user)                                → op=add
        - session.execute(stmt)                        → op=execute
        """
        detail: dict[str, Any] = {}
        if not isinstance(node.func, ast.Attribute):
            return detail

        detail["operation"] = node.func.attr

        # Walk the method chain to find model refs and build chain
        chain: list[str] = [node.func.attr]
        current: ast.expr = node.func.value
        while isinstance(current, ast.Call):
            if isinstance(current.func, ast.Attribute):
                chain.append(current.func.attr)
                # Look for model in first positional arg: query(User), select(User)
                if current.func.attr in ("query", "select") and current.args:
                    arg = current.args[0]
                    if isinstance(arg, ast.Name):
                        detail["model"] = arg.id
                current = current.func.value
            elif isinstance(current.func, ast.Name):
                chain.append(current.func.id)
                if current.func.id in ("select", "insert", "update", "delete") and current.args:
                    arg = current.args[0]
                    if isinstance(arg, ast.Name):
                        detail["model"] = arg.id
                break
            else:
                break

        # Also check direct args: session.add(user_obj), table("users")
        if node.args and node.func.attr in ("add", "delete", "merge", "refresh"):
            arg = node.args[0]
            if isinstance(arg, ast.Name):
                detail["target_var"] = arg.id
        if node.args and node.func.attr == "table":
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                detail["table"] = arg.value

        detail["chain"] = list(reversed(chain))
        return detail

    @staticmethod
    def _extract_http_detail(node: ast.Call) -> dict[str, Any]:
        """Extract HTTP method, URL, and key kwargs from an HTTP call.

        Patterns:
        - client.get("/api/users")          → method=GET, url="/api/users"
        - httpx.post(url, json=payload)     → method=POST, url=<variable>
        - requests.request("GET", url)      → method=GET
        """
        detail: dict[str, Any] = {}
        if not isinstance(node.func, ast.Attribute):
            return detail

        method = node.func.attr.upper()
        if method == "REQUEST" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                method = arg.value.upper()
        if method in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            detail["method"] = method

        # URL from first positional arg
        url_arg = node.args[0] if node.args else None
        if method == "REQUEST" and len(node.args) > 1:
            url_arg = node.args[1]
        if url_arg:
            if isinstance(url_arg, ast.Constant) and isinstance(url_arg.value, str):
                detail["url"] = url_arg.value
            elif isinstance(url_arg, ast.JoinedStr):
                detail["url"] = "<f-string>"
            elif isinstance(url_arg, ast.Name):
                detail["url_var"] = url_arg.id

        # Key kwargs
        for kw in node.keywords:
            if kw.arg in ("json", "data", "params", "headers", "timeout"):
                detail[f"has_{kw.arg}"] = True

        return detail

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
