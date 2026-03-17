"""Extract generic Python entry points from a project.

Combines FastAPI endpoint discovery with non-HTTP entry points such as:
- script entrypoints guarded by ``if __name__ == "__main__"``
- public top-level functions as a fallback when no stronger trigger exists
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

from codecanvas.graph.models import EntryPoint
from codecanvas.parser.fastapi_extractor import FastAPIExtractor


class EntryPointExtractor:
    """Extract API and non-API execution entry points from a Python project."""

    def __init__(self, project_root: str, fastapi_extractor: FastAPIExtractor | None = None):
        self.project_root = Path(project_root)
        self.fastapi = fastapi_extractor or FastAPIExtractor(project_root)
        self._file_asts: dict[str, ast.Module] = {}

    def analyze(self) -> list[EntryPoint]:
        """Return all discovered entry points."""
        api_entrypoints = list(self.fastapi.analyze())
        python_files = self._find_python_files()
        for file_path in python_files:
            self._parse_file(file_path)

        script_entrypoints = self._extract_script_entrypoints(api_entrypoints)
        function_entrypoints = self._extract_function_fallbacks(
            api_entrypoints,
            script_entrypoints,
        )

        return api_entrypoints + script_entrypoints + function_entrypoints

    def _find_python_files(self) -> list[str]:
        exclude = {
            ".venv", "venv", "node_modules", "__pycache__", ".git",
            "migrations", ".tox", ".eggs", "dist", "build",
        }
        result: list[str] = []
        for root, dirs, files in os.walk(self.project_root):
            dirs[:] = [d for d in dirs if d not in exclude]
            for filename in files:
                if filename.endswith(".py"):
                    result.append(os.path.join(root, filename))
        return result

    def _parse_file(self, file_path: str) -> None:
        if file_path in self._file_asts:
            return
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                source = handle.read()
            self._file_asts[file_path] = ast.parse(source, filename=file_path)
        except (SyntaxError, UnicodeDecodeError):
            return

    def _extract_script_entrypoints(self, api_entrypoints: list[EntryPoint]) -> list[EntryPoint]:
        seen: set[tuple[str, str, int]] = {
            (entry.handler_file, entry.handler_name, entry.handler_line)
            for entry in api_entrypoints
        }
        results: list[EntryPoint] = []

        for file_path, tree in self._file_asts.items():
            top_level_functions = self._top_level_functions(tree)
            functions_by_name = {node.name: node for node in top_level_functions}
            for target_name, trigger_line in self._main_guard_targets(tree):
                target_node = functions_by_name.get(target_name)
                handler_line = target_node.lineno if target_node else trigger_line
                key = (file_path, target_name, handler_line)
                if key in seen:
                    continue
                seen.add(key)

                rel_path = os.path.relpath(file_path, self.project_root)
                docstring = ast.get_docstring(target_node) if target_node else ""
                description = docstring or f"Run script entrypoint from `{rel_path}`."
                results.append(EntryPoint(
                    kind="script",
                    group="Scripts",
                    label=f"python {rel_path}",
                    trigger=f"Script: {rel_path}",
                    path=rel_path,
                    handler_name=target_name,
                    handler_file=file_path,
                    handler_line=handler_line,
                    description=description,
                    metadata={"trigger_line": trigger_line},
                ))

        return sorted(results, key=lambda entry: (entry.handler_file, entry.handler_line, entry.label))

    def _extract_function_fallbacks(
        self,
        api_entrypoints: list[EntryPoint],
        script_entrypoints: list[EntryPoint],
    ) -> list[EntryPoint]:
        """Expose top-level functions when the project has no stronger trigger model."""
        if api_entrypoints or script_entrypoints:
            return []

        seen: set[tuple[str, str, int]] = set()
        results: list[EntryPoint] = []
        for file_path, tree in self._file_asts.items():
            rel_path = os.path.relpath(file_path, self.project_root)
            for node in self._top_level_functions(tree):
                if node.name.startswith("_"):
                    continue
                key = (file_path, node.name, node.lineno)
                if key in seen:
                    continue
                seen.add(key)
                description = ast.get_docstring(node) or f"Trace `{node.name}()` from `{rel_path}`."
                results.append(EntryPoint(
                    kind="function",
                    group="Functions",
                    label=f"{node.name}()",
                    trigger=f"Function: {node.name}()",
                    path=rel_path,
                    handler_name=node.name,
                    handler_file=file_path,
                    handler_line=node.lineno,
                    description=description,
                ))
        return sorted(results, key=lambda entry: (entry.handler_file, entry.handler_line, entry.label))

    @staticmethod
    def _top_level_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
        return [
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]

    def _main_guard_targets(self, tree: ast.Module) -> list[tuple[str, int]]:
        targets: list[tuple[str, int]] = []
        for node in tree.body:
            if not isinstance(node, ast.If) or not self._is_main_guard(node.test):
                continue
            for stmt in node.body:
                for call in ast.walk(stmt):
                    if not isinstance(call, ast.Call):
                        continue
                    call_name = self._call_name(call)
                    if not call_name or call_name in {"print", "SystemExit", "exit"}:
                        continue
                    targets.append((call_name.split(".")[-1], call.lineno))
                    break
                if targets:
                    break
        return targets

    @staticmethod
    def _is_main_guard(test: ast.AST) -> bool:
        if not isinstance(test, ast.Compare):
            return False
        if not isinstance(test.left, ast.Name) or test.left.id != "__name__":
            return False
        if len(test.ops) != 1 or len(test.comparators) != 1:
            return False
        if not isinstance(test.ops[0], ast.Eq):
            return False
        comparator = test.comparators[0]
        return isinstance(comparator, ast.Constant) and comparator.value == "__main__"

    @staticmethod
    def _call_name(call: ast.Call) -> str:
        if isinstance(call.func, ast.Name):
            return call.func.id
        if isinstance(call.func, ast.Attribute):
            parts: list[str] = []
            current: ast.AST = call.func
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
            return ".".join(reversed(parts))
        return ""
