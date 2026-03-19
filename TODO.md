# CodeCanvas TODO

## 현재 초점

- [ ] 함수 단위 정적 CFG 구현
  basic block, true / false edge, loop back-edge, try / except edge를 명시적으로 표현

- [ ] Runtime trace 붙이기
  실제 요청 1회를 실행해서 실제로 탄 함수, DB, 외부 API, 예외 경로를 기록

- [ ] static + runtime merge 규칙 만들기
  가능한 경로와 실제 경로를 한 그래프에서 구분해서 보여주기

- [ ] Level 4 로직 레이어 고도화
  현재 top-level statement 요약에서 끝나지 않고 nested `if / try / except / for / while`까지 더 정확히 표현

- [ ] return / response origin 연결
  어떤 assignment / branch / return이 최종 응답을 만들었는지 표시

- [ ] CFG 위 actual path overlay
  nested branch 전체 중 실제 요청이 어느 edge를 탔는지 시각적으로 구분

## UI / UX

- [ ] semantic zoom 강화
  줌아웃은 설명, 줌인은 구조 탐색이 되도록 레벨별 정보량 다시 조정

- [ ] 함수 노드 드릴다운 UX 추가
  함수 클릭 시 Level 4 로직 노드가 자연스럽게 펼쳐지도록 상호작용 정리

- [ ] 리뷰 오버레이 추가
  인증, 예외, DB write, 외부 API, 불확실한 edge를 리뷰 신호로 표시

- [ ] evidence 패널 강화
  노드/엣지가 왜 존재하는지 파일/라인/근거를 더 읽기 쉽게 노출

## 분석 정확도

- [ ] attribute / dynamic dispatch 해석 개선
  현재 constructor-style 타입 추론을 넘어 더 많은 객체 메서드 연결 지원

- [ ] DB / HTTP 체인 호출 분류 강화
  `client.table(...).insert(...).execute()` 같은 체인을 더 정확히 분리

- [ ] side-effect step 분류 개선
  단순 expression과 의미 있는 상태 변경 / 외부 호출을 더 잘 구분

- [ ] false positive 회귀 테스트 확대
  실제 저장소 케이스를 테스트 픽스처로 추가

## 검증 대상 저장소

- [ ] `sample-fastapi` 외 실제 FastAPI 저장소 추가
- [ ] `ai-librarian/poc`를 회귀 테스트 입력으로 추가
- [ ] 인증 / 세션 / 스트리밍 / 외부 호출이 섞인 케이스 확보

## 제품 방향

- [ ] Code review mode 정의
  "코드를 안 읽고 1차 리뷰"를 위해 어떤 신호를 우선 노출할지 정리

- [ ] Risk scoring 설계
  인증 누락 후보, 예외 누락 후보, 외부 의존성 집중 구간, DB write 구간 점수화

- [ ] change impact 설계
  파일/함수 변경 시 어떤 요청 경로가 영향받는지 표시

## 문서

- [ ] `SPEC.md`를 구현 상태와 맞춰 주기적으로 업데이트
- [ ] `VISION.md` 기준으로 우선순위 판단 규칙 유지
- [ ] 데모 시나리오 문서 만들기
  "POST /login", "POST /chat" 같은 대표 흐름을 어떻게 보여줄지 정리
