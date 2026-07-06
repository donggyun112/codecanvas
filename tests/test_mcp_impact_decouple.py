from pathlib import Path

from codecanvas.parser.call_graph import CallGraphBuilder
from codecanvas.graph.impact import ImpactAnalyzer
from codecanvas.parser.entrypoint_extractor import EntryPointExtractor
from codecanvas.parser.fastapi_extractor import FastAPIExtractor

SAMPLE = Path(__file__).parent.parent / "sample-fastapi"

# Diff touching the `login` handler (raises HTTPException(401) -> risk signal).
LOGIN_DIFF = """\
--- a/app/routers/auth.py
+++ b/app/routers/auth.py
@@ -14,3 +14,4 @@
 async def login(
     body: LoginRequest,
     db=Depends(get_db),
+    extra=None,
"""


def _entrypoints():
    extractor = FastAPIExtractor(str(SAMPLE))
    return EntryPointExtractor(str(SAMPLE), extractor).analyze()


def test_risk_populated_without_flow_builder():
    cg = CallGraphBuilder(str(SAMPLE))
    analyzer = ImpactAnalyzer(
        cg, str(SAMPLE), entrypoints=_entrypoints(), flow_builder=None
    )
    result = analyzer.analyze_diff(LOGIN_DIFF)
    login_funcs = [f for f in result.affected_functions if f.name == "login"]
    assert login_funcs, "login should be detected as changed"
    assert login_funcs[0].risk_score > 0, "risk must be computed without the viz builder"
