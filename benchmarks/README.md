# CodeCanvas benchmarks

The benchmark surface now covers three pinned Python projects instead of a
single Google ADK checkout. Two different questions are measured separately:

- **Agent efficiency and answer quality:** paired zero-context Codex agents,
  with and without CodeCanvas.
- **Local analysis performance:** cold indexing, warm symbol search, and
  concurrent search throughput without model calls.

Do not combine these tables into one savings claim. Agent tokens are
trajectory-dependent; local latency is machine- and repository-dependent.

## Coverage

| Repository | Frozen revision | Selected Python root | Agent tasks | Agent result status | Local latency |
|---|---|---|---:|---|---|
| Google ADK | `c3c40bc` | repository root | 3 | Three paired repetitions + two historical runs | Measured |
| LangGraph | `49ae27c` | `libs/langgraph` | 3 | Three paired repetitions | Measured |
| FastAPI | `f336ff8` | repository root | 3 | Three paired repetitions | Measured |

The multi-repository manifest is
[`agent_logic_flow/multi_repo_v1.json`](agent_logic_flow/multi_repo_v1.json).
It selects exactly three tasks per project, excluding ADK's calibration task.
LangGraph covers graph compilation, checkpoint updates, and message reduction.
FastAPI covers recursive dependency injection, response serialization, and
lazy nested-router inclusion.

The LangGraph and FastAPI tasks and hidden rubrics were frozen before execution,
but they were not independently authored or audited. Treat their results as
provisional until that audit is added; freezing prevents later result-driven
task edits but does not remove selection bias.

## Agent benchmark

### Method

Each task compares two fresh `codex exec --ephemeral` processes:

1. **Existing tools only** — built-in shell, search, and read tools with all MCP
   servers disabled.
2. **CodeCanvas** — the same built-in tools plus only `logic_flow`.

This is an additive comparison: `logic_flow` does not replace baseline search.
The only tool-availability difference is whether that one CodeCanvas tool is
present.

Both conditions receive a byte-identical prompt, the same read-only checkout,
the `gpt-5.6-sol` model with high reasoning effort, and no prior task context.
The pair runs concurrently in an isolated `CODEX_HOME` containing only
authentication, so ambient user instructions and MCP configuration cannot
affect either condition. CodeCanvas calls are explicitly pre-approved, and the
runner rejects any treatment trace without a completed `logic_flow` call.

The new suite runner repeats every pair three times. It reports per-repository
and suite-wide median, minimum, and maximum changes for total tokens and
uncached input plus output. Answers and traces are hashed. Grading remains a
separate blinded step against the frozen rubric.

### Multi-repository result, 2026-08-14

All 54 sessions completed: 3 repositories × 3 tasks × 2 conditions × 3
repetitions. Every treatment session made a completed `logic_flow` call.
Positive values below mean CodeCanvas used fewer tokens; negative values mean
it used more.

| Scope | Total-token changes | Median [range] | Uncached changes | Median [range] |
|---|---|---:|---|---:|
| Google ADK | +3.30%, +47.61%, +2.45% | **+3.30%** [+2.45%, +47.61%] | -9.79%, -4.25%, +15.25% | **-4.25%** [-9.79%, +15.25%] |
| LangGraph | +25.25%, +22.90%, +9.61% | **+22.90%** [+9.61%, +25.25%] | -5.50%, -6.82%, -10.14% | **-6.82%** [-10.14%, -5.50%] |
| FastAPI | +38.02%, -19.38%, -1.87% | **-1.87%** [-19.38%, +38.02%] | +11.11%, -17.00%, +17.27% | **+11.11%** [-17.00%, +17.27%] |
| Suite-wide weighted aggregate | +22.98%, +22.95%, +2.43% | **+22.95%** [+2.43%, +22.98%] | -0.78%, -8.77%, +8.41% | **-0.78%** [-8.77%, +8.41%] |

Because both conditions retained identical built-in exploration tools and the
treatment added only `logic_flow`, the three positive suite-wide total-token
changes are meaningful evidence of incremental CodeCanvas value: one compact
structural view helped agents use less total context on top of ordinary search.
The median reduction was 22.95%.

The boundary of that claim still matters. CodeCanvas did not reduce uncached
input plus output (median -0.78%), so the result is consistent with better
reuse of cached context rather than evidence of lower provider billing or fewer
new tokens. FastAPI also changed direction across repetitions, showing that
agent trajectory variance remains material.

These answers have not yet been blind-graded. Do not describe the table as an
efficiency-at-equal-quality result until that separate grading step is complete.
The compact aggregate artifact is
[`results/agent_multi_repo_v1.json`](results/agent_multi_repo_v1.json).

### Historical single-run ADK results

Two earlier runs of the same ADK three-task holdout produced opposite aggregate
token outcomes:

| Evaluation | Treatment tools | Existing tools only | Existing + CodeCanvas | Change | Uncached change | Mean blind score, existing → CodeCanvas |
|---|---|---:|---:|---:|---:|---:|
| Audited holdout, 2026-07-29 | `logic_flow` | 1,363,087 | 646,436 | **52.58% fewer** | **14.39% fewer** | 100.0 → 99.5 |
| Three-tool replication, 2026-07-30 | `logic_flow`, `who_calls`, `call_tree` | 595,556 | 899,687 | **51.07% more** | **5.27% more** | 98.17 → 99.0 |

#### Audited `logic_flow`-only run

