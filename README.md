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
