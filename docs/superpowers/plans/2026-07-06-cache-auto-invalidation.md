# Cache Auto-Invalidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Invalidate CodeCanvas's two disk caches automatically when the analyzer's own source changes, so a cache built by an older analyzer is never silently served.

**Architecture:** A single memoized `_analyzer_fingerprint()` hashes the analyzer's source files plus the package `__version__`. Both caches (`callgraph.json`, `entrypoints.json`) record that fingerprint in their payload and reject a load on mismatch, alongside the existing `version` and `signature` checks.

**Tech Stack:** Python 3.10+, stdlib `hashlib`, pytest.

## Global Constraints

- Design reference: `docs/superpowers/specs/2026-07-06-cache-auto-invalidation-design.md`.
- Hash scope (exact): every `.py` under `core/codecanvas/parser/`, plus `core/codecanvas/graph/builder.py` and `core/codecanvas/graph/models.py`, plus `codecanvas.__version__`.
- `_analyzer_fingerprint()` must never raise; on unreadable source it returns the `__version__`-only hash (never `None`, never disables the check).
- Keep the manual version integers as backstops: `CACHE_FORMAT_VERSION` (unchanged) and a new `ENTRYPOINT_CACHE_VERSION` (replaces the inline literal `1`). Do not merge them.
- Tests run from repo root with the venv interpreter (this environment's lean-ctx shell gate forbids `python3 -c`, `-e`, and bash pipes; plain `python3` lacks deps): `PYTHONPATH=core .venv/bin/python3 -m pytest tests/test_cache_and_throttle.py -v`.
- Commit messages: no AI/tool attribution or co-author footer.

---

### Task 1: Analyzer fingerprint function

**Files:**
- Modify: `core/codecanvas/parser/call_graph.py` (add `import hashlib`; add global + function after `_files_signature`, ~line 84)
- Test: `tests/test_cache_and_throttle.py` (new `TestAnalyzerFingerprint` class)

**Interfaces:**
- Consumes: `codecanvas.__version__` (a `str`, currently `"0.1.0"`).
- Produces: `codecanvas.parser.call_graph._analyzer_fingerprint() -> str` (64-char hex sha256, memoized in module global `_ANALYZER_FP`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cache_and_throttle.py`:

```python
class TestAnalyzerFingerprint:
    def test_stable_and_hex(self, monkeypatch):
        monkeypatch.setattr(cg_mod, "_ANALYZER_FP", None)
        a = cg_mod._analyzer_fingerprint()
        b = cg_mod._analyzer_fingerprint()
        assert a == b
        assert len(a) == 64
        assert all(c in "0123456789abcdef" for c in a)

    def test_folds_version(self, monkeypatch):
        import codecanvas
        monkeypatch.setattr(cg_mod, "_ANALYZER_FP", None)
        monkeypatch.setattr(codecanvas, "__version__", "9.9.9-test")
        fp_a = cg_mod._analyzer_fingerprint()

        monkeypatch.setattr(cg_mod, "_ANALYZER_FP", None)
        monkeypatch.setattr(codecanvas, "__version__", "0.0.0-other")
        fp_b = cg_mod._analyzer_fingerprint()

        assert fp_a != fp_b

    def test_fallback_on_unreadable_source(self, monkeypatch):
        import hashlib
        from pathlib import Path
        import codecanvas

        monkeypatch.setattr(cg_mod, "_ANALYZER_FP", None)

        def _boom(self):
            raise OSError("unreadable")

        monkeypatch.setattr(Path, "read_bytes", _boom)
        fp = cg_mod._analyzer_fingerprint()
        expected = hashlib.sha256(codecanvas.__version__.encode("utf-8")).hexdigest()
        assert fp == expected
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=core .venv/bin/python3 -m pytest tests/test_cache_and_throttle.py::TestAnalyzerFingerprint -v`
Expected: FAIL with `AttributeError: module 'codecanvas.parser.call_graph' has no attribute '_analyzer_fingerprint'`

- [ ] **Step 3: Add `import hashlib`**

In `core/codecanvas/parser/call_graph.py`, the import block is currently:

```python
import ast
import json
import logging
import os
import re
```

Change it to:

```python
import ast
import hashlib
import json
import logging
import os
import re
```

- [ ] **Step 4: Add the fingerprint global + function**

In `core/codecanvas/parser/call_graph.py`, immediately after the `_files_signature` function (which ends around line 84, just before `@dataclass\nclass FunctionDef`), insert:

```python
# Fingerprint of the analyzer's own source. When the analyzer's logic
# changes but the serialized cache layout does not, the file `signature`
# is unchanged and the manual CACHE_FORMAT_VERSION is easy to forget —
# so a stale cache would be served. This hash rejects such caches
# automatically. Memoized per process.
_ANALYZER_FP: str | None = None


def _analyzer_fingerprint() -> str:
    """Return a hash of the analyzer's source + package version.

    Covers every .py under parser/ plus graph/builder.py and
    graph/models.py (where the serialized dataclasses live). Never
    raises: on unreadable source (frozen / .pyc-only build) it falls
    back to a version-only hash rather than disabling the check.
    """
    global _ANALYZER_FP
    if _ANALYZER_FP is not None:
        return _ANALYZER_FP

    try:
        from codecanvas import __version__ as ver
    except Exception:
        ver = "unknown"

    h = hashlib.sha256()
    h.update(ver.encode("utf-8"))
    try:
        parser_dir = Path(__file__).resolve().parent
        graph_dir = parser_dir.parent / "graph"
        sources = sorted(parser_dir.glob("*.py"))
        sources += [graph_dir / "builder.py", graph_dir / "models.py"]
        for src in sorted(sources):
            h.update(src.read_bytes())
    except OSError:
        _ANALYZER_FP = hashlib.sha256(ver.encode("utf-8")).hexdigest()
        return _ANALYZER_FP

    _ANALYZER_FP = h.hexdigest()
    return _ANALYZER_FP
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=core .venv/bin/python3 -m pytest tests/test_cache_and_throttle.py::TestAnalyzerFingerprint -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add core/codecanvas/parser/call_graph.py tests/test_cache_and_throttle.py
git commit -m "Add analyzer source fingerprint for cache invalidation"
```

---

### Task 2: Wire the fingerprint into the callgraph cache

**Files:**
- Modify: `core/codecanvas/parser/call_graph.py` — `_load_cache` (~line 545-583) and `_save_cache` (~line 585-606)
- Test: `tests/test_cache_and_throttle.py` (add to `TestCallGraphCache`)

**Interfaces:**
- Consumes: `_analyzer_fingerprint()` (Task 1).
- Produces: `callgraph.json` payload now contains key `"analyzer"`; `_load_cache` returns `False` when it mismatches.

- [ ] **Step 1: Write the failing test**

Add this method inside the existing `TestCallGraphCache` class in `tests/test_cache_and_throttle.py`:

```python
    def test_callgraph_cache_rejects_stale_analyzer(self, tmp_path, monkeypatch):
        from pathlib import Path
        proj = _make_project(tmp_path)

        # Cold build writes callgraph.json with the real fingerprint.
        b1 = FlowGraphBuilder(proj)
        ep = next(e for e in b1.get_entrypoints() if e.kind == "api")
        b1.build_flow(ep)

        # Simulate an analyzer whose source changed.
        monkeypatch.setattr(cg_mod, "_analyzer_fingerprint", lambda: "STALE")

        b2 = FlowGraphBuilder(proj)
        sig = cg_mod._files_signature(
            cg_mod._iter_project_python_files(Path(proj))
        )
        assert b2.call_graph._load_cache(sig) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=core .venv/bin/python3 -m pytest "tests/test_cache_and_throttle.py::TestCallGraphCache::test_callgraph_cache_rejects_stale_analyzer" -v`
Expected: FAIL — `_load_cache` returns `True` (it ignores the analyzer field today).

- [ ] **Step 3: Add the analyzer check to `_load_cache`**

In `_load_cache`, the current version/signature checks read:

```python
        if payload.get("version") != CACHE_FORMAT_VERSION:
            return False
        if payload.get("signature") != signature:
            return False
```

Insert the analyzer check between them:

```python
        if payload.get("version") != CACHE_FORMAT_VERSION:
            return False
        if payload.get("analyzer") != _analyzer_fingerprint():
            return False
        if payload.get("signature") != signature:
            return False
```

- [ ] **Step 4: Record the fingerprint in `_save_cache`**

In `_save_cache`, the payload currently begins:

```python
            payload = {
                "version": CACHE_FORMAT_VERSION,
                "signature": signature,
```

Change it to:

```python
            payload = {
                "version": CACHE_FORMAT_VERSION,
                "analyzer": _analyzer_fingerprint(),
                "signature": signature,
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=core .venv/bin/python3 -m pytest tests/test_cache_and_throttle.py::TestCallGraphCache -v`
Expected: PASS (all 3 methods, including the existing `test_warm_flow_matches_cold` positive control)

- [ ] **Step 6: Commit**

```bash
git add core/codecanvas/parser/call_graph.py tests/test_cache_and_throttle.py
git commit -m "Reject stale callgraph cache via analyzer fingerprint"
```

---

### Task 3: Wire the fingerprint into the entrypoint cache

**Files:**
- Modify: `core/codecanvas/graph/builder.py` — add `ENTRYPOINT_CACHE_VERSION` constant (after imports, ~line 28); `_load_entrypoint_cache` (~line 73-97); `_save_entrypoint_cache` (~line 99-119)
- Test: `tests/test_cache_and_throttle.py` (add to `TestEntrypointCache`)

**Interfaces:**
- Consumes: `_analyzer_fingerprint()` (Task 1), imported function-locally so tests can monkeypatch it on `cg_mod`.
- Produces: `builder.ENTRYPOINT_CACHE_VERSION = 1`; `entrypoints.json` payload contains key `"analyzer"`; `_load_entrypoint_cache` returns `None` on mismatch.

- [ ] **Step 1: Write the failing test**

Add this method inside the existing `TestEntrypointCache` class in `tests/test_cache_and_throttle.py`:

```python
    def test_entrypoint_cache_rejects_stale_analyzer(self, tmp_path, monkeypatch):
        proj = _make_project(tmp_path)

        # Cold build writes entrypoints.json with the real fingerprint.
        b1 = FlowGraphBuilder(proj)
        b1.get_entrypoints()

        # Simulate an analyzer whose source changed.
        monkeypatch.setattr(cg_mod, "_analyzer_fingerprint", lambda: "STALE")

        b2 = FlowGraphBuilder(proj)
        assert b2._load_entrypoint_cache() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=core .venv/bin/python3 -m pytest "tests/test_cache_and_throttle.py::TestEntrypointCache::test_entrypoint_cache_rejects_stale_analyzer" -v`
Expected: FAIL — `_load_entrypoint_cache` returns a list (it ignores the analyzer field today).

- [ ] **Step 3: Add the `ENTRYPOINT_CACHE_VERSION` constant**

In `core/codecanvas/graph/builder.py`, after the import block (the `from codecanvas.parser.entrypoint_extractor import EntryPointExtractor` line, ~line 27) and before `# Map node types...` / `_LAYER_MAP`, insert:

```python
# Manual backstop version for the entrypoints.json cache. The analyzer
# fingerprint catches ordinary logic changes; bump this only for a
# deliberate change to the entrypoint cache payload layout that lies
# outside the hashed analyzer source.
ENTRYPOINT_CACHE_VERSION = 1
```

- [ ] **Step 4: Update `_load_entrypoint_cache`**

Its local import and version check currently read:

```python
        from codecanvas.parser.call_graph import (
            _iter_project_python_files,
            _files_signature,
        )
        cache_path = self._ep_cache_path()
        if not cache_path.exists():
            return None
        try:
            with cache_path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

        if not isinstance(payload, dict) or payload.get("version") != 1:
            return None
        sig = _files_signature(
            _iter_project_python_files(Path(self.project_root)),
        )
        if payload.get("signature") != sig:
            return None
```

Replace that block with:

```python
        from codecanvas.parser.call_graph import (
            _iter_project_python_files,
            _files_signature,
            _analyzer_fingerprint,
        )
        cache_path = self._ep_cache_path()
        if not cache_path.exists():
            return None
        try:
            with cache_path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

        if not isinstance(payload, dict) or payload.get("version") != ENTRYPOINT_CACHE_VERSION:
            return None
        if payload.get("analyzer") != _analyzer_fingerprint():
            return None
        sig = _files_signature(
            _iter_project_python_files(Path(self.project_root)),
        )
        if payload.get("signature") != sig:
            return None
```

- [ ] **Step 5: Update `_save_entrypoint_cache`**

Its local import and payload currently read:

```python
        from codecanvas.parser.call_graph import (
            _iter_project_python_files,
            _files_signature,
        )
        cache_path = self._ep_cache_path()
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "signature": _files_signature(
                    _iter_project_python_files(Path(self.project_root)),
                ),
                "entrypoints": [_ep_to_dict(ep) for ep in eps],
            }
```

Replace that block with:

```python
        from codecanvas.parser.call_graph import (
            _iter_project_python_files,
            _files_signature,
            _analyzer_fingerprint,
        )
        cache_path = self._ep_cache_path()
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": ENTRYPOINT_CACHE_VERSION,
                "analyzer": _analyzer_fingerprint(),
                "signature": _files_signature(
                    _iter_project_python_files(Path(self.project_root)),
                ),
                "entrypoints": [_ep_to_dict(ep) for ep in eps],
            }
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONPATH=core .venv/bin/python3 -m pytest tests/test_cache_and_throttle.py::TestEntrypointCache -v`
Expected: PASS (all methods, including existing `test_warm_matches_cold`, `test_cache_invalidated_on_file_change`, `test_corrupted_cache_falls_back`, `test_deleted_cache_falls_back`)

- [ ] **Step 7: Commit**

```bash
git add core/codecanvas/graph/builder.py tests/test_cache_and_throttle.py
git commit -m "Reject stale entrypoint cache via analyzer fingerprint"
```

---

### Task 4: Full regression gate

**Files:**
- No source changes; verification + broader test sweep.

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces: confidence that signature-based invalidation and lazy AST still work.

- [ ] **Step 1: Run the full cache/throttle suite**

Run: `PYTHONPATH=core .venv/bin/python3 -m pytest tests/test_cache_and_throttle.py -v`
Expected: PASS — all classes green, including `TestLazyAST` and `TestThrottle` (unchanged behavior) and `test_cache_invalidated_on_file_change` (signature path still works).

- [ ] **Step 2: Run the broader suite to catch unrelated breakage**

Run: `PYTHONPATH=core .venv/bin/python3 -m pytest tests/ -q`
Expected: PASS — no regressions from the cache changes. (If a pre-existing unrelated failure appears, note it; do not fix out of scope.)

- [ ] **Step 3: Manual smoke — confirm auto-invalidation end to end**

The end-to-end behavior is already proven deterministically by the rejection tests in Tasks 2 and 3 (they write a real cache, then reject it under a changed fingerprint). This step is an optional extra smoke.

This environment's shell gate blocks `python3 -c`; write a scratch script and run it with the venv interpreter. Create a temp file (e.g. under the session scratchpad dir) named `smoke_cache.py`:

```python
import sys, os, tempfile, pathlib
sys.path.insert(0, "core")
from codecanvas.graph.builder import FlowGraphBuilder
import codecanvas.parser.call_graph as cg
import codecanvas

d = tempfile.mkdtemp()
pathlib.Path(d, "app").mkdir()
pathlib.Path(d, "app/__init__.py").write_text("")
pathlib.Path(d, "app/main.py").write_text(
    'from fastapi import FastAPI\napp=FastAPI()\n@app.get("/x")\ndef h():\n    return 1\n'
)
b = FlowGraphBuilder(d); b.get_entrypoints()
print("cache written:", os.path.exists(os.path.join(d, ".codecanvas/entrypoints.json")))

# Simulate analyzer change: clear memo + change version.
cg._ANALYZER_FP = None
codecanvas.__version__ = "0.0.0-changed"
b2 = FlowGraphBuilder(d)
print("rejected stale cache (expect None):", b2._load_entrypoint_cache())
```

Run (from repo root): `PYTHONPATH=core .venv/bin/python3 <path>/smoke_cache.py`

Expected output:
```
cache written: True
rejected stale cache (expect None): None
```

- [ ] **Step 4: Commit (if any incidental fixes were needed)**

Only if Step 2/3 surfaced an in-scope fix:

```bash
git add -A
git commit -m "Fix cache fingerprint regression surfaced in verification"
```

---

## Self-Review

**Spec coverage:**
- Analyzer fingerprint (scope: parser/*.py + graph/builder.py + graph/models.py + `__version__`, memoized) → Task 1.
- Fallback to version-only, never raises → Task 1 (Step 4 `except OSError`) + test `test_fallback_on_unreadable_source`.
- `"analyzer"` field in both payloads, mismatch = miss → Tasks 2 & 3.
- Manual ints kept as backstops; `ENTRYPOINT_CACHE_VERSION` replaces inline `1`; not merged → Task 3.
- Tests: warm hit unchanged (existing positive controls) / analyzer-change miss (Tasks 2,3) / signature still works (Task 4 Step 1) / fallback path (Task 1) → covered.
- Acceptance criteria (auto-rebuild on analyzer edit; no perpetual rebuild; no new exceptions) → Tasks 2/3 + Task 4.

**Placeholder scan:** No TBD/TODO; every code step shows full code; no "add error handling" hand-waving.

**Type consistency:** `_analyzer_fingerprint()` returns `str` everywhere; `_ANALYZER_FP` global name consistent; `ENTRYPOINT_CACHE_VERSION` used identically in load/save; payload key `"analyzer"` identical across both caches and both operations.