| Task | Existing tools | Existing + CodeCanvas | Change | Blind score | Built-in commands |
|---|---:|---:|---:|---:|---:|
| Agent callback lifecycle | 1,073,654 | 324,285 | 69.80% fewer | 100 → 100 | 31 → 26 |
| Compaction arbitration | 228,279 | 123,417 | 45.94% fewer | 100 → 98.5 | 9 → 4 |
| File artifact load | 61,154 | 198,734 | 224.97% more | 100 → 100 | 3 → 9 |

The CodeCanvas agents made one, one, and two `logic_flow` calls respectively.

#### Three-tool replication

| Task | Existing tools | Existing + CodeCanvas | Change | Blind score | Built-in commands |
|---|---:|---:|---:|---:|---:|
| Agent callback lifecycle | 269,022 | 439,894 | 63.51% more | 100 → 100 | 15 → 23 |
| Compaction arbitration | 221,120 | 241,296 | 9.12% more | 94.5 → 97 | 9 → 10 |
| File artifact load | 105,414 | 218,497 | 107.28% more | 100 → 100 | 6 → 8 |

The treatment agents made four `logic_flow` calls and did not select the two
additional tools. Existing-tools-only agents made 30 built-in calls; CodeCanvas
agents made 41. A one-turn smoke prompt reported 13,785 input tokens for both
the one-tool and three-tool schemas, so the replication increase was associated
with a longer exploration trajectory, not a measurable initial schema delta.

The opposite aggregate outcomes do not isolate a causal effect or support a
stable universal token-savings percentage.

### Agent artifacts

The ADK tasks, hidden rubric, anonymized grades, exact usage, tool counts, answer
hashes, trace hashes, and limitations are committed under:

- `agent_logic_flow/adk_holdout_v3_tasks.json`
- `agent_logic_flow/adk_holdout_v3_rubric.json`
- `results/adk_holdout_v3_grades.json`
- `results/adk_holdout_v3.json`

The multi-repository artifacts add:

- `agent_logic_flow/langgraph_holdout_v1_tasks.json`
- `agent_logic_flow/langgraph_holdout_v1_rubric.json`
- `agent_logic_flow/fastapi_holdout_v1_tasks.json`
- `agent_logic_flow/fastapi_holdout_v1_rubric.json`
- `results/agent_multi_repo_v1.json`

Raw traces are not committed because they contain large source excerpts.

### Reproduce the multi-repository suite

This launches paid, model-backed Codex runs. With the default manifest it starts
54 agent processes: 3 repositories × 3 tasks × 2 conditions × 3 repetitions.

```bash
mkdir -p /tmp/codecanvas-benchmark-codex-home
ln -s ~/.codex/auth.json /tmp/codecanvas-benchmark-codex-home/auth.json

CODEX_HOME=/tmp/codecanvas-benchmark-codex-home \
python benchmarks/benchmark_agent_suite.py \
  --checkout google-adk=/path/to/adk-python \
  --checkout langgraph=/path/to/langgraph \
  --checkout fastapi=/path/to/fastapi \
  --output-dir /tmp/codecanvas-agent-suite
```

The runner verifies every target commit, applies each configured project
subdirectory, selects only manifest task IDs, launches paired conditions, and
writes repetition, repository, and suite summaries. Use `--repetitions 1` only
for a smoke run, not for a publishable estimate.

### Agent-result limitations

- The multi-repository result covers one model and one pinned revision per
  repository.
- Server-reported tokens are not provider billing or dollar cost.
- Headline totals include cached input; uncached input plus output is separate.
- The ADK rubric defines points but no pass threshold.
- The multi-repository answers have not yet been blind-graded.
- The LangGraph and FastAPI tasks and rubrics were frozen before execution but
  were not independently authored or audited.
- Even three repetitions provide a range, not a narrow confidence interval.
- Do not advertise the historical or multi-repository result as a universal
  token reduction.

## Local analysis performance

Measured sequentially on 2026-08-13 at CodeCanvas commit `98d6596`, using
Python 3.12.10 on an Apple M4 Pro with 48 GiB RAM:

| Repository | Python files | Indexed functions | Cold analysis | First search | Warm median | Warm p95 | 8-worker throughput |
|---|---:|---:|---:|---:|---:|---:|---:|
| Google ADK | 1,650 | 16,960 | 61.62s | 303.271ms | 293.778ms | 306.492ms | 3.26 calls/s |
| LangGraph | 148 | 4,468 | 5.99s | 83.814ms | 83.087ms | 86.011ms | 11.53 calls/s |
| FastAPI | 1,136 | 5,580 | 4.38s | 50.486ms | 48.264ms | 48.821ms | 20.03 calls/s |

For each repository, the runner copied the selected subtree while excluding
Git metadata, CodeCanvas caches, virtual environments, `node_modules`, and
Python bytecode. It then built a fresh call graph, performed one first search,
50 sequential warm searches, and 50 searches through an eight-worker pool.
Repositories were measured one at a time.

The raw machine metadata, pinned revisions, project roots, queries, exact
measurements, and limitations are in
[`results/find_symbols_multi_repo_v1.json`](results/find_symbols_multi_repo_v1.json).

Reproduce one project with:

```bash
core/.venv/bin/python benchmarks/benchmark_find_symbols.py \
  /path/to/project \
  --query "project-relevant symbol query" \
  --iterations 50 \
  --workers 8
```

These are single-machine, single-run measurements. Queries differ by repository
to exercise relevant symbol ranking, and cold time reflects code structure and
indexed symbol complexity rather than file count alone.
