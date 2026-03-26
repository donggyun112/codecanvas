"""CFG correctness tests: verify basic blocks, edges, and control flow semantics."""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from codecanvas.graph.cfg import CFGBuilder, ControlFlowGraph
from codecanvas.parser.call_graph import CallGraphBuilder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_files(project_root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        target = project_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def _build_cfg(project_root: Path, func_name: str, file_name: str = "app.py") -> ControlFlowGraph:
    cg = CallGraphBuilder(str(project_root))
    builder = CFGBuilder(cg)
    return builder.build(func_name, file_name)


def _validate_cfg(cfg: ControlFlowGraph) -> None:
    """Structural invariants that must hold for every CFG."""
    block_ids = {b.id for b in cfg.blocks}
    edge_targets = {e.target_block_id for e in cfg.edges}
    edge_sources = {e.source_block_id for e in cfg.edges}

    for b in cfg.blocks:
        if b.kind == "entry":
            assert b.id not in edge_targets, f"Entry block {b.id} is an edge target"
        elif b.kind != "error_exit":
            assert b.id in edge_targets, f"Block {b.id} [{b.kind}] '{b.label}' has no incoming edges"

    for e in cfg.edges:
        assert e.source_block_id in block_ids, f"Edge {e.id} has dangling source {e.source_block_id}"
        assert e.target_block_id in block_ids, f"Edge {e.id} has dangling target {e.target_block_id}"

    # No exact duplicate edges (same source, target, kind)
    seen = set()
    for e in cfg.edges:
        key = (e.source_block_id, e.target_block_id, e.kind)
        assert key not in seen, f"Duplicate edge: {key}"
        seen.add(key)


def _edges_from(cfg, block_id):
    return [e for e in cfg.edges if e.source_block_id == block_id]

def _edges_to(cfg, block_id):
    return [e for e in cfg.edges if e.target_block_id == block_id]

def _block_by_kind(cfg, kind):
    return [b for b in cfg.blocks if b.kind == kind]

def _edge_kinds_from(cfg, block_id):
    return {e.kind for e in _edges_from(cfg, block_id)}

def _has_stmt_kind(cfg, kind):
    return any(s.kind == kind for b in cfg.blocks for s in b.statements)


# ---------------------------------------------------------------------------
# Phase 1: Tests for all currently supported constructs
# ---------------------------------------------------------------------------

class TestLinear:
    def test_linear_function(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler():
                x = 1
                y = 2
                return x + y
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        assert len(_block_by_kind(cfg, "entry")) == 1
        assert len(_block_by_kind(cfg, "exit")) == 1
        # All edges should be fall_through or exit
        for e in cfg.edges:
            assert e.kind in ("fall_through", "exit")


class TestIfElse:
    def test_if_else(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler(x):
                if x > 0:
                    a = 1
                else:
                    a = 2
                return a
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        assert _has_stmt_kind(cfg, "branch_test")
        # Entry should have true and false outgoing edges
        entry = _block_by_kind(cfg, "entry")[0]
        kinds = _edge_kinds_from(cfg, entry.id)
        assert "true" in kinds
        assert "false" in kinds

    def test_if_no_else(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler(x):
                if x > 0:
                    a = 1
                return a
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        # False edge should go to merge, not directly to exit
        entry = _block_by_kind(cfg, "entry")[0]
        false_edges = [e for e in _edges_from(cfg, entry.id) if e.kind == "false"]
        assert len(false_edges) == 1
        target = next(b for b in cfg.blocks if b.id == false_edges[0].target_block_id)
        assert target.kind == "merge"

    def test_nested_if(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler(x, y):
                if x > 0:
                    if y > 0:
                        a = 1
                    else:
                        a = 2
                else:
                    a = 3
                return a
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        branch_tests = sum(1 for b in cfg.blocks for s in b.statements if s.kind == "branch_test")
        assert branch_tests == 2


class TestLoops:
    def test_for_loop(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler(items):
                for item in items:
                    process(item)
                return done()
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        assert _has_stmt_kind(cfg, "loop_header")
        # Must have a back_edge
        back_edges = [e for e in cfg.edges if e.kind == "back_edge"]
        assert len(back_edges) >= 1

    def test_while_loop(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler():
                while condition():
                    do_work()
                return result()
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        back_edges = [e for e in cfg.edges if e.kind == "back_edge"]
        assert len(back_edges) >= 1

    def test_break(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler(items):
                for item in items:
                    if item.done:
                        break
                    process(item)
                return result()
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        assert _has_stmt_kind(cfg, "break")
        # break block should NOT have a back_edge
        for b in cfg.blocks:
            if any(s.kind == "break" for s in b.statements):
                kinds = _edge_kinds_from(cfg, b.id)
                assert "back_edge" not in kinds
                assert "fall_through" in kinds  # goes to post-loop

    def test_continue(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler(items):
                for item in items:
                    if item.skip:
                        continue
                    process(item)
                return result()
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        assert _has_stmt_kind(cfg, "continue")
        # continue block should have back_edge to header
        for b in cfg.blocks:
            if any(s.kind == "continue" for s in b.statements):
                kinds = _edge_kinds_from(cfg, b.id)
                assert "back_edge" in kinds


class TestTryExcept:
    def test_try_except(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler():
                try:
                    x = risky()
                except ValueError:
                    x = fallback()
                return x
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        exception_edges = [e for e in cfg.edges if e.kind == "exception"]
        assert len(exception_edges) >= 1

    def test_try_else(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler():
                try:
                    x = risky()
                except ValueError:
                    x = fallback()
                else:
                    x = finalize(x)
                return x
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        # else block should exist
        else_blocks = [b for b in cfg.blocks if "else" in b.label.lower()]
        assert len(else_blocks) >= 1
        # else should have incoming fall_through (not exception)
        for eb in else_blocks:
            incoming = _edges_to(cfg, eb.id)
            assert all(e.kind != "exception" for e in incoming)

    def test_try_finally_return(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler():
                try:
                    return compute()
                finally:
                    cleanup()
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        # Return should go through finally, not directly to exit
        finally_blocks = [b for b in cfg.blocks if "finally" in b.label.lower()]
        assert len(finally_blocks) >= 1
        # Finally block should have exit edge to exit block
        exit_blocks = _block_by_kind(cfg, "exit")
        assert len(exit_blocks) == 1
        for fb in finally_blocks:
            out_kinds = _edge_kinds_from(cfg, fb.id)
            assert "exit" in out_kinds or "fall_through" in out_kinds

    def test_nested_finally(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler():
                try:
                    try:
                        return 1
                    finally:
                        inner()
                finally:
                    outer()
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        finally_blocks = [b for b in cfg.blocks if "finally" in b.label.lower()]
        # Should have 2 finally copies (inner and outer) in the return path
        assert len(finally_blocks) >= 2


class TestExceptionMatching:
    def test_raise_matching_handler(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler():
                try:
                    raise ValueError("bad")
                except ValueError:
                    handle()
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        # raise block should connect to except handler, not error_exit
        error_exits = _block_by_kind(cfg, "error_exit")
        assert len(error_exits) == 0  # error_exit pruned since raise goes to handler

    def test_raise_no_match(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler():
                try:
                    raise TypeError("bad")
                except ValueError:
                    handle()
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        # TypeError doesn't match ValueError, should go to error_exit
        error_exits = _block_by_kind(cfg, "error_exit")
        assert len(error_exits) >= 1

    def test_exception_hierarchy(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler():
                try:
                    raise KeyError("k")
                except LookupError:
                    handle()
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        # KeyError is subclass of LookupError — should match
        error_exits = _block_by_kind(cfg, "error_exit")
        assert len(error_exits) == 0  # matched, no error_exit needed
        exception_edges = [e for e in cfg.edges if e.kind == "exception"]
        assert len(exception_edges) >= 1


# ---------------------------------------------------------------------------
# Phase 2: CFG expansion — with, match, yield
# ---------------------------------------------------------------------------

class TestWith:
    def test_with_walks_body(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler():
                with open("f") as fh:
                    x = fh.read()
                return x
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        # Body statement (x = fh.read()) should exist in some block
        all_texts = [s.text for b in cfg.blocks for s in b.statements]
        assert any("read" in t for t in all_texts)

    def test_with_cleanup_on_return(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler():
                with open("f") as fh:
                    return fh.read()
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        # Return inside with should go through __exit__ (finally copy)
        exit_blocks = _block_by_kind(cfg, "exit")
        assert len(exit_blocks) >= 1

    def test_async_with(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            async def handler():
                async with connect() as conn:
                    data = await conn.fetch()
                return data
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        all_texts = [s.text for b in cfg.blocks for s in b.statements]
        assert any("async with" in t for t in all_texts)

    def test_nested_with(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler():
                with open("a") as fa:
                    with open("b") as fb:
                        return fa.read() + fb.read()
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)


class TestYield:
    def test_yield_continues_block(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler():
                yield 1
                yield 2
                return
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        assert _has_stmt_kind(cfg, "yield")
        # yield should not terminate the block — subsequent stmts should exist
        yields = sum(1 for b in cfg.blocks for s in b.statements if s.kind == "yield")
        assert yields == 2

    def test_yield_from(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler(items):
                yield from items
                return
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        assert _has_stmt_kind(cfg, "yield")


@pytest.mark.skipif(sys.version_info < (3, 10), reason="match requires 3.10+")
class TestMatch:
    def test_match_cases(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler(cmd):
                match cmd:
                    case "start":
                        do_start()
                    case "stop":
                        do_stop()
                    case _:
                        do_default()
                return done()
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
        assert _has_stmt_kind(cfg, "branch_test")
        # Should have edges to each case
        entry = _block_by_kind(cfg, "entry")[0]
        out_edges = _edges_from(cfg, entry.id)
        assert len(out_edges) >= 3  # 3 cases

    def test_match_wildcard(self, tmp_path):
        _write_files(tmp_path, {"app.py": """
            def handler(x):
                match x:
                    case 1:
                        return "one"
                    case _:
                        return "other"
        """})
        cfg = _build_cfg(tmp_path, "handler")
        _validate_cfg(cfg)
