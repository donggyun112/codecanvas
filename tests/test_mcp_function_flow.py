from pathlib import Path

from codecanvas_mcp.parser.call_graph import CallGraphBuilder
from codecanvas_mcp.mcp import outline, queries


def _build(tmp_path, src, name="flow_app.py"):
    (tmp_path / name).write_text(src)
    cg = CallGraphBuilder(str(tmp_path))
    cg.analyze_project()
    return cg


FIXTURE = '''
import logging
logger = logging.getLogger(__name__)


class Svc:
    async def run(self, x, items):
        """Run the thing."""
        logger.info("starting")
        if not x:
            raise ValueError("x required")
        result = {"ok": False, "count": 0}
        try:
            data = await self.fetch(x)
            for it in items:
                if it.valid:
                    await self.store(it)
                else:
                    logger.warning("skip")
            if data is None:
                return {"ok": False, "reason": "nodata"}
            result = {"ok": True, "count": len(items)}
        except KeyError as e:
            logger.error("bad")
            result = {"ok": False, "error": str(e)}
        finally:
            await self.cleanup()
        return result

    async def fetch(self, x):
        return {}

    async def store(self, it):
        return None

    async def cleanup(self):
        return None
'''


def _flow_text(tmp_path):
    cg = _build(tmp_path, FIXTURE)
    node = cg.get_ast_node("flow_app.Svc.run")
    lines, truncated = outline.function_flow_lines(node)
    return "\n".join(lines), truncated


def test_flow_captures_control_structure(tmp_path):
    text, _ = _flow_text(tmp_path)
    for token in ("if not x:", "try:", "except KeyError as e:", "finally:",
                  "for it in items:", "if it.valid:", "else:"):
        assert token in text, (token, text)


def test_flow_marks_returns_and_raises_with_shape(tmp_path):
    text, _ = _flow_text(tmp_path)
    assert "✗ raise ValueError" in text
    assert "→ return {ok, reason}" in text     # early return dict-key shape
    assert "→ return result" in text
    assert "result = {ok, count}" in text       # dict assignment shown


def test_flow_renders_await_calls(tmp_path):
    text, _ = _flow_text(tmp_path)
    assert "await self.fetch(…)" in text
    assert "await self.store(…)" in text
    assert "await self.cleanup()" in text      # the finally-body call survives


def test_flow_filters_logging_noise(tmp_path):
    text, _ = _flow_text(tmp_path)
    assert "logger" not in text
    assert "starting" not in text
    assert "skip" not in text


def test_queries_function_flow_shape(tmp_path):
    cg = _build(tmp_path, FIXTURE)
    # queries.function_flow takes a builder-like with .call_graph; use a shim.
    class _B:
        call_graph = cg
    out = queries.function_flow(_B(), "run")
    assert out["function"].endswith("Svc.run")
    assert isinstance(out["flow"], list) and out["flow"]
    assert any("try:" in ln for ln in out["flow"])


def test_queries_function_flow_on_class_returns_error(tmp_path):
    cg = _build(tmp_path, FIXTURE)

    class _B:
        call_graph = cg
    out = queries.function_flow(_B(), "Svc")
    assert "error" in out


def test_queries_function_flow_unknown_returns_error(tmp_path):
    cg = _build(tmp_path, FIXTURE)

    class _B:
        call_graph = cg
    out = queries.function_flow(_B(), "does_not_exist_zzz")
    assert "error" in out
