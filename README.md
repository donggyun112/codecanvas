# CodeCanvas

A VS Code extension that statically analyzes Python codebases and visualizes execution flows, call graphs, control flow, and data flow — so you can understand code without reading every line.

Automatically traces function calls, data transformations, branch structures, and dependency injection chains. Detects which API endpoints are affected when you change code.

## Features

### 5 Visualization Views

| View | Description |
|---|---|
| **Review Brief** | Risk scores, concerns, decision points at a glance |
| **Code Flow** | Execution-order flow with inline source code from CFG blocks |
| **Data Flow** | How data moves through the system (query, transform, validate, branch, respond) |
| **Call Stack** | Function call graph with drill-down from L0 trigger to L4 statements |
| **CFG** | Control flow graph — branches, loops, exception paths with source code |

### Change Impact Analysis

Automatically detects which API endpoints are affected by code changes.

- Click **"Analyze Uncommitted Changes"** in the sidebar
- Maps changed functions → call graph → affected endpoints
- Follows `Depends()` dependency injection chains
- Shows risk score + call depth per endpoint

### Runtime Tracing

Send actual HTTP requests and visualize which code paths were executed.

- HIT / MISS badges on every node
- 3-way path classification: verified / unverified / runtime-only

## Architecture

```
extension/          VS Code host (TypeScript, tsup)
  ├─ src/extension.ts    Activation + command registration
  ├─ src/server.ts       Python analysis server lifecycle
  ├─ src/flowPanel.ts    Webview panel (flow rendering)
  └─ src/sidebar.ts      Sidebar (endpoint list + Impact Analysis)

webview/            React Flow canvas (React 19, Vite)
  ├─ src/App.tsx         Main canvas + view switching
  ├─ src/transform/      Per-view data transforms
  │   ├─ projection.ts       Kind-based projection utility
  │   ├─ cfgTransform.ts     CFG view
  │   ├─ codeFlowTransform.ts Code Flow view
  │   ├─ executionTransform.ts Data Flow view
  │   └─ visibility.ts       Callstack view filtering
  ├─ src/nodes/          Node components (7 types)
  ├─ src/edges/          Smart edges (A* pathfinding)
  └─ src/layout/         ELK layout + branch centering

core/               Python static analysis engine
  ├─ codecanvas/graph/
  │   ├─ models.py       Canonical IR (FlowNode, FlowEdge, FlowGraph)
  │   ├─ builder.py      FlowGraph build pipeline
  │   ├─ ast_execution.py AST → ExecutionGraph (semantic execution steps)
  │   ├─ cfg.py          AST → ControlFlowGraph (branches/loops)
  │   ├─ impact.py       git diff → impact analysis
  │   └─ execution.py    ExecutionGraph model + L3 merge
  ├─ codecanvas/parser/
  │   ├─ call_graph.py   Project-wide call graph + disk cache
  │   ├─ fastapi_extractor.py  FastAPI routes/middleware/exception handlers
  │   └─ entrypoint_extractor.py  API/script/function entrypoint discovery
  └─ codecanvas/server/
      └─ app.py          FastAPI analysis server (5 endpoints)
```

## Canonical IR

All visualization views are projections from a single unified graph.

```
FlowGraph.nodes (classified by kind)
  ├─ trigger / pipeline / file / function / statement   ← Callstack view
  ├─ cfg_block                                          ← CFG view + Code Flow source
  ├─ exec_l4                                            ← Data Flow (detail) + Code Flow
  └─ exec_l3                                            ← Data Flow (summary)
```

Use `projectByKind(flowData, kinds, edgeTypes)` to extract nodes/edges for any view.

## Performance

| Metric | Value |
|---|---|
| Entrypoint discovery (warm) | 12ms |
| Flow build (warm) | 0.1ms |
| Largest flow | 216 nodes / 357KB JSON |
| Disk cache | `.codecanvas/callgraph.json` + `entrypoints.json` |
| File count limit | 5,000 (CODECANVAS_MAX_FILES) |
| CPU throttle | 10ms yield every 50 files |

## Getting Started

```bash
# Install dependencies
pnpm install

# Build (webview + extension)
pnpm -r run build

# Run in VS Code
# Press F5 to launch Extension Development Host

# Run tests
python3 -m pytest tests/
```

### Requirements

- Node.js 18+
- Python 3.9+
- pnpm

## VS Code Commands

| Command | Description |
|---|---|
| `CodeCanvas: Analyze Project` | Analyze project + discover entrypoints |
| `CodeCanvas: Show Flow` | Visualize flow for selected endpoint |
| `CodeCanvas: Analyze Function Flow` | Flow from cursor position |
| `CodeCanvas: Trace Flow (Runtime)` | Execute HTTP request + trace |

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CODECANVAS_MAX_FILES` | 5000 | Max files to analyze |
| `CODECANVAS_BATCH_SIZE` | 50 | CPU throttle batch size |
| `CODECANVAS_THROTTLE_MS` | 10 | Sleep between batches (ms) |

## License

Private
