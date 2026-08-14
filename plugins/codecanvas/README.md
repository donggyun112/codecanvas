# CodeCanvas plugin

This package exposes the complete CodeCanvas MCP tool catalog to both Claude
Code and Codex. Both clients start the same local stdio server:

```text
uvx codecanvas-mcp
```

Install `uv` first if `uvx` is not already on your `PATH`.

## Claude Code

Add this repository as a marketplace, then install the plugin:

```bash
claude plugin marketplace add donggyun112/codecanvas
claude plugin install codecanvas@codecanvas
```

For a checkout that has not been pushed, validate and load the local plugin
directly:

```bash
claude plugin validate ./plugins/codecanvas --strict
claude --plugin-dir ./plugins/codecanvas
```

## Codex

Add this repository as a marketplace, then install the plugin:

```bash
codex plugin marketplace add donggyun112/codecanvas
codex plugin add codecanvas@codecanvas
```

For a checkout that has not been pushed, use the repository root as the local
marketplace source:

```bash
codex plugin marketplace add .
codex plugin add codecanvas@codecanvas
```

The plugin enables the complete tool catalog. Start behavioral investigations
with `logic_flow`, use `who_calls` for upstream impact, and use `call_tree` for
deeper downstream traces. Pass an absolute `project_path` on the first tool
call.
