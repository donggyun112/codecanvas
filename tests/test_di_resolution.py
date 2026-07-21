from codecanvas_mcp.parser.call_graph import CallGraphBuilder


def _build(tmp_path, src, name="di_app.py"):
    (tmp_path / name).write_text(src)
    cg = CallGraphBuilder(str(tmp_path))
    cg.analyze_project()
    return cg


def _build_files(tmp_path, files):
    for name, src in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src)
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


def test_unique_structural_protocol_impl_requires_full_shape(tmp_path):
    cg = _build(tmp_path, '''
from typing import Protocol

class BackendPort(Protocol):
    def run(self): ...
    def stop(self): ...

class PartialBackend:
    def run(self): return "partial"

class FakeBackend:
    def run(self): return "run"
    def stop(self): return "stop"
''')
    assert cg._unique_impl_of("BackendPort") == "FakeBackend"


def test_multiple_structural_protocol_impls_remain_ambiguous(tmp_path):
    cg = _build(tmp_path, '''
from typing import Protocol

class BackendPort(Protocol):
    def run(self): ...
    def stop(self): ...

class FakeBackend:
    def run(self): return "fake"
    def stop(self): return "fake"

class RealBackend:
    def run(self): return "real"
    def stop(self): return "real"
''')
    assert cg._unique_impl_of("BackendPort") is None


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


FASTAPI_UNTYPED_DEPENDS = '''
from fastapi import Depends, FastAPI

app = FastAPI()

class FakeBackend:
    def run(self):
        return "fake"

class OtherBackend:
    def run(self):
        return "other"

def get_backend():
    return FakeBackend()

@app.get("/work")
async def handle(backend=Depends(get_backend)):
    return backend.run()
'''


def test_fastapi_depends_provider_type_resolves_untyped_param(tmp_path):
    cg = _build(tmp_path, FASTAPI_UNTYPED_DEPENDS)
    handler = next(f for f in cg.all_functions() if f.qualified_name.endswith("handle"))
    assert handler.local_types.get("backend") == "FakeBackend"

    fake_callers = _callers_of(cg, "FakeBackend.run")
    other_callers = _callers_of(cg, "OtherBackend.run")
    assert any(q.endswith("handle") for q in fake_callers), fake_callers
    assert not any(q.endswith("handle") for q in other_callers), other_callers


FASTAPI_ANNOTATED_DEPENDS = '''
from typing import Annotated
from fastapi import Depends, FastAPI

app = FastAPI()

class Worker:
    def perform(self):
        return "done"

def get_worker() -> Worker:
    return Worker()

@app.get("/work")
async def handle(worker: Annotated[Worker, Depends(get_worker)]):
    return worker.perform()
'''


def test_fastapi_annotated_depends_normalizes_param_type(tmp_path):
    cg = _build(tmp_path, FASTAPI_ANNOTATED_DEPENDS)
    callers = _callers_of(cg, "Worker.perform")
    assert any(q.endswith("handle") for q in callers), callers


MODULE_GLOBAL_INJECTION = '''
from typing import Protocol

class BackendPort(Protocol):
    def run(self): ...

class FakeBackend:
    def run(self):
        return "fake"

class OtherBackend:
    def run(self):
        return "other"

def get_backend() -> BackendPort:
    return FakeBackend()

backend: BackendPort = get_backend()

def handle():
    return backend.run()
'''


def test_module_global_provider_assignment_resolves_injected_object(tmp_path):
    cg = _build(tmp_path, MODULE_GLOBAL_INJECTION)
    fake_callers = _callers_of(cg, "FakeBackend.run")
    other_callers = _callers_of(cg, "OtherBackend.run")
    assert any(q.endswith("handle") for q in fake_callers), fake_callers
    assert not any(q.endswith("handle") for q in other_callers), other_callers


