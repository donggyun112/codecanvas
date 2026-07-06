from codecanvas.graph.builder import FlowGraphBuilder
from codecanvas.mcp import queries


def _resolved(tmp_path, files):
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    b = FlowGraphBuilder(str(tmp_path))
    b.call_graph.analyze_project()
    return b


def test_suffix_unique_resolves(tmp_path):
    b = _resolved(tmp_path, {
        "app/uploads.py":
            "class UploadSingleFileUseCase:\n"
            "    def execute(self):\n"
            "        return 1\n"
            "class DownloadUseCase:\n"
            "    def execute(self):\n"
            "        return 2\n",
    })
    func, err = queries.resolve_function(b, "UploadSingleFileUseCase.execute")
    assert err is None
    assert func is not None and func.class_name == "UploadSingleFileUseCase"


def test_bare_ambiguous_returns_candidates(tmp_path):
    b = _resolved(tmp_path, {
        "app/uploads.py":
            "class UploadSingleFileUseCase:\n"
            "    def execute(self):\n"
            "        return 1\n"
            "class DownloadUseCase:\n"
            "    def execute(self):\n"
            "        return 2\n",
    })
    func, err = queries.resolve_function(b, "execute")
    assert func is None
    assert "candidates" in err and len(err["candidates"]) == 2
    assert {c["kind"] for c in err["candidates"]} == {"method"}


def test_suffix_boundary_no_false_match(tmp_path):
    b = _resolved(tmp_path, {
        "app/x.py":
            "def execute():\n"
            "    return 1\n"
            "def reexecute():\n"
            "    return 2\n",
    })
    func, err = queries.resolve_function(b, "execute")
    assert err is None
    assert func is not None and func.name == "execute"


def test_non_test_beats_test_double(tmp_path):
    b = _resolved(tmp_path, {
        "app/svc.py":
            "def process():\n"
            "    return 1\n",
        "tests/fakes.py":
            "def process():\n"
            "    return 2\n",
    })
    func, err = queries.resolve_function(b, "process")
    assert err is None
    assert func is not None and func.file_path.endswith("app/svc.py")


def test_concrete_beats_protocol(tmp_path):
    b = _resolved(tmp_path, {
        "app/auth.py":
            "from typing import Protocol\n"
            "class TokenService(Protocol):\n"
            "    def create_token_pair(self): ...\n"
            "class JWTService(TokenService):\n"
            "    def create_token_pair(self):\n"
            "        return 1\n",
    })
    func, err = queries.resolve_function(b, "create_token_pair")
    assert err is None
    assert func is not None
    assert func.qualified_name.endswith("JWTService.create_token_pair")


def test_weak_fan_in_returns_list(tmp_path):
    b = _resolved(tmp_path, {
        "app/a.py":
            "def proc():\n"
            "    return 1\n"
            "def c1():\n"
            "    return proc()\n",
        "app/b.py":
            "def proc():\n"
            "    return 2\n"
            "def d1():\n"
            "    return proc()\n"
            "def d2():\n"
            "    return proc()\n",
    })
    func, err = queries.resolve_function(b, "proc")
    assert func is None
    assert "candidates" in err and len(err["candidates"]) == 2


def test_strong_fan_in_auto_selects(tmp_path):
    b = _resolved(tmp_path, {
        "app/a.py":
            "def helper():\n"
            "    return 1\n"
            "def c1():\n"
            "    return helper()\n"
            "def c2():\n"
            "    return helper()\n",
        "app/b.py":
            "def helper():\n"
            "    return 2\n",
    })
    func, err = queries.resolve_function(b, "helper")
    assert err is None
    assert func is not None and func.file_path.endswith("app/a.py")


def test_miss_returns_suggestions(tmp_path):
    b = _resolved(tmp_path, {
        "app/x.py":
            "def compute():\n"
            "    return 1\n",
    })
    func, err = queries.resolve_function(b, "kompute")
    assert func is None
    assert "suggestions" in err and isinstance(err["suggestions"], list)


def test_file_line_single_resolves(tmp_path):
    b = _resolved(tmp_path, {
        "app/only.py":
            "def alpha():\n"
            "    return 1\n",
    })
    func, err = queries.resolve_function(b, "app/only.py:1")
    assert err is None
    assert func is not None and func.name == "alpha"


def test_file_line_multiple_returns_candidates(tmp_path):
    b = _resolved(tmp_path, {
        "pkg_a/svc.py":
            "def run():\n"
            "    return 1\n",
        "pkg_b/svc.py":
            "def go():\n"
            "    return 2\n",
    })
    func, err = queries.resolve_function(b, "svc.py:1")
    assert func is None
    assert "candidates" in err and len(err["candidates"]) == 2


def test_who_calls_ambiguous_propagates_candidates(tmp_path):
    b = _resolved(tmp_path, {
        "app/a.py":
            "def proc():\n"
            "    return 1\n"
            "def c1():\n"
            "    return proc()\n",
        "app/b.py":
            "def proc():\n"
            "    return 2\n"
            "def d1():\n"
            "    return proc()\n"
            "def d2():\n"
            "    return proc()\n",
    })
    out = queries.who_calls(b, "proc")
    assert "error" in out and "candidates" in out


def test_top_level_function_after_protocol_not_flagged(tmp_path):
    b = _resolved(tmp_path, {
        "app/svc.py":
            "from typing import Protocol\n"
            "class Reader(Protocol):\n"
            "    def read(self): ...\n"
            "def make_reader():\n"
            "    return None\n",
    })
    cg = b.call_graph
    fn = next(f for f in cg.all_functions() if f.name == "make_reader")
    assert fn.is_protocol is False
    assert fn.class_name is None
    assert fn.class_qname is None


def test_protocol_method_still_flagged(tmp_path):
    b = _resolved(tmp_path, {
        "app/svc.py":
            "from typing import Protocol\n"
            "class Reader(Protocol):\n"
            "    def read(self): ...\n",
    })
    cg = b.call_graph
    m = next(f for f in cg.all_functions() if f.name == "read")
    assert m.is_protocol is True
