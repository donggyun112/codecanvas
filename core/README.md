# CodeCanvas MCP

Evidence-backed code intelligence for Python coding agents.

CodeCanvas is a local static-analysis
[Model Context Protocol](https://modelcontextprotocol.io/) server. It answers
questions about call paths, control flow, and change impact without forcing an
agent to grep through an entire repository and infer the architecture from
partial text matches.

## Quick start

Python 3.10 or newer is required.

```bash
claude mcp add codecanvas -- uvx codecanvas-mcp
```

For another MCP client, use the equivalent stdio configuration:

```json
{
  "mcpServers": {
    "codecanvas": {
      "command": "uvx",
      "args": ["codecanvas-mcp"]
    }
  }
}
```

Pass an absolute `project_path` on the first tool call. CodeCanvas remembers the
last explicitly selected project for the rest of the server session.

## Tools

| Tool | Answers |
|---|---|
| `project_status` | Which Python project is active, and is its analysis cached or ambiguous? |
| `list_entrypoints` | What FastAPI routes, scripts, function entrypoints, and public library exports exist? |
| `find_symbols` | Where is a function, method, or class by name or meaning? |
| `who_calls` | Who calls this function, directly or transitively? |
| `call_tree` | What project code does this function reach, and where are the effects? |
| `what_does` | What are this function's signature, calls, effects, exceptions, and direct risk? |
| `function_flow` | How does this function branch? |
| `reaching_conditions` | Which guards lead to each return or raise? |
| `verify_claim` | Does a qualified `source reaches target` claim hold under the observed paths and guards? |
| `analyze_impact` | Which entrypoints or public surfaces can an inline diff or git ref affect? |
| `validate_state_schema` | Do a function's state reads, writes, and returns agree with a supplied schema? |
| `simulate_state_transition` | What happens for focused state cases, invariants, and dependency overrides? |

## Evidence-first responses

Static analysis is not runtime truth, so CodeCanvas exposes uncertainty instead
of hiding it. Successful responses identify the selected `analysis_root` and
report evidence strength, inferred or ambiguous edges, truncation, and whether
the result is safe to summarize as an unconditional claim.

`verify_claim` returns `true`, `false`, or `uncertain`. Unsupported qualifiers
and inferred-only paths cannot silently become a definite `true`.

## Scope and safety

- CodeCanvas analyzes Python source with AST-based call-graph and control-flow
  analysis.
- FastAPI routes and `Depends()` chains, scripts, and distributed package
  exports are recognized as entrypoints.
- Compatible analysis is cached locally under `<project>/.codecanvas/`.
- Dynamic imports, reflection, monkey patching, and runtime-only values may
  remain unresolved and are reported as qualifications where possible.
- `simulate_state_transition` imports and executes trusted project code in a
  separate process. It is not a security sandbox; the imported code may access
  the filesystem, network, or subprocesses and may have import-time effects.

The complete guide, agent instruction snippet, benchmark command, and
development setup are available in the
[GitHub repository](https://github.com/donggyun112/codecanvas).

## License

CodeCanvas MCP is open-source software licensed under the
[MIT License](https://github.com/donggyun112/codecanvas/blob/main/LICENSE).
