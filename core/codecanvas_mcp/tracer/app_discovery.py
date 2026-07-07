"""Discover and import the target FastAPI app from a project.

Scans for ``FastAPI()`` instantiation and imports the module to get
a live ASGI app instance.  Searches recursively up to 3 levels deep.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from typing import Any

_EXCLUDE_DIRS = {
    ".git", ".venv", "__pycache__", "node_modules", "venv",
    "migrations", ".tox", ".eggs", "dist", "build", ".mypy_cache",
}


def discover_app(project_root: str) -> Any:
    """Find and return the FastAPI app object from *project_root*."""
    # Activate target project's venv site-packages if available
    _activate_project_venv(project_root)

    # Priority candidates (common patterns)
    priority = [
        "app/main.py", "app.py", "main.py",
        "src/main.py", "src/app.py", "src/api/app.py",
        "backend/main.py", "backend/app.py",
        "api/app.py", "api/main.py",
        "server/app.py", "server/main.py",
    ]

    for rel_path in priority:
        full_path = os.path.join(project_root, rel_path)
        if os.path.isfile(full_path):
            app = _try_import_app(full_path, project_root)
            if app is not None:
                return app

    # Fallback: walk up to 3 levels deep looking for FastAPI instances
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in sorted(dirs) if d not in _EXCLUDE_DIRS]
        depth = root.replace(project_root, "").count(os.sep)
        if depth > 2:
            dirs.clear()
            continue

        for f in sorted(files):
            if not f.endswith(".py"):
                continue
            full_path = os.path.join(root, f)
            # Quick check: does the file even mention FastAPI?
            try:
                with open(full_path, "r", encoding="utf-8") as fh:
                    content = fh.read(4096)
                if "FastAPI" not in content:
                    continue
            except (OSError, UnicodeDecodeError):
                continue

            app = _try_import_app(full_path, project_root)
            if app is not None:
                return app

    last_err = getattr(_try_import_app, '_last_error', '')
    raise RuntimeError(
        f"Could not find a FastAPI app in {project_root}. "
        f"Searched priority paths and all .py files up to 3 levels deep."
        + (f"\nLast import error: {last_err}" if last_err else "")
    )


def _try_import_app(file_path: str, project_root: str) -> Any | None:
    """Try to import a module and find a FastAPI instance in it."""
    rel = os.path.relpath(file_path, project_root)
    module_name = (
        rel.replace(os.sep, ".")
        .removesuffix(".py")
        .removesuffix(".__init__")
    )

    # Add project root and its parent to sys.path
    # (many projects have config/settings at repo root while source is in src/)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    parent = os.path.dirname(project_root)
    if parent and parent not in sys.path:
        sys.path.insert(0, parent)

    try:
        spec = importlib.util.spec_from_file_location(
            module_name, file_path,
            submodule_search_locations=[os.path.dirname(file_path)],
        )
        if spec is None or spec.loader is None:
            return None

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        # Look for FastAPI instance — prefer 'app' attr name
        candidates = []
        for attr_name in dir(module):
            obj = getattr(module, attr_name, None)
            if obj is not None and _is_fastapi_app(obj):
                if attr_name == "app":
                    return obj
                candidates.append(obj)

        if candidates:
            return candidates[0]

    except Exception as exc:
        # Store last error for debugging — caller may raise with details
        _try_import_app._last_error = f"{file_path}: {exc}"
        return None

    return None

_try_import_app._last_error = ""


def _activate_project_venv(project_root: str) -> None:
    """Add the target project's venv site-packages to sys.path."""
    import glob

    # Search project root and parent for .venv or venv
    search_dirs = [project_root, os.path.dirname(project_root)]
    for base in search_dirs:
        for venv_name in (".venv", "venv"):
            venv_dir = os.path.join(base, venv_name)
            if not os.path.isdir(venv_dir):
                continue
            # Find site-packages
            patterns = [
                os.path.join(venv_dir, "lib", "python*", "site-packages"),
                os.path.join(venv_dir, "Lib", "site-packages"),  # Windows
            ]
            for pattern in patterns:
                for sp in glob.glob(pattern):
                    if sp not in sys.path:
                        sys.path.insert(0, sp)
                    return


def _is_fastapi_app(obj: Any) -> bool:
    """Check if an object is a FastAPI (or Starlette) application."""
    for cls in type(obj).__mro__:
        if cls.__name__ in ("FastAPI", "Starlette"):
            return True
    return False
