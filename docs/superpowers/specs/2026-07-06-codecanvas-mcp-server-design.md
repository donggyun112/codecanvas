# CodeCanvas MCP Server — Design (v1)

**Date:** 2026-07-06
**Status:** Approved (design), pending implementation plan
**Scope:** Expose CodeCanvas's static analysis engine to coding agents (Claude Code, Cursor, etc.) as an MCP server.

---

## 1. Motivation

Developers increasingly read code less and ask an LLM about logic more. For that workflow, a
human-facing visualization is the minority case; feeding **precise, token-bounded, question-shaped**
answers to the agent is higher leverage.

The value is not summarization (LLMs already do that, and bigger context windows keep eating it).
The durable moat is **precision + compression** of things an LLM does slowly and unreliably by
reading raw files:

- Whole-project call-graph reasoning that does not fit in context.
- Ground-truth call relationships (LLMs guess; static analysis knows).
- `Depends()` DI chain resolution (LLMs are bad at this across files).
- "What breaks if I change this?" impact analysis grounded in the real call graph.

Because the agent will trust these answers, **accuracy is now existential** — a wrong call graph is
worse than no tool, since the agent would be better off reading the code. Correctness of the reused
analysis code is a first-class requirement, not a nice-to-have.

## 2. Key architectural finding

The engine has two layers, and the reusable asset is **not** the "Canonical IR" the README
advertises — it is the layer *beneath* it.

**Layer 1 — raw analysis (the crown jewel, already agent-shaped):**
- `parser/call_graph.py` — `CallGraphBuilder`: `_functions` (qname → `FunctionDef`), `_caller_index`
  (reverse edges → "who calls X" is nearly free), type/DI/protocol/abstract/method-on-type
  resolution, disk cache for warm queries.
- `FunctionDef` / `CallSite` carry semantic facts (db/http calls, `raise` + status, branch/loop
  info, docstring, params, return type) — decoupled from rendering.
- `graph/impact.py` — `ImpactAnalyzer`: hunk → affected functions → reachable endpoints (BFS over
  caller index) → risk / call depth. The killer feature is already complete at the compute level.
- `parser/fastapi_extractor.py`, `parser/entrypoint_extractor.py` — entrypoint discovery.

**Layer 2 — visualization projection (human-only; noise for agents):**
- `graph/models.py` `FlowGraph` / `FlowNode` IR: `level`, `display_name`, ELK coordinates,
  parent/children grouping. `to_dict` is explicitly *"for JSON transport to VS Code webview."*
- `graph/builder.py` + CFG / `exec_l3`/`exec_l4` merges → the 216-node render graph.

**Consequence:** the MCP server reuses Layer 1 directly and does **not** touch the Layer 2 IR.
Wrapping the existing FastAPI endpoints (which emit Layer 2) would re-expose exactly the wrong shape.

## 3. Approaches considered

**A. Thin MCP layer over the shared core — CHOSEN.**
New `codecanvas/mcp/` module reuses `CallGraphBuilder` + `ImpactAnalyzer` + extractors, with its own
lean answer models and a stdio transport. The VS Code extension is untouched (non-destructive,
reversible).

**B. MCP wraps the existing FastAPI server (HTTP passthrough) — rejected.**
Tools would call the 5 HTTP endpoints, re-exposing the Layer 2 IR (357 KB dumps). Wrong output shape,
defeats the purpose.

**C. Full IR redesign driving both viz and MCP — rejected for v1.**
Too large; risks the working extension; viz and agent needs diverge enough that a single shared IR is
premature. Revisit only if v1 proves the direction.

## 4. Architecture (Approach A)

```
core/codecanvas/
  parser/call_graph.py      [reuse]  analysis + caller_index + resolution
  graph/impact.py           [reuse + small decouple]  impact
  parser/fastapi_extractor.py       [reuse]  entrypoints
  parser/entrypoint_extractor.py    [reuse]  entrypoints
  mcp/                      [NEW]
    __init__.py
    server.py     MCP server (stdio) + tool registration
    queries.py    agent-facing query functions over CallGraphBuilder / ImpactAnalyzer
    answers.py    token-bounded response models (compact dict + summary string)
    session.py    per-project CallGraphBuilder in-process cache (warm reuse)
```

