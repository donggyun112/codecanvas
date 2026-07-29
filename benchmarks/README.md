# CodeCanvas benchmarks

## Independent zero-context agent benchmark

This benchmark runs two fresh Codex agents on identical, source-grounded
questions about a Python repository:

1. **Existing tools only** — built-in shell, search, and read tools; every MCP
   server is disabled.
2. **CodeCanvas** — the same built-in tools plus an explicitly enabled compact
   CodeCanvas profile.

Each task and condition gets an independent `codex exec --ephemeral` process,
the `gpt-5.6-sol` model with high reasoning effort, the same read-only target
checkout, and byte-identical task prompts. The conditions run concurrently.
The CodeCanvas condition explicitly pre-approves its read-only MCP tools because
non-interactive Codex otherwise reports the misleading error
`user cancelled MCP tool call`.

Two treatment profiles have been measured on the same holdout:

1. An audited `logic_flow`-only profile.
2. The current product-oriented compact profile:

```toml
[mcp_servers.codecanvas]
command = "/path/to/codecanvas/core/.venv/bin/python"
args = ["-m", "codecanvas_mcp.mcp.server"]
cwd = "/path/to/codecanvas/core"
default_tools_approval_mode = "approve"
enabled_tools = ["logic_flow", "who_calls", "call_tree"]
```

The current profile preserves the three navigation tools intended for
unfamiliar-code investigation without exposing the full tool catalog.

### Frozen ADK holdout

An independent source-only agent created four tasks at ADK commit
`c3c40bcd74a5c8e98b8d764d5f5e76c6fccfde7a`. One task was used to calibrate
the compact profile. The other three remained untouched until the configuration
and implementation were frozen.

### Results

Two fresh runs of the same three-task holdout produced opposite aggregate token
outcomes:

| Evaluation | Treatment tools | Existing tools only (same-run control) | Existing tools + CodeCanvas | Change vs same-run control | Uncached change vs same-run control | Mean blind score (/100), existing → CodeCanvas |
|---|---|---:|---:|---:|---:|---:|
| Audited holdout, 2026-07-29 | `logic_flow` | 1,363,087 | 646,436 | **52.58% fewer** | **14.39% fewer** | 100.0 → 99.5 |
| Three-tool replication, 2026-07-30 | `logic_flow`, `who_calls`, `call_tree` | 595,556 | 899,687 | **51.07% more** | **5.27% more** | 98.17 → 99.0 |

The per-task results include both sides of each holdout run.

#### Audited `logic_flow`-only run

| Task | Existing tools tokens | Existing + CodeCanvas tokens | Change | Blind score (/100), existing → CodeCanvas | Built-in commands, existing → CodeCanvas |
|---|---:|---:|---:|---:|---:|
| Agent callback lifecycle | 1,073,654 | 324,285 | 69.80% fewer | 100 → 100 | 31 → 26 |
| Compaction arbitration | 228,279 | 123,417 | 45.94% fewer | 100 → 98.5 | 9 → 4 |
| File artifact load | 61,154 | 198,734 | 224.97% more | 100 → 100 | 3 → 9 |

The CodeCanvas side also made one, one, and two `logic_flow` calls respectively.

#### Three-tool replication

| Task | Existing tools tokens | Existing + CodeCanvas tokens | Change | Blind score (/100), existing → CodeCanvas | Built-in commands, existing → CodeCanvas |
|---|---:|---:|---:|---:|---:|
| Agent callback lifecycle | 269,022 | 439,894 | 63.51% more | 100 → 100 | 15 → 23 |
| Compaction arbitration | 221,120 | 241,296 | 9.12% more | 94.5 → 97 | 9 → 10 |
| File artifact load | 105,414 | 218,497 | 107.28% more | 100 → 100 | 6 → 8 |

The treatment agents called `logic_flow` four times in total: once for callback,
once for compaction, and twice for artifact. They did not call `who_calls` or
`call_tree`. Existing-tools-only agents made 30 built-in command calls and the
CodeCanvas agents made 41.
A one-turn smoke prompt reported 13,785 input tokens for both the one-tool and
three-tool profiles.

The one-tool and three-tool schemas showed no measurable initial-token difference
in the smoke check, and the two added tools incurred no invocation cost. The
replication's higher total was associated with a longer agent exploration path.
Because the control trajectory also changed sharply between runs, the two
single-run holdout outcomes cannot isolate a causal effect or support a stable
token-savings percentage.

The frozen tasks, hidden rubric, anonymized grades, exact usage values, tool
counts, answer hashes, trace hashes, and limitations for the original audited
run are committed under:

- `agent_logic_flow/adk_holdout_v3_tasks.json`
- `agent_logic_flow/adk_holdout_v3_rubric.json`
- `results/adk_holdout_v3_grades.json`
- `results/adk_holdout_v3.json`

Raw traces are not committed because they contain large source excerpts. Their
SHA-256 hashes are recorded in the original result file. The three-tool
replication used the same frozen tasks and rubric; its temporary raw traces are
not committed.

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

- They are one model, one repository commit, two single-run evaluations, and
  three holdout tasks.
- Server-reported tokens are not provider billing or dollar cost.
- Cached input is included in the headline total and also reported separately.
- The rubric did not define a pass threshold, so exact scores are reported
  rather than retroactively inventing pass/fail.
- The opposite aggregate results demonstrate high trajectory variance.
- Do not advertise either run as a universal token reduction.
- A stronger estimate requires repeated paired runs, reported with a median and
  dispersion or confidence interval, on additional repositories and task types.

## Latency

`benchmark_find_symbols.py` measures cold analysis, warm symbol-search latency,
and concurrent lookup throughput:

```bash
core/.venv/bin/python benchmarks/benchmark_find_symbols.py /path/to/project
```
