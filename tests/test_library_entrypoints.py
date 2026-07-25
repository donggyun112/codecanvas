"""Library projects expose their public API as entrypoints.

`list_entrypoints` previously had no concept of a library surface: run against
LangGraph it returned only bench and CI scripts. A distributed package's
`__all__` and `[project.scripts]` are the declarations that actually say where
a library starts.
"""
import textwrap

from codecanvas_mcp.mcp import queries
from codecanvas_mcp.mcp.answers import DEFAULT_CAP
from codecanvas_mcp.mcp.session import get_builder
from codecanvas_mcp.parser.entrypoint_extractor import EntryPointExtractor
from codecanvas_mcp.parser.library_extractor import LibraryExportExtractor


def _write(root, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")


def _package(root, name="demo", scripts: str = "") -> None:
    """A minimal distributed package: pyproject + importable directory."""
    body = f'[project]\nname = "{name}"\nversion = "0.1.0"\n'
    if scripts:
        body += f"\n[project.scripts]\n{scripts}\n"
    _write(root, "pyproject.toml", body)


def _exports(root):
    return LibraryExportExtractor(str(root)).analyze()


def _by_name(entrypoints):
    return {entry.handler_name: entry for entry in entrypoints}


def _monorepo_package(root, name: str, count: int) -> None:
    """A package under libs/ exporting ``count`` public functions."""
    _package(root / "libs" / name, name=name)
    symbols = [f"sym_{index:02d}" for index in range(count)]
    _write(root, f"libs/{name}/{name}/core.py",
           "".join(f"def {s}():\n    pass\n\n" for s in symbols))
    _write(root, f"libs/{name}/{name}/__init__.py",
           f"from {name}.core import {', '.join(symbols)}\n\n__all__ = {symbols!r}\n")


# ----------------------------------------------------------------------
# __all__ as the export declaration
# ----------------------------------------------------------------------

def test_all_declares_the_export_surface(tmp_path):
    _package(tmp_path)
    _write(tmp_path, "demo/core.py", """
        class Engine:
            pass
    """)
    _write(tmp_path, "demo/__init__.py", """
        from demo.core import Engine

        __all__ = ["Engine"]
    """)

    exports = _exports(tmp_path)

    assert [e.handler_name for e in exports] == ["Engine"]
    assert exports[0].kind == "export"
    assert exports[0].metadata["package"] == "demo"


def test_all_may_be_a_tuple(tmp_path):
    _package(tmp_path)
    _write(tmp_path, "demo/core.py", "def run():\n    pass\n")
    _write(tmp_path, "demo/__init__.py", """
        from demo.core import run

        __all__ = ("run",)
    """)

    assert [e.handler_name for e in _exports(tmp_path)] == ["run"]


def test_export_resolves_to_the_defining_module_not_the_init(tmp_path):
    """Anchoring at __init__.py would make call_tree and who_calls useless."""
    _package(tmp_path)
    _write(tmp_path, "demo/core.py", """
        # padding
        # padding
        class Engine:
            pass
    """)
    _write(tmp_path, "demo/__init__.py", """
        from demo.core import Engine

        __all__ = ["Engine"]
    """)

    engine = _exports(tmp_path)[0]

    assert engine.handler_file.endswith("demo/core.py")
    assert engine.handler_line == 3


def test_relative_reexport_is_resolved(tmp_path):
    _package(tmp_path)
    _write(tmp_path, "demo/core.py", "class Engine:\n    pass\n")
    _write(tmp_path, "demo/__init__.py", """
        from .core import Engine

        __all__ = ["Engine"]
    """)

    assert _exports(tmp_path)[0].handler_file.endswith("demo/core.py")


def test_reexport_chain_is_followed(tmp_path):
    _package(tmp_path)
    _write(tmp_path, "demo/deep/impl.py", "class Engine:\n    pass\n")
    _write(tmp_path, "demo/deep/__init__.py", "from demo.deep.impl import Engine\n")
    _write(tmp_path, "demo/__init__.py", """
        from demo.deep import Engine

        __all__ = ["Engine"]
    """)

    assert _exports(tmp_path)[0].handler_file.endswith("demo/deep/impl.py")


def test_names_that_resolve_to_no_callable_are_dropped(tmp_path):
    """Constants like START/END have no definition worth anchoring."""
    _package(tmp_path)
    _write(tmp_path, "demo/constants.py", 'START = "__start__"\n')
    _write(tmp_path, "demo/core.py", "def run():\n    pass\n")
    _write(tmp_path, "demo/__init__.py", """
        from demo.constants import START
        from demo.core import run

        __all__ = ["START", "run"]
    """)

    assert [e.handler_name for e in _exports(tmp_path)] == ["run"]


# ----------------------------------------------------------------------
# Class exports carry every callable anchor
# ----------------------------------------------------------------------

def test_exported_class_is_one_row_carrying_its_public_methods(tmp_path):
    _package(tmp_path)
    _write(tmp_path, "demo/core.py", """
        class Engine:
            def __init__(self):
                pass

            def compile(self):
                pass

            def _internal(self):
                pass
    """)
    _write(tmp_path, "demo/__init__.py", """
        from demo.core import Engine

        __all__ = ["Engine"]
    """)

    exports = _exports(tmp_path)

    assert len(exports) == 1
    candidates = {c["name"] for c in exports[0].metadata["handler_candidates"]}
    assert candidates == {"__init__", "compile"}


def test_exported_function_is_its_own_only_anchor(tmp_path):
    _package(tmp_path)
    _write(tmp_path, "demo/core.py", "def run():\n    pass\n")
    _write(tmp_path, "demo/__init__.py", """
        from demo.core import run

        __all__ = ["run"]
    """)

    candidates = _exports(tmp_path)[0].metadata["handler_candidates"]

    assert [c["name"] for c in candidates] == ["run"]


# ----------------------------------------------------------------------
# Inference fallback when __all__ is absent
# ----------------------------------------------------------------------

def test_public_init_names_are_used_when_all_is_absent(tmp_path):
    _package(tmp_path)
    _write(tmp_path, "demo/core.py", """
        def run():
            pass

        def _helper():
            pass
    """)
    _write(tmp_path, "demo/__init__.py", """
        from demo.core import run, _helper

        def local():
            pass

        def _private():
            pass
    """)

    names = {e.handler_name for e in _exports(tmp_path)}

    assert names == {"run", "local"}


# ----------------------------------------------------------------------
# The distributed-package gate
# ----------------------------------------------------------------------

def test_a_repo_without_pyproject_yields_no_exports(tmp_path):
    """Regression guard: application repos must be untouched by inference."""
    _write(tmp_path, "app/__init__.py", "from app.core import handler\n")
    _write(tmp_path, "app/core.py", "def handler():\n    pass\n")

    assert _exports(tmp_path) == []


def test_monorepo_packages_are_discovered_independently(tmp_path):
    _package(tmp_path / "libs" / "alpha", name="alpha")
    _write(tmp_path, "libs/alpha/alpha/core.py", "def a():\n    pass\n")
    _write(tmp_path, "libs/alpha/alpha/__init__.py",
           "from alpha.core import a\n\n__all__ = ['a']\n")
    _package(tmp_path / "libs" / "beta", name="beta")
    _write(tmp_path, "libs/beta/beta/core.py", "def b():\n    pass\n")
    _write(tmp_path, "libs/beta/beta/__init__.py",
           "from beta.core import b\n\n__all__ = ['b']\n")

    packages = {e.metadata["package"] for e in _exports(tmp_path)}

    assert packages == {"alpha", "beta"}


def test_example_packages_are_skipped(tmp_path):
    _package(tmp_path / "examples" / "sample", name="sample")
    _write(tmp_path, "examples/sample/sample/core.py", "def demo():\n    pass\n")
    _write(tmp_path, "examples/sample/sample/__init__.py",
           "from sample.core import demo\n\n__all__ = ['demo']\n")
    _package(tmp_path / "pkg", name="real")
    _write(tmp_path, "pkg/real/core.py", "def go():\n    pass\n")
    _write(tmp_path, "pkg/real/__init__.py",
           "from real.core import go\n\n__all__ = ['go']\n")

    assert {e.metadata["package"] for e in _exports(tmp_path)} == {"real"}


def test_a_venv_inside_the_package_root_is_not_scanned(tmp_path):
    """LangGraph keeps a .venv inside every libs/* package root."""
    _package(tmp_path)
    _write(tmp_path, "demo/core.py", "def go():\n    pass\n")
    _write(tmp_path, "demo/__init__.py", "from demo.core import go\n\n__all__ = ['go']\n")
    _write(tmp_path, ".venv/lib/python3.12/site-packages/vendor/__init__.py",
           "__all__ = ['leaked']\n")

    assert [e.handler_name for e in _exports(tmp_path)] == ["go"]


# ----------------------------------------------------------------------
# Console scripts
# ----------------------------------------------------------------------

def test_project_scripts_become_script_entrypoints(tmp_path):
    _package(tmp_path, scripts='demo-cli = "demo.cli:main"')
    _write(tmp_path, "demo/cli.py", """
        # padding
        def main():
            pass
    """)
    _write(tmp_path, "demo/__init__.py", "")

    scripts = [e for e in _exports(tmp_path) if e.kind == "script"]

    assert len(scripts) == 1
    assert scripts[0].handler_name == "main"
    assert scripts[0].handler_file.endswith("demo/cli.py")
    assert scripts[0].handler_line == 2
    assert "demo-cli" in scripts[0].label


# ----------------------------------------------------------------------
# Integration with the existing extractor
# ----------------------------------------------------------------------

def test_exports_survive_alongside_main_guard_scripts(tmp_path):
    """A junk __main__ guard must no longer hide the library surface."""
    _package(tmp_path)
    _write(tmp_path, "demo/core.py", "def go():\n    pass\n")
    _write(tmp_path, "demo/__init__.py", "from demo.core import go\n\n__all__ = ['go']\n")
    _write(tmp_path, "bench/run.py", """
        def bench():
            pass

        if __name__ == "__main__":
            bench()
    """)

    entrypoints = EntryPointExtractor(str(tmp_path)).analyze()
    kinds = [e.kind for e in entrypoints]

    assert "export" in kinds
    assert "script" in kinds
    assert kinds.index("export") < kinds.index("script")


def test_every_package_is_represented_before_the_output_cap(tmp_path):
    """Concatenating packages let the first few consume every slot."""
    for package in ("alpha", "beta", "gamma"):
        _monorepo_package(tmp_path, package, count=30)

    entrypoints = EntryPointExtractor(str(tmp_path)).analyze()
    head = {e.metadata["package"] for e in entrypoints[:DEFAULT_CAP]}

    assert head == {"alpha", "beta", "gamma"}


def test_truncated_output_names_the_packages_it_dropped(tmp_path):
    """A cap that hides two thirds of the surface must say what to ask for."""
    for package in ("alpha", "beta", "gamma"):
        _monorepo_package(tmp_path, package, count=30)

    out = queries.list_entrypoints(get_builder(str(tmp_path)))

    assert out["count"] == 90
    assert len(out["entrypoints"]) == DEFAULT_CAP
    assert "alpha (30)" in out["note"]
    assert "beta (30)" in out["note"]
    assert "filter" in out["note"]


def test_a_truncated_symbol_is_still_reachable_by_filter(tmp_path):
    for package in ("alpha", "beta", "gamma"):
        _monorepo_package(tmp_path, package, count=30)

    out = queries.list_entrypoints(get_builder(str(tmp_path)), filter="sym_29")

    names = {e["handler"] for e in out["entrypoints"]}
    assert names == {"sym_29"}
    assert out["count"] == 3


def test_uncapped_output_has_no_package_inventory_note(tmp_path):
    _monorepo_package(tmp_path, "alpha", count=3)

    out = queries.list_entrypoints(get_builder(str(tmp_path)))

    assert out.get("note") is None


def test_application_project_entrypoints_are_unchanged(tmp_path):
    """No pyproject: the extractor must behave exactly as before."""
    _write(tmp_path, "run.py", """
        def main():
            pass

        if __name__ == "__main__":
            main()
    """)

    entrypoints = EntryPointExtractor(str(tmp_path)).analyze()

    assert [(e.kind, e.handler_name) for e in entrypoints] == [("script", "main")]


def test_main_guard_ignores_timers_constructors_and_nested_helpers(tmp_path):
    _write(tmp_path, "run.py", """
        from argparse import ArgumentParser
        from time import perf_counter

        class TracerProvider:
            pass

        def main():
            pass

        if __name__ == "__main__":
            started = perf_counter()
            parser = ArgumentParser()
            tracer = TracerProvider()
            print(perf_counter())
            main()
    """)

    scripts = [
        entry
        for entry in EntryPointExtractor(str(tmp_path)).analyze()
        if entry.kind == "script"
    ]

    assert [(entry.handler_name, entry.handler_line) for entry in scripts] == [
        ("main", 7),
    ]


def test_main_guard_accepts_known_launcher_without_local_function(tmp_path):
    _write(tmp_path, "run.py", """
        import uvicorn

        if __name__ == "__main__":
            uvicorn.run("app:app")
    """)

    scripts = [
        entry
        for entry in EntryPointExtractor(str(tmp_path)).analyze()
        if entry.kind == "script"
    ]

    assert [entry.handler_name for entry in scripts] == ["run"]


def test_references_directory_is_excluded_from_entrypoint_discovery(tmp_path):
    _write(tmp_path, "app.py", """
        def main():
            pass

        if __name__ == "__main__":
            main()
    """)
    _write(tmp_path, "references/copied.py", """
        def copied_main():
            pass

        if __name__ == "__main__":
            copied_main()
    """)

    scripts = [
        entry.handler_name
        for entry in EntryPointExtractor(str(tmp_path)).analyze()
        if entry.kind == "script"
    ]

    assert scripts == ["main"]
