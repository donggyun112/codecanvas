# MCP-Only Repo Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the abandoned VS Code extension from the repo and rewrite the root README so the repo reads as a single-product Python MCP server.

**Architecture:** Pure deletion + documentation change. No Python source is modified; `core/` layout and the PyPI build flow stay untouched. Verification is the existing test suite plus a local install check.

**Tech Stack:** git, pytest, pip (venv)

**Spec:** `docs/superpowers/specs/2026-07-28-mcp-repo-cleanup-design.md`

## Global Constraints

- Do NOT modify anything under `core/codecanvas_mcp/` (source stays byte-identical).
- Do NOT touch `core/pyproject.toml` or `core/README.md`.
- `core/` directory layout stays (no flattening).
- Root README must not mention: VS Code, webview, visualization views, runtime tracing, pnpm, or the `[server]` extra.
- Commit messages: conventional style (`chore:`, `docs:`), no AI attribution trailers.

---

### Task 1: Remove extension artifacts

**Files:**
- Delete (tracked): `extension/`, `webview/`, `package.json`, `pnpm-lock.yaml`, `pnpm-workspace.yaml`
- Delete (untracked leftover): `core/codecanvas/`
- Modify: `.gitignore` (remove lines 12–13 referencing `webview/` and `extension/`)

**Interfaces:**
- Consumes: nothing.
- Produces: a repo where `git ls-files extension webview` returns nothing; Task 2 rewrites the README on top of this state.

- [ ] **Step 1: Delete tracked extension files**

```bash
cd /Users/dongkseo/project/codecanvas
git rm -r --quiet extension webview
git rm --quiet package.json pnpm-lock.yaml pnpm-workspace.yaml
```

- [ ] **Step 2: Delete the untracked `core/codecanvas/` leftover**

It contains only `.DS_Store`, empty dirs, and `__pycache__` (verified: `git ls-files core/codecanvas` is empty, no code imports package `codecanvas`).

```bash
rm -rf core/codecanvas
```

- [ ] **Step 3: Clean `.gitignore`**

Remove exactly these two lines from `.gitignore` (currently lines 12–13); leave everything else untouched:

```
webview/src/styles/*.css.map
extension/media/assets/
```

- [ ] **Step 4: Verify deletion state**

```bash
git ls-files extension webview | wc -l   # expect 0
ls core                                   # expect: README.md codecanvas_mcp pyproject.toml tests uv.lock (no "codecanvas")
git status --short                        # only deletions + .gitignore modification
```

- [ ] **Step 5: Run the full test suite (must pass, proves no code depended on deleted files)**

```bash
python3 -m pytest
```

Expected: all tests pass (testpaths: `tests/` + `core/tests/`). If any test fails referencing extension/webview paths, STOP and report — do not fix by editing Python source.

- [ ] **Step 6: Commit**

```bash
git add .gitignore
git commit -m "chore: remove abandoned VS Code extension, go MCP-only"
```

---

### Task 2: Rewrite root README

**Files:**
- Modify: `README.md` (full replacement with the content below)

**Interfaces:**
- Consumes: Task 1's cleaned repo state.
- Produces: final README; Task 3 only verifies packaging.

- [ ] **Step 1: Replace `README.md` with exactly this content**

````markdown
# CodeCanvas

Precision static-analysis MCP server for Python codebases. Gives coding
agents ground-truth answers about call graphs, control flow, and change
impact — instead of grepping and guessing.

## Why

When a coding agent needs to know "who calls this function?" or "what
breaks if I change this?", it greps and reads whole files — token-hungry
and easy to get wrong. CodeCanvas parses the project once into a real
call graph and control-flow graph, caches it on disk, and answers those
questions precisely. Outputs are compact and token-bounded, and every
successful response identifies its `analysis_root` and carries
evidence/truncation metadata that says whether it is safe to summarize
as an unconditional claim.

## Quick Start

```bash
claude mcp add codecanvas -- uvx codecanvas-mcp
```

Or in any MCP client config:

```json
{ "mcpServers": { "codecanvas": { "command": "uvx", "args": ["codecanvas-mcp"] } } }
```

Pass `project_path` (the repo root) on the first tool call; it is
remembered for the rest of the session. Python 3.10+.

## Tools

