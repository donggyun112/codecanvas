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


SINGLE = '''
from typing import Protocol

class TokenService(Protocol):
    async def create_token_pair(self, user): ...

class JWTService(TokenService):
    async def create_token_pair(self, user):
        return {"a": 1}

class OAuthHandler:
    def __init__(self, token_service: TokenService):
        self.token_service = token_service
    async def handle_exchange(self, user):
        return await self.token_service.create_token_pair(user)
'''


def test_constructor_param_injection_recorded_as_attr_type(tmp_path):
    cg = _build(tmp_path, SINGLE)
    handler_cls = next(
        f for f in cg.all_functions()
        if f.definition_type == "class" and f.qualified_name.endswith("OAuthHandler")
    )
    attrs = cg._class_attr_types.get(handler_cls.qualified_name, {})
    assert attrs.get("token_service") == "TokenService", attrs
