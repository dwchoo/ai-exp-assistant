# OMP Workbench

Manager OMP·worker OMP·host persistent terminal을 연결하는 작업 환경. Task Inbox & Web Board는 후속 Phase 2 기능이다.

## 기본 규칙

- 한국어로 응답하고 기술 용어·라이브러리·코드 식별자는 원문을 유지한다.
- 문서는 한국어, skill·MCP 관련 문서는 영어가 기본이다. 사용자가 다른 문서 언어를 요청하면 전환 전에 확인한다.
- 요청 범위만 최소한으로 변경하고 기존 사용자 변경을 보존한다. 합의되지 않은 기능·기술 선택을 확정하지 않는다.
- 관측 사실·사용자 결정·미결정을 구분하고, 변경에 맞는 최소 검증 결과를 보고한다.
- MCP 서버 실행·설치에 Docker를 사용하지 않는다. Commit·게시·배포는 별도 요청이 있어야 한다.

## 제품 경계

- 본체는 Python 중심으로 개발한다. Linux를 우선 지원하고 OS 의존 처리를 분리한다. Windows는 제외한다.
- 1차 core와 Phase 2를 구분한다. Phase 2 구현은 core 완성·검증 이후다.
- Workbench 내부에서 tmux 등 외부 terminal multiplexer를 실행·의존하지 않는다. 외부 tmux·herdr 안에서 실행할 수 있어야 한다.
- 외부 요청 접수는 실행 승인이 아니다. Manager의 승인 명세와 worker의 실행·결과 보고를 구분한다.
- 현재 요구사항은 [Core BRIEF](docs/features/core-workbench/BRIEF.md), 용어는 [CONTEXT.md](CONTEXT.md)를 확인한다. 참고 스펙과의 차이는 [대조표](docs/features/core-workbench/SOURCE-RECONCILIATION.md)를 따른다. 초안은 승인이 아니다.
- 실험 위임·worktree·수동 개입은 [제품 동작 규약](docs/features/core-workbench/OPERATING-CONTRACT.md)의 합의와 제안을 구분해 따른다.

<!-- BEGIN CODEX_ORCHESTRATION_WORKFLOW -->
## Codex feature workflow

Preserve repository safety/build rules. Read `.codex/workflow-contract.md` for the shared
handoff, scope, concurrency, Oracle and evidence contracts.

Use `$refine-spec` for an explicitly requested Astra requirements/design interview. Save the
agreed BRIEF plus glossary/selective ADR updates, not product code. In the fresh user-selected
Sol XHigh/Max planning session, `$to-tickets <brief>` creates SPEC and the dependency/resource
aware ticket bundle in one command. Do not implement or invent missing product decisions.

Use `$implement <feature>` for authorized development. Sol owns ready-ticket scheduling,
safe parallel workspaces, integration, independent `test_designer` tests, fresh `reviewer` review,
necessary fixes and final checks. Do not ask separately whether to test/review. Skills do
not switch models; development defaults to GPT-6 Sol XHigh unless the user selects otherwise.

Use `$ask-workflow` when asked what stage/command comes next. Inspect artifacts/evidence,
recommend one next action, and never execute that recommendation as part of navigation.

Use `$investigate-bug <symptom-or-incident>` only when the user explicitly invokes it.
Recommend Astra Root with oracle_senior advice and worker_senior experiments/fixes, without
switching models or settings. Diagnosis is the default; requested repairs include independent
verification. Do not invoke it automatically from implement, take over its ledger, or resume
implement after investigation. Reuse relevant evidence and return a bounded incident result.

`change-verification` is the internal verification procedure for implement and requested bug repair. Preserve explicit review-only
mode without writes. All custom agents are leaves. Root alone invokes `oracle` or
`oracle_senior` for concrete deep blockers, including worker help requests.

One mutation owner per physical workspace; simultaneous writers require verified isolation
and compatible shared resources. Root integrates serially and verifies the final combined
candidate. Root adjusts finite internal call budgets under contract section 8 without routine
approval waits; explicit user/host limits, user changes and authorization remain binding.
Never infer success from a status flag alone or publish/commit/deploy without separate authorization.
<!-- END CODEX_ORCHESTRATION_WORKFLOW -->