| Tool | Answers |
|---|---|
| `list_entrypoints` | What entrypoints exist? API routes, scripts, and a library's public API |
| `find_symbols` | Where is this symbol? Exact-first search with score floor and separated suggestions |
| `who_calls` | Who calls this function? Ground-truth reverse call edges, N hops |
| `call_tree` | What does this function reach downstream, and what side effects does it trigger? |
| `what_does` | What does this function do? Signature, docstring, db/http/raise effects, risk |
| `function_flow` | How does this function branch? Structured branch subjects and scopes |
| `reaching_conditions` | Under exactly which guard conditions does each return/raise happen? |
| `verify_claim` | Is a "source reaches target" claim true, false, or uncertain? Conservative verdict from call paths and guards |
| `analyze_impact` | Which entrypoints/public surfaces are affected by this diff or git ref? |
| `project_status` | What has been analyzed? Cache state, analysis root, worker interpreter |
| `validate_state_schema` | Do these state fields actually exist? Static field validation |
| `simulate_state_transition` | Run focused synthetic or custom state-transition cases against project code |

Notes:

- `list_entrypoints` accepts `filter` / `kind` to narrow large projects.
  For libraries it reports the public API surface as `kind="export"`,
  read from each distributed package's `__all__` (or its `__init__.py`
  public names) plus `[project.scripts]`.
- `simulate_state_transition` runs project code, so its worker uses the
  project's virtualenv interpreter when one is found (`<project>/.venv`
  or `venv`, then the same in the parent directory). Pass
  `python_executable` to choose explicitly; every result reports the
  interpreter under `worker`.

## How It Works

- **libcst-based parsing** builds a project-wide call graph and per-function
  control-flow graphs, resolving dependency-injection chains (e.g. FastAPI
  `Depends()`).
- **Canonical IR** — every answer is a projection from one unified graph,
  so tools agree with each other.
- **Disk cache** at `.codecanvas/` (call graph + entrypoints) makes warm
  queries fast.

## Performance

| Metric | Value |
|---|---|
| Entrypoint discovery (warm) | 12ms |
| File count limit | 5,000 (`CODECANVAS_MAX_FILES`) |
| CPU throttle | 10ms yield every 50 files |

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CODECANVAS_MAX_FILES` | 5000 | Max files to analyze |
| `CODECANVAS_BATCH_SIZE` | 50 | CPU throttle batch size |
| `CODECANVAS_THROTTLE_MS` | 10 | Sleep between batches (ms) |

## Development

```bash
git clone https://github.com/donggyun112/codecanvas.git
cd codecanvas/core
pip install -e ".[dev]"
cd ..
python3 -m pytest
```

## License

Private
````

- [ ] **Step 2: Verify banned words are gone**

```bash
cd /Users/dongkseo/project/codecanvas
grep -inE "vs ?code|webview|pnpm|visualization|tracing|\[server\]" README.md
```

Expected: no output (exit 1).

- [ ] **Step 3: Verify the 12 tool names match the server**

```bash
grep -A1 "@mcp.tool()" core/codecanvas_mcp/mcp/server.py | grep "def " | wc -l   # expect 12
```

Cross-check each of the 12 names appears in README.md's Tools table.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: rewrite README around the MCP server"
```

---

### Task 3: Packaging verification

**Files:**
- Create (scratch only, not in repo): a throwaway venv under the session scratchpad.

**Interfaces:**
- Consumes: final repo state from Tasks 1–2.
- Produces: evidence that `pip install ./core` still works (deploy flow intact).

- [ ] **Step 1: Fresh venv install from the repo**

```bash
python3 -m venv /private/tmp/claude-501/-Users-dongkseo-project-codecanvas/83c40461-67bc-4c08-b604-9fe29ed81df9/scratchpad/ccvenv
/private/tmp/claude-501/-Users-dongkseo-project-codecanvas/83c40461-67bc-4c08-b604-9fe29ed81df9/scratchpad/ccvenv/bin/pip install --quiet /Users/dongkseo/project/codecanvas/core
```

Expected: install succeeds.

- [ ] **Step 2: Import + entrypoint smoke test**

```bash
echo 'import codecanvas_mcp.mcp.server as s; print("ok:", s.main.__module__)' > /private/tmp/claude-501/-Users-dongkseo-project-codecanvas/83c40461-67bc-4c08-b604-9fe29ed81df9/scratchpad/smoke.py
/private/tmp/claude-501/-Users-dongkseo-project-codecanvas/83c40461-67bc-4c08-b604-9fe29ed81df9/scratchpad/ccvenv/bin/python /private/tmp/claude-501/-Users-dongkseo-project-codecanvas/83c40461-67bc-4c08-b604-9fe29ed81df9/scratchpad/smoke.py
```

Expected output: `ok: codecanvas_mcp.mcp.server`

- [ ] **Step 3: Final state report**

```bash
cd /Users/dongkseo/project/codecanvas
git log --oneline -3
git status --short   # expect clean
```

Report the two new commits and clean status. No push unless the user asks.
