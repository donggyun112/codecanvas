# CodeCanvas

Python API 코드를 정적 분석하여 실행 흐름을 시각화하는 VS Code 확장.

함수 호출, 데이터 흐름, 분기 구조, 의존성 주입 체인을 자동으로 추적하고,
코드 변경이 어떤 API에 영향을 미치는지 분석합니다.

## 주요 기능

### 5가지 시각화 뷰

| 뷰 | 설명 |
|---|---|
| **Review Brief** | 리스크 스코어, 주요 관심사, 의사결정 포인트 요약 |
| **Code Flow** | 실행 순서대로 코드 블록을 보여주는 흐름도 (CFG 코드 + 의미 라벨) |
| **Data Flow** | 데이터가 어떻게 변환/전달되는지 (query, transform, validate, branch, respond) |
| **Call Stack** | 함수 간 호출 관계 (L0 트리거 ~ L4 문장 수준까지 드릴다운) |
| **CFG** | 제어 흐름 그래프 — 분기/루프/예외 경로를 소스코드와 함께 표시 |

### Change Impact Analysis

코드 변경 시 영향받는 API 엔드포인트를 자동 탐지합니다.

- sidebar의 **"Analyze Uncommitted Changes"** 버튼 클릭
- 변경된 함수 → call graph 역추적 → 영향받는 엔드포인트 목록
- `Depends()` 의존성 주입 체인도 추적
- 엔드포인트별 risk score + call depth 표시

### Runtime Tracing

실제 HTTP 요청을 보내고 실행 경로를 시각적으로 추적합니다.

- 각 노드에 HIT/MISS 배지 (실행됨/안 됨)
- 3-way 경로 분류: verified / unverified / runtime-only

## 아키텍처

```
extension/          VS Code 호스트 (TypeScript, tsup)
  ├─ src/extension.ts    활성화 + 커맨드 등록
  ├─ src/server.ts       Python 분석 서버 관리
  ├─ src/flowPanel.ts    Webview 패널 (flow 렌더링)
  └─ src/sidebar.ts      사이드바 (엔드포인트 목록 + Impact)

webview/            React Flow 캔버스 (React 19, Vite)
  ├─ src/App.tsx         메인 캔버스 + 뷰 전환
  ├─ src/transform/      뷰별 데이터 변환
  │   ├─ projection.ts       kind 기반 projection 유틸리티
  │   ├─ cfgTransform.ts     CFG 뷰
  │   ├─ codeFlowTransform.ts Code Flow 뷰
  │   ├─ executionTransform.ts Data Flow 뷰
  │   └─ visibility.ts       Callstack 뷰 필터링
  ├─ src/nodes/          노드 컴포넌트 (7종)
  ├─ src/edges/          스마트 엣지 (A* 경로 탐색)
  └─ src/layout/         ELK 레이아웃 + 분기 중앙 정렬

core/               Python 정적 분석 엔진
  ├─ codecanvas/graph/
  │   ├─ models.py       Canonical IR (FlowNode, FlowEdge, FlowGraph)
  │   ├─ builder.py      FlowGraph 빌드 파이프라인
  │   ├─ ast_execution.py AST → ExecutionGraph (의미적 실행 스텝)
  │   ├─ cfg.py          AST → ControlFlowGraph (분기/루프)
  │   ├─ impact.py       git diff → 영향 분석
  │   └─ execution.py    ExecutionGraph 모델 + L3 merge
  ├─ codecanvas/parser/
  │   ├─ call_graph.py   전체 프로젝트 call graph 빌드 + 디스크 캐시
  │   ├─ fastapi_extractor.py FastAPI 라우트/미들웨어/예외핸들러 추출
  │   └─ entrypoint_extractor.py API/스크립트/함수 엔트리포인트 탐색
  └─ codecanvas/server/
      └─ app.py          FastAPI 분석 서버 (5개 엔드포인트)
```

## Canonical IR

모든 시각화 뷰는 하나의 통합 그래프에서 projection으로 생성됩니다.

```
FlowGraph.nodes (kind 기반 분류)
  ├─ trigger / pipeline / file / function / statement   ← Callstack 뷰
  ├─ cfg_block                                          ← CFG 뷰 + Code Flow 코드 소스
  ├─ exec_l4                                            ← Data Flow (상세) + Code Flow
  └─ exec_l3                                            ← Data Flow (요약)
```

`projectByKind(flowData, kinds, edgeTypes)` 로 원하는 뷰의 노드/엣지만 추출.

## 성능

| 항목 | 값 |
|---|---|
| 엔트리포인트 탐색 (warm) | 12ms |
| Flow 빌드 (warm) | 0.1ms |
| 최대 Flow 크기 | 216 nodes / 357KB JSON |
| 디스크 캐시 | `.codecanvas/callgraph.json` + `entrypoints.json` |
| 파일 수 상한 | 5,000 (CODECANVAS_MAX_FILES) |
| CPU 쓰로틀 | 50파일마다 10ms yield (CODECANVAS_BATCH_SIZE / CODECANVAS_THROTTLE_MS) |

## 빌드 & 실행

```bash
# 의존성 설치
pnpm install

# 빌드 (webview + extension)
pnpm -r run build

# VS Code에서 실행
# F5 (Extension Development Host)

# 테스트
python3 -m pytest tests/
```

### 요구사항

- Node.js 18+
- Python 3.9+
- pnpm

## VS Code 커맨드

| 커맨드 | 설명 |
|---|---|
| `CodeCanvas: Analyze Project` | 프로젝트 분석 + 엔트리포인트 탐색 |
| `CodeCanvas: Show Flow` | 선택한 엔드포인트의 플로우 시각화 |
| `CodeCanvas: Analyze Function Flow` | 커서 위치 함수의 플로우 |
| `CodeCanvas: Trace Flow (Runtime)` | HTTP 요청 실행 + 런타임 추적 |

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `CODECANVAS_MAX_FILES` | 5000 | 분석 파일 수 상한 |
| `CODECANVAS_BATCH_SIZE` | 50 | CPU 쓰로틀 배치 크기 |
| `CODECANVAS_THROTTLE_MS` | 10 | 배치 간 sleep (ms) |

## 라이선스

Private