- Uses the official `mcp` Python SDK, **stdio** transport (plugs directly into Claude Code / Cursor).
- New `codecanvas-mcp` console script in `core/pyproject.toml [project.scripts]`.
- `mcp` added as a dependency.

### Module responsibilities

- **`session.py`** — resolves a `project_path` to a cached `CallGraphBuilder` (mirrors the existing
  server's `_get_or_create_builder`). Warm queries reuse the disk-cache signature mechanism already
  in `call_graph.py` for invalidation. What it does: hand out an analyzed builder. Depends on:
  `CallGraphBuilder`.
- **`queries.py`** — pure functions: `(builder, args) → answer model`. One function per tool. No MCP
  or transport concerns. Independently unit-testable. Depends on: `CallGraphBuilder`,
  `ImpactAnalyzer`, extractors.
- **`answers.py`** — dataclasses with `to_dict()` + a short `summary` string; enforce token caps and
  emit explicit `"N more (truncated)"` markers. No coordinates, no node dumps. Depends on: nothing
  (plain data).
- **`server.py`** — registers tools, parses MCP tool calls into `queries.py` calls, serializes
  answers, maps exceptions to structured errors. Depends on: `mcp` SDK, `session`, `queries`,
  `answers`.

## 5. v1 tool surface (4 tools)

Every tool takes `project_path`. `function` arguments accept a qualified name, a bare name, or
`file:line`, resolved via `name_index`; on miss, return near-name suggestions.

| Tool | Input | Output (shape) |
|---|---|---|
| `list_entrypoints` | `project_path` | `[{id, method, path, handler, file:line, tags}]` |
| `who_calls` | `project_path, function` | direct callers `[{caller, file:line, condition?}]` + entrypoints that reach it |
| `what_does` | `project_path, function` | signature, 1-line docstring, outgoing-call summary (db read/write, http, raises+status, `Depends()` injected), risk score |
| `analyze_impact` | `project_path, diff? \| ref?` | `{changed_functions:[{qname, file:line, risk, change_type}], affected_endpoints:[{method, path, via, call_depth, aggregate_risk}], summary}` |

**Output principle:** compact structured data + a short `summary` string, hard token cap, explicit
truncation markers. No ELK coordinates, no `level`, no 200-node graphs.

## 6. Work required (beyond reuse)

1. Write the 4 `mcp/` modules.
2. **Decoupling:** `ImpactAnalyzer` risk currently depends on the viz builder
   (`self._flow_builder.build_flow(ep)`, impact.py ~line 205). Switch to the standalone
   `_compute_function_risk` (impact.py ~line 240) as the risk source so the MCP path never imports
   the Layer 2 builder. Keep the existing `_flow_builder` path working for the extension.
3. `session.py` caching + invalidation.
4. `pyproject.toml`: add `mcp` dependency + `codecanvas-mcp` console script.

## 7. Error handling

- `ProjectTooLargeError` → structured error carrying file count + limit.
- Function not found → helpful message with near-name suggestions from `name_index`.
- No diff / no changes → clear message, not an empty payload.
- Never surface a raw stack trace to the agent.

## 8. Testing

- Unit tests for each `queries.py` function against the in-repo fixtures `sample-fastapi/` and
  `sample-script/`, asserting `analyze_impact` / `who_calls` / `what_does` results on known functions
  (accuracy is existential — these tests are the guardrail).
- MCP protocol smoke test: `list_tools` + invoke each tool once end-to-end.
- Follow existing `tests/` conventions.

## 9. Deferred to v1.1 (intentional)

- `explain_endpoint` — compressed endpoint-flow summary. Needs a dedicated distillation design and
  risks re-importing Layer 2 complexity.
- `trace_path(from, to)` — path between two functions.
- Runtime tracing exposed over MCP.

## 10. Out of scope

- Any change to the VS Code extension or webview.
- Redesigning the Layer 2 `FlowGraph` IR.
- Non-Python language support.
