# CodeCanvas MCP

[![PyPI](https://img.shields.io/pypi/v/codecanvas-mcp)](https://pypi.org/project/codecanvas-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/codecanvas-mcp)](https://pypi.org/project/codecanvas-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Evidence-backed code intelligence for Python coding agents.

CodeCanvas is a local static-analysis
[Model Context Protocol](https://modelcontextprotocol.io/) server. It gives
coding agents compact answers about call paths, control flow, and change impact
without making them grep through an entire repository and guess how the pieces
fit together.

In three blinded agent evaluations on Google ADK's 433K-line Python codebase,
answer quality remained comparable, but token outcomes varied substantially.
The initial broad-tool profile used 46.11% more server-reported input + output
tokens, a later `logic_flow`-only holdout used 52.58% fewer, and a fresh
three-tool replication used 51.07% more. CodeCanvas therefore treats the
current benchmark as evidence of quality retention, not yet as a stable
token-reduction estimate. See the [methodology, all results, and
limitations](benchmarks/README.md).

Use it to answer questions such as:

- Who calls this function, directly or transitively?
- What can this function reach, and where do side effects happen?
- Under which guards can this return or exception occur?
- Does this source really reach that target in the requested mode?
- Which API routes, scripts, or public exports are affected by a diff?

CodeCanvas is Python-only and requires Python 3.10 or newer.

## Quick start

Install [uv](https://docs.astral.sh/uv/) if `uvx` is not already available,
then register the server with Claude Code:

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

For a repository with one nested Python project, `project_status` reports the
candidate analysis root. Select that root explicitly before relying on other
results.

## Teach your agent when to use it

Adding tools does not guarantee that an agent will choose them at the right
time. Put a short instruction like this in `AGENTS.md`, `CLAUDE.md`, or the
equivalent file used by your coding agent:

```markdown
## Code analysis

Use CodeCanvas before text search when you need to know:

- who calls a Python function or what it reaches downstream;
- which entrypoints a change can affect;
- how a function branches or what guards a return/raise;
- whether a source-to-target reachability claim is actually supported.

Pass `project_path` once, then reuse the active project. Treat
`safe_to_summarize: false`, inferred edges, ambiguity, and truncation as
qualifications rather than unconditional facts.
```

Then ask your agent naturally:

```text
Use CodeCanvas to list the entrypoints in this project.
Use logic_flow first to understand checkout without repeated source searches.
What calls UserService.delete, up to three hops?
What does checkout reach downstream, including HTTP or database effects?
Under exactly what conditions can authenticate raise?
Verify that dry-run publish reaches _call_api.
Analyze the impact of the current diff.
```

## What makes the answers trustworthy

Static analysis is not runtime truth, so CodeCanvas makes uncertainty visible
instead of hiding it.

Every successful MCP response identifies the selected `analysis_root` and
includes metadata that helps an agent decide how strongly it may state the
result:

- `evidence_grade` describes the strength of the resolved evidence.
- `inferred_edge_count` and `ambiguous_calls` expose uncertain call edges.
- `truncated` says whether the bounded response omitted results.
- `safe_to_summarize` says whether the result supports an unconditional claim.
- `response_guidance` explains how to qualify a result when it does not.

`verify_claim` goes further by combining candidate call paths with branch and
return/raise guards. It returns `true`, `false`, or `uncertain`; unsupported
qualifiers and inferred-only paths cannot silently become a definite `true`.

## Tools

### Discover and understand

| Tool | Use it for |
|---|---|
| `project_status` | Inspect the active root, Python file count, cache, worker interpreter, and nested project candidates |
| `list_entrypoints` | Find FastAPI routes, scripts, function entrypoints, and distributed library exports |
| `find_symbols` | Locate functions, methods, and classes with exact-first name, semantic, or hybrid search |
| `logic_flow` | Get one compact, citation-ready view of a function's branches, outcomes, downstream calls, and effects |
| `what_does` | Triage a function from its signature, docstring, calls, effects, exceptions, and direct risk |
| `function_flow` | Inspect a structured branch tree with subjects, conditions, scopes, and nesting |
| `reaching_conditions` | Get the enclosing guards for each return or raise, plus complexity and unreachable code |

### Follow behavior and assess change

| Tool | Use it for |
|---|---|
| `who_calls` | Walk direct or transitive callers upstream |
| `call_tree` | Walk project-internal callees downstream and attribute direct/transitive effects |
| `verify_claim` | Conservatively check a qualified `source reaches target` claim against paths and guards |
| `analyze_impact` | Map an inline diff or git ref to changed functions and affected entrypoints/public surfaces |

### Reproduce state-shaped bugs

| Tool | Use it for |
|---|---|
| `validate_state_schema` | Compare a function's state reads, writes, and mapping returns with a caller-provided schema |
| `simulate_state_transition` | Execute focused generated or explicit state cases with invariants and dependency overrides |

Large result sets are capped. Use each tool's `filter`, `kind`, `path`, `depth`,
or pagination arguments to narrow the answer before treating it as complete.

## How it works

1. **Select a project.** CodeCanvas resolves and remembers an explicit Python
   project root. Ambiguous nested roots must be selected rather than guessed.
2. **Build structural indexes.** Python AST analysis builds a project-wide call
   graph and per-function control-flow data. Extractors add FastAPI routes and
   `Depends()` chains, scripts, generic function entrypoints, and package
   exports.
3. **Reuse compatible analysis.** The call graph and entrypoints are cached in
   `<project>/.codecanvas/`; an in-process builder is reused during the MCP
   session.
4. **Project compact answers.** Each MCP tool queries the shared analysis and
   returns bounded results with origin, evidence, ambiguity, and truncation
   metadata.

The default analysis limit is 5,000 Python files. Tune large-project behavior
with:

| Variable | Default | Description |
|---|---:|---|
| `CODECANVAS_MAX_FILES` | `5000` | Maximum Python files to analyze |
| `CODECANVAS_BATCH_SIZE` | `50` | Files processed before yielding |
| `CODECANVAS_THROTTLE_MS` | `10` | Delay between batches in milliseconds |

## Safety and limitations

- CodeCanvas analyzes Python source; it does not model every possible dynamic
  import, monkey patch, reflection path, or runtime value.
- Inferred and ambiguous edges are reported as qualifications, not promoted to
  definite evidence.
- Static-analysis tools read project files and write the local `.codecanvas/`
  cache. No remote CodeCanvas service is required.
- `simulate_state_transition` is different: it imports and executes trusted
  project code in a separate process. It is isolation for focused repros, not a
  security sandbox. Project code may still access the filesystem, network, or
  subprocesses and may have import-time side effects.
- The simulator prefers `<project>/.venv` or `venv`, then the same directories
  in the parent project. Use `python_executable` to choose explicitly and check
  the returned `worker` metadata when imports fail.

## Independent agent benchmark

### Method

The evaluation compares paired, zero-context agents on source-grounded questions
about Google ADK commit
`c3c40bcd74a5c8e98b8d764d5f5e76c6fccfde7a`. It includes an initial four-task
suite and a later three-task frozen holdout:

- **Existing tools only:** built-in shell, search, and read tools with every MCP
  server disabled.
- **CodeCanvas:** the same built-in tools plus an explicitly enabled compact
  CodeCanvas profile.

Every task and condition runs in a fresh `codex exec --ephemeral` process with
the same `gpt-5.6-sol` model, high reasoning effort, read-only checkout, and
byte-identical prompt. A separate source-blind grader scores anonymized answers
against a rubric frozen before the holdout was opened.

### Results

| Evaluation | Treatment profile | Existing tools only | Existing tools + CodeCanvas | Total-token change | Uncached input + output change | Mean blind score, existing → CodeCanvas |
|---|---|---:|---:|---:|---:|---:|
| Initial four-task suite | Broad pre-compact tool profile | 2,018,662 | 2,949,473 | **46.11% more** | **23.73% more** | 22.0 → 22.25 / 25 |
| Frozen three-task holdout | `logic_flow` only | 1,363,087 | 646,436 | **52.58% fewer** | **14.39% fewer** | 100.0 → 99.5 / 100 |
| Frozen holdout replication | `logic_flow`, `who_calls`, `call_tree` | 595,556 | 899,687 | **51.07% more** | **5.27% more** | 98.17 → 99.0 / 100 |

In the initial suite, existing-tools-only agents made 58 commands. CodeCanvas
agents made 61 built-in commands and 32 MCP calls. In the `logic_flow`-only
holdout, the counts were 43 versus 39 built-in commands plus four MCP calls. In
the three-tool replication, they were 30 versus 41 plus four MCP calls. The
replication agents did not select `who_calls` or `call_tree`.

A one-turn smoke prompt reported the same 13,785 input tokens for the one-tool
and three-tool profiles. The observed difference between those two runs was
therefore dominated by different exploration trajectories rather than direct
use of the two additional tools.

The opposite aggregate outcomes show that one run per task is not a stable
estimate. Do not quote either percentage as a universal saving; repeated paired
runs should report a median and dispersion before making a token-reduction
claim. Server-reported tokens include cached input and are not provider billing.

### Reproduce the frozen holdout

The included runner reproduces the audited `logic_flow`-only condition:

```bash
python benchmarks/benchmark_agent_logic_flow.py \
  /path/to/adk-python \
  --tasks benchmarks/agent_logic_flow/adk_holdout_v3_tasks.json \
  --output-dir /tmp/codecanvas-agent-benchmark
```

The three-tool replication used the same protocol with this compact
configuration:

```toml
[mcp_servers.codecanvas]
command = "/path/to/codecanvas/core/.venv/bin/python"
args = ["-m", "codecanvas_mcp.mcp.server"]
cwd = "/path/to/codecanvas/core"
default_tools_approval_mode = "approve"
enabled_tools = ["logic_flow", "who_calls", "call_tree"]
```

This launches model-backed Codex runs and can consume substantial tokens. See
the [full methodology, per-task results, audit artifacts, and
limitations](benchmarks/README.md).

For cold analysis, warm symbol lookup, and concurrent lookup throughput:

```bash
core/.venv/bin/python benchmarks/benchmark_find_symbols.py /path/to/project
```

## Development

```bash
git clone https://github.com/donggyun112/codecanvas.git
cd codecanvas/core
uv sync --extra dev
cd ..
core/.venv/bin/python -m pytest
```

The package source lives under `core/`. The root test configuration runs both
the product tests in `tests/` and the package-level tests in `core/tests/`.

Issues and focused reproduction cases are welcome:
<https://github.com/donggyun112/codecanvas/issues>.

## License

CodeCanvas MCP is open-source software licensed under the [MIT License](LICENSE).
