from codecanvas_mcp.parser.call_graph import CallGraphBuilder


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


def _callers_of(cg, suffix):
    target = next(
        (f for f in cg.all_functions() if f.qualified_name.endswith(suffix)), None
    )
    assert target is not None, f"{suffix} not found in graph"
    return {caller.qualified_name for caller, _ref in cg.get_callers(target.qualified_name)}


def test_di_single_impl_binds_to_concrete(tmp_path):
    cg = _build(tmp_path, SINGLE)
    callers = _callers_of(cg, "JWTService.create_token_pair")
    assert any(q.endswith("OAuthHandler.handle_exchange") for q in callers), callers


TWO_IMPLS = SINGLE + '''
class OpaqueTokenService(TokenService):
    async def create_token_pair(self, user):
        return {}
'''


def test_di_two_impls_does_not_bind_concrete(tmp_path):
    cg = _build(tmp_path, TWO_IMPLS)
    jwt = _callers_of(cg, "JWTService.create_token_pair")
    opaque = _callers_of(cg, "OpaqueTokenService.create_token_pair")
    assert not any(q.endswith("OAuthHandler.handle_exchange") for q in jwt), jwt
    assert not any(q.endswith("OAuthHandler.handle_exchange") for q in opaque), opaque


GUARD = '''
class Base:
    def m(self):
        return 0

class Sub(Base):
    def m(self):
        return 1

class User:
    def __init__(self, b: Base):
        self.b = b
    def go(self):
        return self.b.m()
'''


def test_concrete_base_not_redirected_to_subclass(tmp_path):
    cg = _build(tmp_path, GUARD)
    base_callers = _callers_of(cg, "Base.m")
    sub_callers = _callers_of(cg, "Sub.m")
    assert any(q.endswith("User.go") for q in base_callers), base_callers
    assert not any(q.endswith("User.go") for q in sub_callers), sub_callers
