# CodeCanvas benchmarks

## Independent zero-context logic-flow benchmark

This benchmark runs two fresh Codex agents on identical, source-grounded
questions about a Python repository:

1. **Baseline** — built-in shell, search, and read tools; every MCP server is
   disabled.
2. **CodeCanvas** — the same built-in tools plus only `codecanvas.logic_flow`.

Each task and condition gets an independent `codex exec --ephemeral` process,
the same model and reasoning effort, the same read-only target checkout, and
byte-identical task prompts. The CodeCanvas condition explicitly pre-approves
the read-only MCP tool because non-interactive Codex otherwise reports the
misleading error `user cancelled MCP tool call`.

The compact profile is intentional:

```toml
[mcp_servers.codecanvas]
command = "/path/to/codecanvas/core/.venv/bin/python"
args = ["-m", "codecanvas_mcp.mcp.server"]
cwd = "/path/to/codecanvas/core"
default_tools_approval_mode = "approve"
enabled_tools = ["logic_flow"]
```

Exposing every MCP schema materially increases the context repeated at each
agent inference. This benchmark tests the product's token-efficient logic-flow
profile, not the full multi-tool profile.

### Frozen ADK holdout

An independent source-only agent created four tasks at ADK commit
`c3c40bcd74a5c8e98b8d764d5f5e76c6fccfde7a`. One task was used to calibrate
the compact profile. The other three remained untouched until the configuration
and implementation were frozen.

The three-task holdout result:

| Metric | Baseline | CodeCanvas | Change |
|---|---:|---:|---:|
| Server-reported input + output tokens | 1,363,087 | 646,436 | **52.58% fewer** |
| Uncached input + output tokens | 183,951 | 157,476 | **14.39% fewer** |
| Mean blind rubric score | 100.0/100 | 99.5/100 | -0.5 point |

Per-task savings were `69.80%`, `45.94%`, and `-224.97%`. The simple artifact
lookup regressed because its baseline needed only three commands. CodeCanvas is
not a universal per-task win; its advantage appeared on the broader callback
and compaction flows.

The frozen tasks, hidden rubric, anonymized grades, exact usage values, tool
counts, answer hashes, trace hashes, and limitations are committed under:

- `agent_logic_flow/adk_holdout_v3_tasks.json`
- `agent_logic_flow/adk_holdout_v3_rubric.json`
- `results/adk_holdout_v3_grades.json`
- `results/adk_holdout_v3.json`

Raw traces are not committed because they contain large source excerpts. Their
SHA-256 hashes are recorded in the result file.

### Reproduce

This launches paid/model-backed Codex runs and can consume substantial tokens:

```bash
python benchmarks/benchmark_agent_logic_flow.py \
  /path/to/adk-python \
  --tasks benchmarks/agent_logic_flow/adk_holdout_v3_tasks.json \
  --output-dir /tmp/codecanvas-agent-benchmark
```

The runner verifies the target commit, launches paired conditions concurrently,
requires exactly one successful `turn.completed` event, and records both total
and uncached token views. Answer grading remains separate so the evaluator can
receive anonymized answers and a frozen rubric without seeing condition labels.

### What the numbers do not mean

- They are one model, one repository commit, one run per task and condition,
  and three holdout tasks.
- Server-reported tokens are not provider billing or dollar cost.
- Cached input is included in the headline total and also reported separately.
- The rubric did not define a pass threshold, so exact scores are reported
  rather than retroactively inventing pass/fail.
- Do not generalize the aggregate to small lookup tasks or other repositories
  without rerunning the benchmark.

## Latency

`benchmark_find_symbols.py` measures cold analysis, warm symbol-search latency,
and concurrent lookup throughput:

```bash
core/.venv/bin/python benchmarks/benchmark_find_symbols.py /path/to/project
```
