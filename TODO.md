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

- [x] attribute / dynamic dispatch 해석 개선
  _resolve_call_in_expr에 full owner_parts/is_attribute_call 전달하여
  _resolve_attribute_call의 타입 체인 해석을 AST execution에서도 활용

- [ ] DB / HTTP 체인 호출 분류 강화
  `client.table(...).insert(...).execute()` 같은 체인을 더 정확히 분리

- [x] side-effect step 분류 개선
  unresolved bare expression을 inferred confidence로 emit하여
  logger/cache/event 등 side-effect 호출을 시각화에 포함

- [x] AST-based pipeline steps
  FlowGraph 의존 제거 — trigger/middleware/dependency/validation/serialization을
  handler의 decorator, Depends() 파라미터, return annotation에서 직접 도출

- [x] adaptive max_depth
  reachable call graph 크기에 따라 depth 4~8 자동 조절

- [x] resolution confidence 전파
  _resolve_call에서 ambiguous fallback시 inferred 마킹, step에 전파

- [x] multi-implementation DI resolution
  같은 패키지 우선, Depends() 바인딩 추론, test/mock 제외 휴리스틱

- [x] isinstance 타입 가드 narrowing
  if isinstance(x, Foo) 뒤에서 x 타입을 Foo로 좁혀 메서드 해석 정확도 향상

- [x] 타입 체인 끊김 보완
  _follow_attr_chain에서 property/method return annotation으로 중간 타입 추론

- [x] generator/yield 스텝 emit
  yield/yield from을 inferred confidence respond 스텝으로 시각화

- [x] decorator 파라미터 추출
  @limiter.limit("100/min") 등 파라미터 컨텍스트를 pipeline step에 포함

- [x] exception hierarchy 확장
  프로젝트 정의 예외 클래스를 동적 발견하여 CFG except 매칭에 등록

- [x] structural type inference
  타입 정보 없는 receiver에 대해 프로젝트에서 해당 메서드를 가진 유일한 클래스로 추론
  체인 호출(client.table().execute())에는 적용 안 함 — false positive 방지

- [x] getattr/setattr 리터럴 추적
  getattr(obj, "method") → obj.method() 호출로 변환, setattr → 타입 추적

- [x] whole-program call-site 타입 추론
  모든 호출처에서 같은 타입을 전달하면 파라미터 타입으로 전파

- [x] descriptor/property protocol
  @property return annotation, __get__ return type으로 속성 타입 추론

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
