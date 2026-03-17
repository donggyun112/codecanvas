# CodeCanvas v1 Specification

## Philosophy

AI 사용으로 인해 코드 생산량을 인간의 인지능력이 따라가지 못한다.
이를 시각적으로 표현하여 인간의 인지능력과 코드 생산량을 일치시킨다.

**핵심**: 코드로 만들어졌지만, 코드를 보지 않아도 되는 상태.
**한 줄 결론**: 코드 맵이 아니라, Python 요청을 읽을 수 있는 실행 설명기.

## v1 Scope

- Language: Python only
- Framework: FastAPI only
- Target: Local Git repository
- Input: HTTP method + path + headers + query + body
- Output: Request -> Route -> Dependency -> Service -> Repository/DB -> External API -> Serialization -> Response
- Each node shows: function name, description, branch conditions, latency, exceptions, DB/API calls, response summary
- Abstraction levels: Level 0~4

## Abstraction Levels

- **Level 0**: Client -> API -> DB/Cache/External API -> Client
- **Level 1**: auth router -> auth service -> user repo -> token service
- **Level 2**: routes.py -> service.py -> repo.py
- **Level 3**: login() -> verify_user() -> find_by_email() -> issue_token()
- **Level 4**: if expired, if user not found, except timeout

## Core Principles

- Graph facts are based on static analysis + runtime trace
- AI is used for description generation, summarization, node labeling, grouped abstraction
- AI does NOT determine graph structure alone
- Every node and edge has confidence and evidence
- Definite connections vs inferred connections must be visually distinguished (Python dynamic nature)

## Technical Design

### Code Parsing
- `ast` + `libcst` when needed

### Framework Interpretation
- Extract FastAPI routes, dependencies, middleware, exception handlers

### Static Graph
- Node types: package, file, class, function, branch, db, external_api

### Runtime Trace
- Middleware, decorator, SQLAlchemy hook, httpx/requests wrapper for event collection

### Description Generation
- Combine docstring, function name, type hints, comments, decorator metadata into natural language

### Storage
- v1: SQLite with tables: nodes, edges, trace_events, snapshots

### UI (VS Code Extension)
- Graph canvas + right detail panel + level slider + request input panel

## Screen Layout

- **Left**: Request selector
- **Center**: Flow diagram
- **Top**: Abstraction level slider
- **Right**: Selected node detail
- **Bottom**: Actual request/response, latency, exception, SQL/API logs

## User Flow

1. Load repository
2. Select endpoint
3. Input sample request or execute actual request
4. Generate diagram
5. Double-click to zoom in
6. Back to zoom out
7. Click node to view branch conditions and evidence

## Implementation Phases

### Phase 1: Static Endpoint Map
- Connect FastAPI routes to handlers
- Extract file/function/call relationships
- Generate Level 1~3 flow from code alone

### Phase 2: Request Execution Tracer
- Execute 1 actual request, record function calls, DB, external API, exceptions
- Highlight "actual path taken" not "possible paths"

### Phase 3: Description Layer
- Generate descriptions from docstring + function names
- AI-powered node summaries and upper-level grouping
- Evidence for each description

### Phase 4: Branch/Response Description
- Visually separate success/failure/exception paths
- Connect response fields to their origins

### Phase 5: Change Impact
- Highlight affected request paths when function/file is modified
- Partial re-analysis on save

## Out of Scope (v1)

- Django/Flask support
- 100% accurate static call graph
- Complete inference of all Python dynamic patterns
- Field-level complete data lineage
- Distributed system tracing

## Success Criteria

- End-to-end flow of POST /login visualized within 5 seconds on sample FastAPI project
- User understands "why this response was returned" from diagram alone
- Runtime trace and function paths visibly match
- Uncertain connections are explicitly marked, never hidden
