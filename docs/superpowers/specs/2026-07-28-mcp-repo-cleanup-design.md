# CodeCanvas 리포 MCP 단일 제품화 — 설계

날짜: 2026-07-28
상태: 승인됨

## 배경

원래 계획은 VS Code 익스텐션(React Flow 웹뷰)으로 시각화를 제공하는 것이었으나,
Python MCP 서버(`codecanvas-mcp`)로 제품 방향을 전환했다. PyPI 배포는 이미
완료된 상태(0.1.13, `uvx codecanvas-mcp`로 설치 가능)이지만, 리포는 여전히
익스텐션 중심으로 보인다. 홍보(레지스트리 등록, 해외/국내 커뮤니티) 전에
리포가 MCP 제품으로 보이도록 정리한다.

## 목표

- 리포 방문자가 3초 안에 "Python 코드베이스용 정적분석 MCP 서버"임을 인지한다.
- PyPI 빌드·배포 플로우는 전혀 건드리지 않는다 (`core/` 구조 유지).

## 결정 사항

- 익스텐션 코드는 **리포에서 제거** (git 히스토리로 보존, 복구 가능).
- `core/` 평탄화는 하지 않음 — 홍보 후 별도 판단.
- 레지스트리 등록·홍보 글 작성은 다음 세션.

## 변경 내용

### 1. 삭제

| 대상 | 근거 |
|------|------|
| `extension/`, `webview/` | 중단된 VS Code 익스텐션 (git 추적 51개 파일) |
| 루트 `package.json`, `pnpm-lock.yaml`, `pnpm-workspace.yaml` | pnpm 모노레포 잔재. `scripts/sync-core.mjs` 참조는 파일이 이미 없음 |
| `core/codecanvas/` | `codecanvas_mcp` rename(bd6245e) 잔재. git 추적 파일 0개, `.DS_Store`·`__pycache__`만 존재. 어떤 코드도 `codecanvas` 패키지를 import하지 않음 (검증 완료) |

### 2. 루트 README 재작성

MCP 제품 중심으로 전면 재작성:

1. 한 줄 피치 — 코딩 에이전트에게 grep 대신 콜그래프·제어흐름 기반
   ground-truth를 주는 정밀 정적분석 MCP 서버 (Python 전용)
2. Quick start — `claude mcp add codecanvas -- uvx codecanvas-mcp` +
   범용 MCP 클라이언트 JSON 설정
3. 툴 목록 — **실제 MCP 서버가 등록하는 툴 기준으로 작성**
   (구현 시 `codecanvas_mcp/mcp/server.py`에서 확인). 현재 파악된 12개:
   `list_entrypoints`, `find_symbols`, `who_calls`, `call_tree`, `what_does`,
   `function_flow`, `reaching_conditions`, `verify_claim`, `analyze_impact`,
   `simulate_state_transition`, `validate_state_schema`, `project_status`
4. 왜 필요한가 — grep/추측의 한계 vs 정적분석 근거 답변
5. 아키텍처·성능 간단 소개 (기존 README에서 유효한 내용만 발췌)
6. License

제거: VS Code Commands, 5 Visualization Views, Runtime Tracing, webview
아키텍처 등 익스텐션 관련 전부. `[server]` extra는 루트 README에서 언급하지 않는다.

`core/README.md`(PyPI 페이지용)는 현행 유지.

### 3. 검증

- `pytest` 전체 통과 (루트 `tests/` + `core/tests/`, pytest.ini testpaths 기준)
- `pip install ./core` 로컬 빌드 성공 — 배포 플로우 무손상 확인

## 범위 제외 (다음 세션)

- `codecanvas_mcp/server/`(FastAPI)·`tracer/` 코드 제거 여부 — `[server]`
  extra로 배포 중이므로 별도 결정
- `core/` 내용 루트 평탄화
- MCP 레지스트리 등록 (공식 Registry, Smithery, PulseMCP, mcp.so, Glama)
- 홍보 글 (Show HN, Reddit r/ClaudeAI·r/mcp, GeekNews 등)
