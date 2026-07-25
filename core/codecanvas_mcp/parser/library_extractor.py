"""Extract a library's public API surface as entry points.

An application announces where it starts with HTTP routes and ``__main__``
guards. A library announces it with packaging metadata and ``__all__`` — and
those declarations were previously ignored, so running ``list_entrypoints``
against a library returned only its bench and CI scripts.

Only directories that declare a distribution (a ``pyproject.toml`` with
``[project].name``) are scanned, so repositories without one behave exactly as
before.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from itertools import zip_longest
import os
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from codecanvas_mcp.graph.models import EntryPoint

# Kept in sync with EntryPointExtractor._find_python_files. Applied during the
# walk, not after: virtualenvs live *inside* package roots in real monorepos.
EXCLUDED_DIRS = {
    ".venv", "venv", "node_modules", "__pycache__", ".git",
    "migrations", ".tox", ".eggs", "dist", "build",
}

_FIXTURE_DIR_NAMES = {"examples"}
_FIXTURE_DIR_SUFFIXES = ("-example", "-examples", "_example", "_examples")
_TEST_DIR_NAMES = {"tests", "test"}

# Depth limit for following ``from x import y`` chains between __init__ files.
_MAX_REEXPORT_DEPTH = 5


@dataclass
class _Package:
    """A distribution found in the tree, plus its module lookup table."""

    name: str
    root: Path
    scripts: dict[str, str] = field(default_factory=dict)
    modules: dict[str, Path] = field(default_factory=dict)


class LibraryExportExtractor:
    """Discover distributed packages and turn their public surface into entrypoints."""

    def __init__(self, project_root: str):
        self.project_root = Path(project_root)
        self._asts: dict[Path, ast.Module] = {}

    def analyze(self) -> list[EntryPoint]:
        # Sorted by distribution name so output never depends on filesystem walk
        # order, shortest first: in a monorepo that is the umbrella package.
        packages = sorted(self._discover_packages(), key=lambda p: (len(p.name), p.name))

        groups = []
        for package in packages:
            rows = self._console_scripts(package) + self._exports(package)
            if rows:
                groups.append(rows)

        # Interleave across distributions. The output cap is small relative to a
        # monorepo's surface, and concatenating would let the first few packages
        # consume every slot — LangGraph's `create_react_agent` fell outside the
        # cap entirely that way.
        interleaved = [
            row for column in zip_longest(*groups)
            for row in column if row is not None
        ]
        return _deduplicate(interleaved)

    # ------------------------------------------------------------------
    # Package discovery
    # ------------------------------------------------------------------

    def _discover_packages(self) -> list[_Package]:
        packages: list[_Package] = []
        for pyproject in self._iter_pyprojects():
            try:
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            project = data.get("project")
            if not isinstance(project, dict):
                continue
            name = project.get("name")
            if not isinstance(name, str) or not name:
                continue

            scripts = {
                key: value
                for key, value in (project.get("scripts") or {}).items()
                if isinstance(value, str)
            }
            for group in (project.get("entry-points") or {}).values():
                if isinstance(group, dict):
                    scripts.update(
                        {k: v for k, v in group.items() if isinstance(v, str)}
                    )

            root = pyproject.parent
            packages.append(
                _Package(name=name, root=root, scripts=scripts,
                         modules=self._module_index(root))
            )
        return packages

    def _iter_pyprojects(self) -> list[Path]:
        found: list[Path] = []
        for directory, subdirs, files in os.walk(self.project_root):
            subdirs[:] = [d for d in subdirs if not _is_skipped_dir(d)]
            if "pyproject.toml" in files:
                found.append(Path(directory) / "pyproject.toml")
        return found

    def _module_index(self, package_root: Path) -> dict[str, Path]:
        """Map dotted module names to files, so imports can be followed."""
        index: dict[str, Path] = {}
        for directory, subdirs, files in os.walk(package_root):
            subdirs[:] = [d for d in subdirs if not _is_skipped_dir(d)]
            for filename in files:
                if not filename.endswith(".py"):
                    continue
                path = Path(directory) / filename
                dotted = _dotted_name(path, package_root)
                if dotted:
                    index.setdefault(dotted, path)
        return index

    # ------------------------------------------------------------------
    # Console scripts
    # ------------------------------------------------------------------

    def _console_scripts(self, package: _Package) -> list[EntryPoint]:
        results: list[EntryPoint] = []
        for script_name, target in sorted(package.scripts.items()):
            module_name, _, attribute = target.partition(":")
            attribute = attribute.split(".")[0].strip()
            if not module_name or not attribute:
                continue
            file_path = package.modules.get(module_name.strip())
            if file_path is None:
                continue
            node = self._find_definition(file_path, attribute)
            if node is None:
                continue

            relative = os.path.relpath(file_path, self.project_root)
            results.append(EntryPoint(
                kind="script",
                group="Scripts",
                label=f"{script_name} ({package.name})",
                trigger=f"Console script: {script_name}",
                path=relative,
                handler_name=node.name,
                handler_file=str(file_path),
                handler_line=node.lineno,
                tags=[package.name, "console_script"],
                description=(
                    ast.get_docstring(node)
                    or f"Console script `{script_name}` from `{relative}`."
                ),
                metadata={
                    "package": package.name,
                    "console_script": script_name,
                    "handler_candidates": self._handler_candidates(file_path, node),
                },
            ))
        return results

    # ------------------------------------------------------------------
    # Exported public surface
    # ------------------------------------------------------------------

    def _exports(self, package: _Package) -> list[EntryPoint]:
        """Exports, shallowest declaring package first.

        The output cap is small relative to a large library's surface, so the
        order decides what an agent actually sees. A name declared in the
        distribution's root ``__init__.py`` is its headline API; one declared
        four levels down is a detail.
        """
        collected: list[tuple[int, str, str, EntryPoint]] = []
        for dotted, path in package.modules.items():
            if path.name != "__init__.py" or _has_test_component(path, package.root):
                continue
            tree = self._parse(path)
            if tree is None:
                continue

            names = _declared_all(tree)
            if names is None:
                names = _inferred_public_names(tree)
            depth = dotted.count(".")
            for name in names:
                entry = self._export_entry(package, path, name, dotted)
                if entry is not None:
                    collected.append((depth, dotted, name, entry))

        # Case-insensitive: otherwise every CamelCase class outranks the
        # snake_case factory functions a user is just as likely to want.
        collected.sort(key=lambda item: (item[0], item[1], item[2].lower()))
        return [entry for _depth, _dotted, _name, entry in collected]

    def _export_entry(
        self, package: _Package, init_path: Path, name: str, declared_in: str,
    ) -> EntryPoint | None:
        resolved = self._resolve(package, init_path, name, depth=0)
        if resolved is None:
            # A constant, a TypedDict, or a name we cannot follow. Anchoring it
            # at __init__.py would point call_tree at the wrong place.
            return None
        file_path, node = resolved

        relative = os.path.relpath(file_path, self.project_root)
        kind_word = "class" if isinstance(node, ast.ClassDef) else "function"
        return EntryPoint(
            kind="export",
            group="Exports",
            label=f"{package.name}.{name}",
            trigger=f"Export: {name}",
            path=relative,
            handler_name=name,
            handler_file=str(file_path),
            handler_line=node.lineno,
            tags=[package.name, "export"],
            description=(
                ast.get_docstring(node)
                or f"Public {kind_word} `{name}` exported by `{package.name}`."
            ),
            metadata={
                "package": package.name,
                "declared_in": declared_in,
                "export_kind": kind_word,
                "handler_candidates": self._handler_candidates(file_path, node),
            },
        )

    # ------------------------------------------------------------------
    # Name resolution
    # ------------------------------------------------------------------

    def _resolve(
        self, package: _Package, file_path: Path, name: str, depth: int,
    ) -> tuple[Path, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef] | None:
        """Follow re-exports until the defining class or function is reached."""
        if depth > _MAX_REEXPORT_DEPTH:
            return None
        tree = self._parse(file_path)
        if tree is None:
            return None

        node = _top_level_definition(tree, name)
        if node is not None:
            return file_path, node

        for statement in tree.body:
            if not isinstance(statement, ast.ImportFrom):
                continue
            original = _imported_as(statement, name)
            if original is None:
                continue
            target = _import_target(statement, file_path, package.root)
            if target is None:
                continue
            next_path = package.modules.get(target)
            if next_path is None or next_path == file_path:
                continue
            found = self._resolve(package, next_path, original, depth + 1)
            if found is not None:
                return found
        return None

    def _find_definition(self, file_path: Path, name: str):
        tree = self._parse(file_path)
        return _top_level_definition(tree, name) if tree is not None else None

    def _handler_candidates(self, file_path: Path, node) -> list[dict]:
        """Every callable an impact analysis should treat as this entrypoint.

        A class contributes its constructor and public methods, so a change to
        ``Pregel.stream`` still reports the ``Pregel`` export as affected.
        """
        if not isinstance(node, ast.ClassDef):
            return [{"name": node.name, "file": str(file_path), "line": node.lineno}]

        # One entry per method name, keeping the last definition: a name that
        # repeats is an @overload chain, and only the final def has a body.
        by_name: dict[str, dict] = {}
        for child in node.body:
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if child.name.startswith("_") and child.name != "__init__":
                continue
            by_name[child.name] = {
                "name": child.name, "file": str(file_path), "line": child.lineno,
            }
        return list(by_name.values())

    def _parse(self, file_path: Path) -> ast.Module | None:
        if file_path in self._asts:
            return self._asts[file_path]
        try:
            tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
            return None
        self._asts[file_path] = tree
        return tree


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------

def _is_skipped_dir(name: str) -> bool:
    if name in EXCLUDED_DIRS or name in _FIXTURE_DIR_NAMES:
        return True
    return name.endswith(_FIXTURE_DIR_SUFFIXES)


def _has_test_component(path: Path, package_root: Path) -> bool:
    try:
        relative = path.relative_to(package_root)
    except ValueError:
        return False
    return any(part in _TEST_DIR_NAMES for part in relative.parts[:-1])


def _dotted_name(path: Path, package_root: Path) -> str | None:
    """Dotted module name for ``path``, honoring a ``src/`` layout."""
    try:
        parts = list(path.relative_to(package_root).parts)
    except ValueError:
        return None
    if parts and parts[0] == "src":
        parts = parts[1:]
    if not parts:
        return None
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][: -len(".py")]
    return ".".join(parts) if parts else None


def _declared_all(tree: ast.Module) -> list[str] | None:
    """Names in ``__all__``, or None when the module does not declare one."""
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            continue
        if node.value is None:
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError):
            return []
        if isinstance(value, (list, tuple, set)):
            return [item for item in value if isinstance(item, str)]
        return []
    return None


def _inferred_public_names(tree: ast.Module) -> list[str]:
    """Public names an ``__init__.py`` binds, when it declares no ``__all__``."""
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                if bound != "*" and not bound.startswith("_"):
                    names.append(bound)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                names.append(node.name)
    return names


def _top_level_definition(tree: ast.Module, name: str):
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return node
    return None


def _imported_as(statement: ast.ImportFrom, name: str) -> str | None:
    """Return the original name if ``statement`` binds ``name``."""
    for alias in statement.names:
        if (alias.asname or alias.name) == name:
            return alias.name
    return None


def _import_target(
    statement: ast.ImportFrom, file_path: Path, package_root: Path,
) -> str | None:
    """Absolute dotted module an ``ImportFrom`` refers to."""
    if not statement.level:
        return statement.module

    current = _dotted_name(file_path, package_root)
    if current is None:
        return None
    parts = current.split(".") if current else []
    if file_path.name != "__init__.py":
        parts = parts[:-1]
    for _ in range(statement.level - 1):
        if not parts:
            return None
        parts = parts[:-1]
    if statement.module:
        parts = parts + statement.module.split(".")
    return ".".join(parts) if parts else None


def _deduplicate(entrypoints: list[EntryPoint]) -> list[EntryPoint]:
    """Nested pyproject files can surface the same symbol twice."""
    seen: set[tuple[str, str, int]] = set()
    unique: list[EntryPoint] = []
    for entry in entrypoints:
        key = (entry.handler_file, entry.handler_name, entry.handler_line)
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique
