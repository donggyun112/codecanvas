"""Tests for codecanvas_mcp.mcp.queries.resolve_function.

Regression coverage for the resolver gap surfaced in a real session
(docs/superpowers/specs/2026-07-10-resolver-gapped-suffix-design.md):
a query that skips an enclosing scope (`Class.nested`, omitting the method
in between) must resolve, and genuine misses must suggest *qualified* names.

resolve_function reads only `builder.call_graph`, so the builder here is a
plain namespace wrapping a real, fully-analyzed CallGraphBuilder. The AST
indexing and the resolver are the real code under test.
"""
from __future__ import annotations

import types

import pytest

from codecanvas_mcp.parser.call_graph import CallGraphBuilder
from codecanvas_mcp.mcp.queries import resolve_function


_SVC = '''\
class UserTransferService:
    async def transfer_user(self, uid, target):
        def _do_transfer(session):
            return uid
        return _do_transfer(None)
'''

# Two modules with an identically-named class/method/nested function, so a
# gapped ref (`Handler._step`) is genuinely ambiguous across the project.
_HANDLER = '''\
class Handler:
    def process(self):
        def _step():
            return 1
        return _step()
'''


@pytest.fixture(scope="module")
def builder(tmp_path_factory):
    root = tmp_path_factory.mktemp("proj")
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "svc.py").write_text(_SVC)
    (pkg / "handler_a.py").write_text(_HANDLER)
    (pkg / "handler_b.py").write_text(_HANDLER)

    cg = CallGraphBuilder(str(root))
    cg.analyze_project()
    return types.SimpleNamespace(call_graph=cg)


# --- resolution paths -------------------------------------------------------

def test_gapped_suffix_skipping_enclosing_scope_resolves(builder):
    """The exact query from the log: Class.nested, skipping the method."""
    func, err = resolve_function(builder, "UserTransferService._do_transfer")
    assert err is None, err
    assert func.qualified_name.endswith(
        ".UserTransferService.transfer_user._do_transfer"
    )


def test_bare_nested_name_still_resolves(builder):
    func, err = resolve_function(builder, "_do_transfer")
    assert err is None, err
    assert func.qualified_name.endswith(".transfer_user._do_transfer")


def test_contiguous_suffix_still_resolves(builder):
    func, err = resolve_function(builder, "transfer_user._do_transfer")
    assert err is None, err
    assert func.qualified_name.endswith(".transfer_user._do_transfer")


def test_exact_qualified_name_resolves(builder):
    func, err = resolve_function(
        builder, "pkg.svc.UserTransferService.transfer_user._do_transfer"
    )
    assert err is None, err
    assert func.qualified_name == (
        "pkg.svc.UserTransferService.transfer_user._do_transfer"
    )


# --- ambiguity: gapped ref matching two functions ---------------------------

def test_ambiguous_gapped_ref_returns_ranked_candidates(builder):
    func, err = resolve_function(builder, "Handler._step")
    assert func is None
    assert err is not None
    names = {c["qualified_name"] for c in err["candidates"]}
    assert names == {
        "pkg.handler_a.Handler.process._step",
        "pkg.handler_b.Handler.process._step",
    }


# --- miss suggestions -------------------------------------------------------

def test_miss_suggestions_are_qualified_names(builder):
    """A typo'd tail must suggest the real *qualified* target, not a bare name."""
    func, err = resolve_function(builder, "UserTransferService.do_transfer")
    assert func is None
    assert err is not None
    sugg = err["suggestions"]
    assert sugg, "expected non-empty suggestions"
    assert all("." in s for s in sugg), sugg
    assert any(s.endswith("._do_transfer") for s in sugg), sugg


# --- precision guard: tail-anchoring + ordering -----------------------------

def test_gapped_match_is_tail_anchored_and_ordered(builder):
    """Wrong segment order must not resolve, even though both names exist."""
    func, err = resolve_function(builder, "_do_transfer.transfer_user")
    assert func is None
    assert err is not None
