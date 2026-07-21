# codecanvas-mcp

Precision static-analysis MCP server for Python codebases. Gives coding
agents ground-truth answers about call graphs, control flow, and change
impact — instead of grepping and guessing.

## Tools

- `list_entrypoints` — API routes, scripts, and function entrypoints
- `who_calls` / `call_tree` — reverse and forward call graph, N hops
- `what_does` — signature, docstring, db/http/raise effects, risk
- `function_flow` — de-noised control-flow outline of a function
- `reaching_conditions` — the guard conditions behind every return/raise
- `analyze_impact` — changed functions and affected entrypoints/public surface for a diff or git ref

## Usage

```bash
claude mcp add codecanvas -- uvx codecanvas-mcp
```

Or in any MCP client config:

```json
{ "mcpServers": { "codecanvas": { "command": "uvx", "args": ["codecanvas-mcp"] } } }
```

Pass `project_path` on the first tool call; it is remembered for the rest
of the session.

## Extras

The base install is analysis-only. `pip install "codecanvas-mcp[server]"`
adds the FastAPI web server and runtime tracer used by the
[CodeCanvas VS Code extension](https://github.com/donggyun112/codecanvas).
