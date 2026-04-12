# PyFlowLens

**Understand Python code without reading every line.**

PyFlowLens statically analyzes your Python (FastAPI) project and visualizes the entire execution flow — function calls, data transformations, branch logic, and dependency injection chains — in an interactive graph inside VS Code.

## What It Does

- **See how your API works** — one click shows the full execution path from HTTP request to response
- **See what breaks when you change code** — impact analysis traces your changes through the call graph
- **See which code actually ran** — send a real request and watch the execution path light up

## Views

### Review Brief
Get a quick summary: risk scores, key concerns, decision points, and error paths — before you dive into the code.

### Code Flow
Execution steps with **actual source code** inline. See what the code does in the order it runs.

### Data Flow
Follow the data: query → transform → validate → branch → respond. See how variables flow through your API.

### Call Stack
Classic function call graph. Drill down from the API trigger (L0) through services (L1-L2) to individual statements (L4).

### CFG (Control Flow Graph)
Branches, loops, and exception paths visualized with source code. Human-readable branch labels like `"user exists"` / `"user is None"` instead of generic yes/no.

## Change Impact Analysis

Changed a function? Click **"Analyze Uncommitted Changes"** in the sidebar to instantly see:

- Which functions you modified
- Which API endpoints are affected (including through `Depends()` chains)
- Risk score and call depth for each endpoint

## Runtime Tracing

Send an actual HTTP request from the sidebar and see which code paths were executed:

- **HIT** (green) — this code ran
- **MISS** (dimmed) — this code was skipped
- **RUNTIME-ONLY** — only seen at runtime, not in static analysis

## Quick Start

1. Install the extension
2. Open a Python (FastAPI) project
3. Run **`PyFlowLens: Analyze Project`** from the command palette (Ctrl+Shift+P)
4. Click any endpoint in the sidebar to visualize its flow

## Commands

| Command | Description |
|---|---|
| `PyFlowLens: Analyze Project` | Discover all API endpoints |
| `PyFlowLens: Show Flow` | Visualize selected endpoint |
| `PyFlowLens: Analyze Function Flow` | Right-click any Python function |
| `PyFlowLens: Trace Flow (Runtime)` | Send HTTP request + trace |

## Requirements

- Python 3.9+
- A FastAPI project (other frameworks coming soon)

## Feedback & Issues

Found a bug? Have a feature request?

- [GitHub Issues](https://github.com/donggyun112/codecanvas/issues)
- [Source Code](https://github.com/donggyun112/codecanvas)

Contributions and feedback are always welcome!
