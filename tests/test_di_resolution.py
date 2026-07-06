from codecanvas.parser.call_graph import CallGraphBuilder


def _build(tmp_path, src, name="di_app.py"):
    (tmp_path / name).write_text(src)
    cg = CallGraphBuilder(str(tmp_path))
    cg.analyze_project()
    return cg


UNIQUE_FIX = '''
from typing import Protocol

class OneIface(Protocol):
    def f(self): ...

class OnlyImpl(OneIface):
    def f(self):
        return 1

class TwoIface(Protocol):
    def g(self): ...

class ImplA(TwoIface):
    def g(self):
        return 1

class ImplB(TwoIface):
    def g(self):
        return 2

class Plain:
    def h(self):
        return 1
'''


def test_unique_impl_of_helper(tmp_path):
    cg = _build(tmp_path, UNIQUE_FIX)
    assert cg._unique_impl_of("OneIface") == "OnlyImpl"   # exactly one impl
    assert cg._unique_impl_of("TwoIface") is None          # ambiguous (2 impls)
    assert cg._unique_impl_of("Plain") is None             # concrete type
    assert cg._unique_impl_of("Nonexistent") is None       # unknown
