"""Extract FastAPI routes, dependencies, and handler chains from source code.

Uses Python's ast module to statically analyze FastAPI applications.
Identifies: routes, Depends() injections, middleware, exception handlers.
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codecanvas.graph.models import Endpoint, Evidence


# FastAPI/Starlette HTTP method decorators
ROUTE_DECORATORS = {"get", "post", "put", "delete", "patch", "options", "head", "trace"}
ROUTER_CLASSES = {"FastAPI", "APIRouter"}


@dataclass
class ImportInfo:
    """Tracks an import statement."""
    module: str           # e.g. "fastapi"
    name: str             # e.g. "FastAPI"
    alias: str | None     # e.g. "app" in "from fastapi import FastAPI as app"
    file_path: str = ""
    line: int = 0


@dataclass
class RouterInstance:
    """A discovered FastAPI/APIRouter instance."""
    var_name: str         # Variable name (e.g. "app", "router")
    class_name: str       # "FastAPI" or "APIRouter"
    file_path: str = ""
    line: int = 0
    prefix: str = ""      # APIRouter prefix
    tags: list[str] = field(default_factory=list)
    include_targets: list[str] = field(default_factory=list)  # app.include_router(xxx)


@dataclass
class DependencyCall:
    """A Depends() call found in a route handler."""
    func_name: str
    param_name: str = ""
    declared_type: str | None = None
    file_path: str = ""
    line: int = 0
    is_class: bool = False
    resolved_file_path: str | None = None
    resolved_line: int | None = None


@dataclass
class MiddlewareInfo:
    """A middleware registered on the app."""
    class_name: str
    file_path: str = ""
    line: int = 0
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExceptionHandlerInfo:
    """An exception handler registered on the app."""
    exception_class: str
    handler_name: str
    file_path: str = ""
    line: int = 0


class FastAPIExtractor:
    """Extract FastAPI application structure from a Python project."""

    def __init__(self, project_root: str):
        self.project_root = Path(project_root)
        self.imports: dict[str, list[ImportInfo]] = {}      # file -> imports
        self.routers: list[RouterInstance] = []
        self.endpoints: list[Endpoint] = []
        self.dependencies: dict[str, list[DependencyCall]] = {}  # handler_key -> deps
        self.middlewares: list[MiddlewareInfo] = []
        self.exception_handlers: list[ExceptionHandlerInfo] = []
        self._file_asts: dict[str, ast.Module] = {}
        self._router_vars: dict[str, RouterInstance] = {}   # var_name -> router (may conflict)
        self._router_by_file: dict[str, RouterInstance] = {}  # file_path -> router

    @staticmethod
    def dependency_key(handler_name: str, file_path: str) -> str:
        """Stable key for route handler dependency lookup."""
        return f"{file_path}:{handler_name}"

    def analyze(self) -> list[Endpoint]:
        """Run full analysis on the project. Returns discovered endpoints."""
        python_files = self._find_python_files()

        # Pass 1: Parse all files, collect imports and router instances
        for fpath in python_files:
            self._parse_file(fpath)

        # Pass 2: Extract routes, dependencies, middleware
        for fpath in python_files:
            tree = self._file_asts.get(fpath)
            if tree is None:
                continue
            self._extract_routers(tree, fpath)

        # Pass 3: Now that we know all routers, extract routes
        for fpath in python_files:
            tree = self._file_asts.get(fpath)
            if tree is None:
                continue
            self._extract_routes(tree, fpath)
            self._extract_middleware(tree, fpath)
            self._extract_exception_handlers(tree, fpath)

        # Resolve router includes (app.include_router)
        self._resolve_router_includes()
        self._sync_api_entrypoints()

        return self.endpoints

    def _find_python_files(self) -> list[str]:
        """Find all .py files in the project, excluding common non-source dirs."""
        exclude = {".venv", "venv", "node_modules", "__pycache__", ".git",
                   "migrations", ".tox", ".eggs", "dist", "build"}
        result = []
        for root, dirs, files in os.walk(self.project_root):
            dirs[:] = [d for d in dirs if d not in exclude]
            for f in files:
                if f.endswith(".py"):
                    result.append(os.path.join(root, f))
        return result

    def _parse_file(self, file_path: str) -> None:
        """Parse a Python file and extract imports."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source, filename=file_path)
            self._file_asts[file_path] = tree
            self._extract_imports(tree, file_path)
        except (SyntaxError, UnicodeDecodeError):
            pass

    def _extract_imports(self, tree: ast.Module, file_path: str) -> None:
        """Extract import statements from AST."""
        file_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    file_imports.append(ImportInfo(
                        module=alias.name,
                        name=alias.name.split(".")[-1],
                        alias=alias.asname,
                        file_path=file_path,
                        line=node.lineno,
                    ))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    file_imports.append(ImportInfo(
                        module=module,
                        name=alias.name,
                        alias=alias.asname,
                        file_path=file_path,
                        line=node.lineno,
                    ))
        self.imports[file_path] = file_imports

    def _extract_routers(self, tree: ast.Module, file_path: str) -> None:
        """Find FastAPI() and APIRouter() instantiations."""
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Call):
                continue

            call = node.value
            class_name = self._get_call_name(call)
            if class_name not in ROUTER_CLASSES:
                continue

            for target in node.targets:
                if isinstance(target, ast.Name):
                    prefix = ""
                    tags = []
                    for kw in call.keywords:
                        if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                            prefix = kw.value.value
                        elif kw.arg == "tags" and isinstance(kw.value, ast.List):
                            tags = [
                                elt.value for elt in kw.value.elts
                                if isinstance(elt, ast.Constant)
                            ]

                    router = RouterInstance(
                        var_name=target.id,
                        class_name=class_name,
                        file_path=file_path,
                        line=node.lineno,
                        prefix=prefix,
                        tags=tags,
                    )
                    self.routers.append(router)
                    self._router_vars[target.id] = router
                    self._router_by_file[file_path] = router

    def _extract_routes(self, tree: ast.Module, file_path: str) -> None:
        """Extract route decorators like @app.get('/path')."""
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            for decorator in node.decorator_list:
                endpoint = self._parse_route_decorator(decorator, node, file_path)
                if endpoint:
                    # Extract Depends() from function parameters
                    deps = self._extract_depends(node, file_path)
                    endpoint.dependencies = [d.func_name for d in deps]
                    self.dependencies[self.dependency_key(endpoint.handler_name, endpoint.handler_file)] = deps
                    self.endpoints.append(endpoint)

    def _parse_route_decorator(
        self,
        decorator: ast.expr,
        func_node: ast.FunctionDef | ast.AsyncFunctionDef,
        file_path: str,
    ) -> Endpoint | None:
        """Parse a single decorator to see if it's a route decorator."""
        # @app.get("/path") or @router.post("/path")
        if not isinstance(decorator, ast.Call):
            return None
        if not isinstance(decorator.func, ast.Attribute):
            return None

        attr = decorator.func
        method = attr.attr.upper()
        if attr.attr not in ROUTE_DECORATORS:
            return None

        # Check that the object is a known router
        router_var = None
        if isinstance(attr.value, ast.Name):
            router_var = attr.value.id

        # Extract path from first argument
        path = ""
        if decorator.args and isinstance(decorator.args[0], ast.Constant):
            path = str(decorator.args[0].value)

        # Extract keyword arguments
        response_model = None
        tags: list[str] = []
        description = ""
        for kw in decorator.keywords:
            if kw.arg == "response_model":
                response_model = self._get_name(kw.value)
            elif kw.arg == "tags" and isinstance(kw.value, ast.List):
                tags = [
                    elt.value for elt in kw.value.elts
                    if isinstance(elt, ast.Constant)
                ]
            elif kw.arg == "description" and isinstance(kw.value, ast.Constant):
                description = str(kw.value.value)

        # Prepend router prefix if available — use file-scoped lookup
        # to avoid collision when multiple files use the same var name
        if router_var and file_path in self._router_by_file:
            router = self._router_by_file[file_path]
            path = router.prefix + path
            tags = tags or router.tags

        return Endpoint(
            method=method,
            path=path,
            handler_name=func_node.name,
            handler_file=file_path,
            handler_line=func_node.lineno,
            tags=tags,
            response_model=response_model,
            description=description or ast.get_docstring(func_node) or "",
        )

    def _extract_depends(
        self,
        func_node: ast.FunctionDef | ast.AsyncFunctionDef,
        file_path: str,
    ) -> list[DependencyCall]:
        """Extract Depends() calls from function parameters."""
        deps = []
        positional = func_node.args.args
        positional_defaults: list[ast.expr | None] = (
            [None] * (len(positional) - len(func_node.args.defaults))
            + list(func_node.args.defaults)
        )

        for arg, default in zip(positional, positional_defaults):
            dep = self._extract_param_dependency(arg, default, file_path)
            if dep:
                deps.append(dep)

        for arg, default in zip(func_node.args.kwonlyargs, func_node.args.kw_defaults):
            dep = self._extract_param_dependency(arg, default, file_path)
            if dep:
                deps.append(dep)
        return deps

    def _extract_param_dependency(
        self,
        arg: ast.arg,
        default: ast.expr | None,
        file_path: str,
    ) -> DependencyCall | None:
        """Extract a dependency with parameter/type context preserved."""
        declared_type = self._declared_type(arg.annotation)
        if arg.annotation is not None:
            dep = self._parse_depends_annotation(
                arg.annotation,
                file_path,
                param_name=arg.arg,
                declared_type=declared_type,
            )
            if dep:
                return dep
        if default is not None:
            dep = self._parse_depends_call(
                default,
                file_path,
                param_name=arg.arg,
                declared_type=declared_type,
            )
            if dep:
                return dep
        return None

    def _parse_depends_annotation(
        self,
        annotation: ast.expr,
        file_path: str,
        param_name: str = "",
        declared_type: str | None = None,
    ) -> DependencyCall | None:
        """Check if annotation is Annotated[type, Depends(func)]."""
        if not isinstance(annotation, ast.Subscript):
            return None
        # Annotated[SomeType, Depends(func)]
        if not (isinstance(annotation.value, ast.Name)
                and annotation.value.id == "Annotated"):
            return None
        if not isinstance(annotation.slice, ast.Tuple):
            return None
        for elt in annotation.slice.elts[1:]:
            dep = self._parse_depends_call(
                elt,
                file_path,
                param_name=param_name,
                declared_type=declared_type,
            )
            if dep:
                return dep
        return None

    def _parse_depends_call(
        self,
        node: ast.expr,
        file_path: str,
        param_name: str = "",
        declared_type: str | None = None,
    ) -> DependencyCall | None:
        """Parse a Depends(func) call."""
        if not isinstance(node, ast.Call):
            return None
        call_name = self._get_call_name(node)
        if call_name != "Depends":
            return None
        if not node.args:
            return None
        func_ref = node.args[0]
        func_name = self._get_name(func_ref)
        if not func_name and isinstance(func_ref, ast.Call):
            func_name = self._get_name(func_ref.func)
        if func_name:
            resolved_file_path, resolved_line = self._resolve_symbol_definition(func_name, file_path)
            return DependencyCall(
                func_name=func_name,
                param_name=param_name,
                declared_type=declared_type,
                file_path=file_path,
                line=node.lineno,
                is_class=isinstance(func_ref, ast.Call),
                resolved_file_path=resolved_file_path,
                resolved_line=resolved_line,
            )
        return None

    def _declared_type(self, annotation: ast.expr | None) -> str | None:
        """Extract the declared runtime type for a dependency parameter."""
        if annotation is None:
            return None
        if (
            isinstance(annotation, ast.Subscript)
            and isinstance(annotation.value, ast.Name)
            and annotation.value.id == "Annotated"
            and isinstance(annotation.slice, ast.Tuple)
            and annotation.slice.elts
        ):
            return self._expr_text(annotation.slice.elts[0])
        return self._expr_text(annotation)

    def _expr_text(self, node: ast.expr) -> str | None:
        """Best-effort text form for annotations and type expressions."""
        if hasattr(ast, "unparse"):
            try:
                return ast.unparse(node)
            except Exception:
                pass
        return self._get_name(node) or None

    def _extract_middleware(self, tree: ast.Module, file_path: str) -> None:
        """Extract app.add_middleware() calls."""
        for node in ast.walk(tree):
            if not isinstance(node, ast.Expr):
                continue
            if not isinstance(node.value, ast.Call):
                continue
            call = node.value
            if not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr != "add_middleware":
                continue
            if not call.args:
                continue

            middleware_class = self._get_name(call.args[0])
            if middleware_class:
                self.middlewares.append(MiddlewareInfo(
                    class_name=middleware_class,
                    file_path=file_path,
                    line=node.lineno,
                ))

    def _extract_exception_handlers(self, tree: ast.Module, file_path: str) -> None:
        """Extract @app.exception_handler() decorators."""
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                if not isinstance(dec.func, ast.Attribute):
                    continue
                if dec.func.attr != "exception_handler":
                    continue
                if not dec.args:
                    continue
                exc_class = self._get_name(dec.args[0])
                if exc_class:
                    self.exception_handlers.append(ExceptionHandlerInfo(
                        exception_class=exc_class,
                        handler_name=node.name,
                        file_path=file_path,
                        line=node.lineno,
                    ))

    def _resolve_router_includes(self) -> None:
        """Find app.include_router(router) calls and resolve prefixes.

        Handles patterns like:
          app.include_router(auth.router, prefix="/api/v1")
          app.include_router(router, prefix="/api/v1")
        """
        for file_path, tree in self._file_asts.items():
            file_imports = self.imports.get(file_path, [])

            for node in ast.walk(tree):
                if not isinstance(node, ast.Expr):
                    continue
                if not isinstance(node.value, ast.Call):
                    continue
                call = node.value
                if not isinstance(call.func, ast.Attribute):
                    continue
                if call.func.attr != "include_router":
                    continue
                if not call.args:
                    continue

                included_name = self._get_name(call.args[0])
                if not included_name:
                    continue

                # Extract prefix from kwargs
                include_prefix = ""
                for kw in call.keywords:
                    if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                        include_prefix = kw.value.value

                if not include_prefix:
                    continue

                # Resolve which router file this refers to
                # Case 1: "auth.router" -> module_name="auth", find import for "auth"
                # Case 2: "router" -> direct variable
                matched_router = None

                if "." in included_name:
                    # e.g. "auth.router" -> find the module "auth" in imports
                    module_alias = included_name.split(".")[0]
                    for imp in file_imports:
                        actual_name = imp.alias or imp.name
                        if actual_name == module_alias:
                            # Found: `from app.routers import auth` or `import auth`
                            # The router is in the file matching imp.name
                            for router in self.routers:
                                router_base = os.path.basename(router.file_path).removesuffix(".py")
                                if router_base == imp.name:
                                    matched_router = router
                                    break
                            break
                else:
                    # Direct variable reference
                    matched_router = self._router_vars.get(included_name)

                if matched_router is None:
                    continue

                # Apply prefix to all endpoints from this router's file
                for ep in self.endpoints:
                    if ep.handler_file == matched_router.file_path:
                        if not ep.path.startswith(include_prefix):
                            ep.path = include_prefix + ep.path

    def _sync_api_entrypoints(self) -> None:
        """Refresh derived labels after path prefixes have been resolved."""
        for ep in self.endpoints:
            ep.label = f"{ep.method} {ep.path}".strip()
            ep.trigger = f"HTTP {ep.method} {ep.path}".strip()
            ep.id = f"api:{ep.method}:{ep.path}"

    def _resolve_symbol_definition(
        self, symbol_name: str, from_file: str,
    ) -> tuple[str | None, int | None]:
        """Resolve a symbol to the file/line where it is defined, when possible."""
        simple_name = symbol_name.split(".")[-1]

        local_line = self._find_definition_in_file(from_file, simple_name)
        if local_line is not None:
            return from_file, local_line

        imports = self.imports.get(from_file, [])
        symbol_parts = symbol_name.split(".")
        import_alias = symbol_parts[0]
        member_name = symbol_parts[-1]

        for imp in imports:
            actual_name = imp.alias or imp.name
            if actual_name not in {simple_name, import_alias}:
                continue
            for candidate_file in self._candidate_import_files(imp):
                line = self._find_definition_in_file(candidate_file, member_name)
                if line is not None:
                    return candidate_file, line

        return self._find_project_definition(simple_name)

    def _candidate_import_files(self, imp: ImportInfo) -> list[str]:
        """Return possible files that an import may refer to."""
        candidates: list[str] = []

        if imp.module:
            module_plus_name = self._module_to_file(f"{imp.module}.{imp.name}")
            if module_plus_name is not None:
                candidates.append(module_plus_name)
            module_file = self._module_to_file(imp.module)
            if module_file is not None:
                candidates.append(module_file)
        else:
            import_file = self._module_to_file(imp.name)
            if import_file is not None:
                candidates.append(import_file)

        seen: set[str] = set()
        return [path for path in candidates if not (path in seen or seen.add(path))]

    def _module_to_file(self, module_name: str) -> str | None:
        """Resolve a dotted module path inside the project to a Python source file."""
        if not module_name:
            return None

        module_path = self.project_root / module_name.replace(".", os.sep)
        file_candidate = module_path.with_suffix(".py")
        if file_candidate.exists():
            return str(file_candidate)

        init_candidate = module_path / "__init__.py"
        if init_candidate.exists():
            return str(init_candidate)

        return None

    def _find_definition_in_file(self, file_path: str, symbol_name: str) -> int | None:
        """Find the line number for a top-level or nested symbol definition in a file."""
        tree = self._file_asts.get(file_path)
        if tree is None:
            return None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == symbol_name:
                    return node.lineno
        return None

    def _find_project_definition(self, symbol_name: str) -> tuple[str | None, int | None]:
        """Best-effort project-wide lookup for a uniquely named symbol."""
        matches: list[tuple[str, int]] = []
        for file_path in self._file_asts:
            line = self._find_definition_in_file(file_path, symbol_name)
            if line is not None:
                matches.append((file_path, line))
        if len(matches) == 1:
            return matches[0]
        return None, None

    # --- AST utility methods ---

    @staticmethod
    def _get_call_name(call: ast.Call) -> str:
        """Get the function name from a Call node."""
        if isinstance(call.func, ast.Name):
            return call.func.id
        if isinstance(call.func, ast.Attribute):
            return call.func.attr
        return ""

    @staticmethod
    def _get_name(node: ast.expr) -> str:
        """Get a string name from various AST node types."""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parts = []
            current = node
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
            return ".".join(reversed(parts))
        if isinstance(node, ast.Constant):
            return str(node.value)
        return ""
