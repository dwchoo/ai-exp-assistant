# 다음 세션에서 Core Workbench 구현 재개

아래 프롬프트를 저장소 `/home/dwchoo/ai-exp-assistant`를 연 새 세션에 입력한다. 모델은 사용자가 선택하며 이 문서는 모델을 변경하지 않는다.

```text
$implement docs/features/core-workbench

.codex/handoffs/2026-09-26-core-workbench-p26/HANDOFF.md를 handoff 수신 절차로 읽고 현재 파일·승인·ledger·host assignments와 대조한 뒤 구현을 재개해.
docs/features/core-workbench/DECISION-2026-09-26.md의 C-D55와 BRIEF r1.13 / SPEC s2.6 / PLAN p2.6을 현재 승인된 기준으로 적용해. 기존 r1.12/s2.5/p2.5 및 Oracle 보완 승인도 유효 범위에서 유지해.

- 환경은 사용자가 terminal에서 직접 준비하고 agent는 실행 명령을 전달한다. conda 전용 점검은 하지 마. cwd·PATH·exported environment 상속과 부모 상태 비전파, app 환경 분리·비호환 hook/trap 보류는 검증해.
- 자동 실험의 관리 대상 후손을 추적하고 수동 daemon의 소급 관리를 보장하지 마. 수명/대상이 불명확하면 unknown과 자동 후속 실행 보류를 유지해. 사용자 확인만으로 종료 완료 처리하거나 불명확한 프로세스를 자동 종료하지 마. cgroup은 필수화하거나 새 선택 기능으로 추가하지 마.
- Docker runtime 검증 생략을 유지하고 passed로 기록하지 마. suspend/resume·입력 반환·takeover/ACK-loss/no-replay·supervisor/FD/flush 장애 검증은 계속 필요해.
- 누적 child 호출 220회를 보존해. 호출 예산 산정·증액은 Root에게 위임했으므로 새 상한 승인을 반복해서 묻지 마. 과거 ledger의 user limit 220을 조용히 수정하거나 새 run에서 사용량을 초기화하지 마. C-D55의 도구 제약과 원본 보존 조건에 따라 명시적인 예산 전환을 완료하고, 필요한 유한 operating budget과 독립 검증 여유를 예약한 다음 dispatch해.
- 현재 CW-03과 core는 미완료다. 기존 검증의 재사용 가능 범위를 먼저 대조하고 남은 가장 작은 CW-03 검증 단위를 진행해. CW-02/04도 실제 근거와 대조하고 선행 gate 충족 전 CW-05 이후를 진행 가능으로 표시하지 마.
- 기존 사용자 변경·staged/unstaged/untracked 파일을 보존하고 한 workspace 한 writer를 지켜. 독립 test_designer와 fresh reviewer, 필요한 수정·최종 통합 검증을 포함해 완료 기준까지 진행해. 실제 격리가 검증된 경우에만 병렬 writer를 사용해.
- 정식 candidate/요구 identity/item/log 근거를 연결해. 과거 수기 요약이나 상태 flag만으로 전체 gate 통과를 주장하지 마. 코드 변경 뒤 graphify update .를 실행해.
- Commit/push/게시/배포는 하지 마. 합의된 제품 범위·지원 shell·필수 권한을 바꿔야 하는 실제 문제가 있을 때만 근거와 대안을 제시해.
```
