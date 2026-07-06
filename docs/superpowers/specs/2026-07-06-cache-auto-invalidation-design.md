# Cache Auto-Invalidation Design

**Date:** 2026-07-06
**Status:** Approved — ready for implementation plan
**Topic:** Tie the disk-cache validity to a fingerprint of the analyzer's own source, so a cache built by an older analyzer is invalidated automatically.

## Problem

CodeCanvas keeps two per-project disk caches under `.codecanvas/`:

| Cache | Producer | Serialized shape | Validity keys today |
|-------|----------|------------------|---------------------|
| `callgraph.json` | `parser/call_graph.py` (`FlowGraphBuilder.analyze_project`) | `FunctionDef` (via `_function_to_dict`), `name_index`, `module_map`, `class_attr_types` | `version` = `CACHE_FORMAT_VERSION` (int, manual) + `signature` (mtime/size of analyzed `.py` files) |
| `entrypoints.json` | `graph/builder.py` (`get_entrypoints` → `entrypoint_extractor.analyze`) | `EntryPoint` (via `_ep_to_dict`; dataclass in `graph/models.py`) | `version` = inline literal `1` + same `signature` |

Both caches are invalidated when the **analyzed project's** files change (via `signature`). Neither is invalidated when the **analyzer's own logic** changes. When analyzer behavior changes but the serialized layout does not — e.g. the DI-resolution work in commits `af7207e`, `dca0d27`, `a7335d2` — a stale cache is silently served. The only recovery today is a manual `CACHE_FORMAT_VERSION` bump (easy to forget) or hand-deleting `.codecanvas/` (what happened for the IN7 project during the DI PR).

## Goal

A cache written by one version of the analyzer is rejected by a materially different version, automatically — no manual version bump required for ordinary logic changes.

## Non-goals

- Fine-grained "only invalidate the cache whose producer changed" precision. We accept coarse, always-correct invalidation: editing any in-scope analyzer file invalidates both caches. Over-invalidation only costs a one-time rebuild, and only while the analyzer itself is being edited (i.e. during CodeCanvas development).
- Detecting intent behind changes. Any content change to an in-scope file changes the fingerprint, even a comment. That is acceptable and desirable (safe over precise).

## Design

### Analyzer fingerprint

A single shared fingerprint, computed once per process and cached in a module-level global, placed in `parser/call_graph.py` (which `graph/builder.py` already imports cache helpers from — `_files_signature`, `_iter_project_python_files`).

```
_analyzer_fingerprint() -> str
```

- Hash (sha256) the sorted contents of the analyzer source files:
  - every `.py` in `core/codecanvas/parser/`
  - `core/codecanvas/graph/builder.py`
  - `core/codecanvas/graph/models.py`  ← included so `EntryPoint`/`NodeType` format changes are caught; without it the serialized entrypoint dataclass lives outside the hashed scope
- Always fold `codecanvas.__version__` into the hash, so a release version bump also invalidates.
- Paths are resolved relative to the analyzer's own package (`Path(__file__).parent` for `parser/`, `../graph/` for the two graph files), not the analyzed project.

**Rationale for scope (Option A, shared hash):** the motivating bug (DI commits) changed only `parser/call_graph.py`, which is inside the scope — verified via `git show --stat`. Per-cache precise scoping was rejected because it requires maintaining a file→cache dependency map, and missing a transitive dependency is exactly the class of bug being fixed.

### Fallback

If any source file is unreadable (frozen build / `.pyc`-only distribution):

- The fingerprint falls back to `codecanvas.__version__` alone — never `None`/skip, so the check is never silently disabled, and releases still invalidate.
- Any error during hashing is caught; the function never raises. Cache correctness degrades gracefully to "version-only invalidation," matching today's behavior at worst.

Note: in practice the VS Code extension spawns `python3` against the `../core` package **source** (`extension/src/server.ts`), so `.py` files are present at runtime and the primary hash path is taken in production too. The fallback is a genuine safety net, rarely hit.

### Cache integration

Add an `"analyzer"` field to both cache payloads:

- `_save_cache` / `_save_entrypoint_cache`: write `"analyzer": _analyzer_fingerprint()`.
- `_load_cache` / `_load_entrypoint_cache`: in addition to the existing `version` and `signature` checks, treat `payload.get("analyzer") != _analyzer_fingerprint()` as a cache miss.

### Manual version integers (kept as backstops)

- Keep `CACHE_FORMAT_VERSION` (callgraph cache).
- Add `ENTRYPOINT_CACHE_VERSION` constant and replace the inline literal `1` in `graph/builder.py` with it. Do **not** collapse the two into one constant — the two payloads have different formats and evolve independently.
- With `models.py` in the hashed scope these become pure backstops: a free, self-documenting lever for deliberate format breaks, and coverage for the rare format surface outside the hash.

## Cache inventory (verified complete)

A grep for `.codecanvas/`, `json.dump(`, and `.json.tmp` write sites across `core/` found exactly the two caches above. No third disk cache exists to reintroduce the bug.

## Testing

Extend `tests/test_cache_and_throttle.py`:

1. **Warm hit unchanged** — cold build populates cache, second build loads from cache (existing behavior preserved for both caches).
2. **Analyzer change invalidates** — with a warm cache, force `_analyzer_fingerprint()` to return a different value (monkeypatch) and assert both `_load_cache` and `_load_entrypoint_cache` miss and rebuild.
3. **Signature still works** — modifying an analyzed source file still invalidates (regression guard).
4. **Fallback path** — simulate unreadable sources; assert the fingerprint equals the `__version__`-only value and the cache still loads/saves without raising.

## Acceptance criteria

- Editing any file in `parser/`, `graph/builder.py`, or `graph/models.py` causes both caches to be rebuilt on the next analysis, with no manual version bump.
- An unchanged analyzer + unchanged project still loads from cache (no perpetual rebuild).
- No new exceptions on any path; frozen/`.pyc`-only builds degrade to version-only invalidation rather than crashing.