def test_qualified_and_aliased_provider_does_not_bind_same_named_factory(tmp_path):
    cg = _build_files(tmp_path, {
        "a.py": '''
class ABackend:
    def run(self): return "a"

def get_backend(): return ABackend()
''',
        "b.py": '''
class BBackend:
    def run(self): return "b"

def get_backend(): return BBackend()
''',
        "app.py": '''
from fastapi import Depends as Inject
from b import get_backend as selected_backend

backend = selected_backend()

def global_handle(): return backend.run()
def dep_handle(backend=Inject(selected_backend)): return backend.run()
''',
    })
    b_callers = _callers_of(cg, "BBackend.run")
    a_callers = _callers_of(cg, "ABackend.run")
    assert any(q.endswith("global_handle") for q in b_callers), b_callers
    assert any(q.endswith("dep_handle") for q in b_callers), b_callers
    assert not any(q.endswith(("global_handle", "dep_handle")) for q in a_callers), a_callers


def test_untyped_parameter_shadows_module_global(tmp_path):
    cg = _build(tmp_path, '''
class FakeBackend:
    def run(self): return "fake"

class OtherBackend:
    def run(self): return "other"

def get_backend(): return FakeBackend()
backend = get_backend()

def handle(backend): return backend.run()
''')
    assert not any(q.endswith("handle") for q in _callers_of(cg, "FakeBackend.run"))
    assert not any(q.endswith("handle") for q in _callers_of(cg, "OtherBackend.run"))


def test_typed_dep_preserves_contract_and_indexes_runtime_implementation(tmp_path):
    cg = _build(tmp_path, '''
from typing import Annotated, Protocol
from fastapi import Depends

class BackendPort(Protocol):
    def run(self): ...

class FakeBackend(BackendPort):
    def run(self): return "fake"

def get_backend() -> BackendPort: return FakeBackend()
def handle(backend: Annotated[BackendPort, Depends(get_backend)]): return backend.run()
''')
    handler = next(f for f in cg.all_functions() if f.name == "handle")
    assert handler.local_types["backend"].startswith("Annotated[")
    assert handler.runtime_types["backend"] == "FakeBackend"
    assert any(q.endswith("handle") for q in _callers_of(cg, "BackendPort.run"))
    assert any(q.endswith("handle") for q in _callers_of(cg, "FakeBackend.run"))


def test_multiple_provider_returns_remain_ambiguous(tmp_path):
    cg = _build(tmp_path, '''
from fastapi import Depends

class FakeBackend:
    def run(self): return "fake"

class RealBackend:
    def run(self): return "real"

def get_backend(flag):
    if flag:
        return FakeBackend()
    return RealBackend()

def handle(backend=Depends(get_backend)): return backend.run()
''')
    handler = next(f for f in cg.all_functions() if f.name == "handle")
    assert "backend" not in handler.runtime_types
    assert not any(q.endswith("handle") for q in _callers_of(cg, "FakeBackend.run"))
    assert not any(q.endswith("handle") for q in _callers_of(cg, "RealBackend.run"))


def test_fastapi_dependency_override_selects_runtime_provider(tmp_path):
    cg = _build(tmp_path, '''
from fastapi import Depends, FastAPI

app = FastAPI()

class RealBackend:
    def run(self): return "real"

class FakeBackend:
    def run(self): return "fake"

def get_backend(): return RealBackend()
def override_backend(): return FakeBackend()

app.dependency_overrides[get_backend] = override_backend

def handle(backend=Depends(get_backend)): return backend.run()
''')
    assert any(q.endswith("handle") for q in _callers_of(cg, "FakeBackend.run"))
    assert not any(q.endswith("handle") for q in _callers_of(cg, "RealBackend.run"))


def test_yield_dependency_uses_yielded_type(tmp_path):
    cg = _build(tmp_path, '''
from collections.abc import Iterator
from fastapi import Depends

class Session:
    def close(self): return None

def get_session() -> Iterator[Session]:
    session = Session()
    yield session

def handle(session=Depends(get_session)): return session.close()
''')
    assert any(q.endswith("handle") for q in _callers_of(cg, "Session.close"))


def test_imported_module_global_resolves_runtime_type(tmp_path):
    cg = _build_files(tmp_path, {
        "deps.py": '''
class FakeBackend:
    def run(self): return "fake"

def get_backend(): return FakeBackend()
backend = get_backend()
''',
        "app.py": '''
import deps
from deps import backend as injected_backend

def module_handle(): return deps.backend.run()
def alias_handle(): return injected_backend.run()
''',
    })
    callers = _callers_of(cg, "FakeBackend.run")
    assert any(q.endswith("module_handle") for q in callers), callers
    assert any(q.endswith("alias_handle") for q in callers), callers


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
