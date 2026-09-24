# ADR-0002: 같은 interpreter의 자동 제어 대기

상태: 사용자 결정 C-D50~C-D52에 따른 설계 방향 수용, 2026-09-24.
개정 BRIEF 전체는 [현재 승인 기록](../../.workflow/core-workbench/runs/implement-p2.3-20260924/approval.json)으로 승인됐다. Bash/dash의 구현 가능성 검증은 남아 있다.
근거: [결정 이력](../features/core-workbench/DECISIONS.md),
[진단 기록](../features/core-workbench/DIAGNOSIS.md).

## 배경

현재 G2 후보는 shell의 READY·job event를 읽은 뒤 PTY에 자동 명령을 쓴다.
Event를 보낸 hook이 계속 실행될 수 있어, 관측 시점의 상태와 실제 명령 수신 시점의
상태가 달라진다. 같은 interpreter의 cwd·export·선택 환경을 보존하면서 이 경계를
바꾸어야 한다. 기존 실패와 사용자 결정은 진단 기록에서 구분한다.

## 결정한 방향

- 관리된 shell에서 일반 입력·job 경합과 상태 불명을 다룬다. 비호환 hook·trap
  변경은 자동 복귀를 보류한다. 같은 사용자 권한 코드가 내부 helper나 event를
  고의로 위조하는 공격을 막는 보안 경계를 제품 요구로 두지 않는다.
- 사용자가 같은 shell에서 직접 `wb-handoff`를 실행하고 상태를 확인하면,
  동일 interpreter가 prompt로 돌아가지 않고 전용 control 경로에서 자동 명령을
  기다리는 방향을 채택한다. Shell PID·cwd·환경을 보존한다.
- 인수 요청 즉시 새 자동 전송을 막고 인수 확인과 구분한다. 이미 전달된 명령의
  실행 중·종료·미확인 상태를 보존하며 자동 취소·rollback으로 표시하지 않는다.
  실행 중 실험은 유지하고 수동 입력은 확인된 현재 대상에 전달한다.
- 인수나 재접속 뒤 오래된 요청을 자동 replay하지 않는다. 인수 확인을 위해
  foreground 실험 종료나 prompt 복귀를 반드시 기다리는 정책은 아니다.

명시적 handoff는 수동 개입 뒤 반환 절차에 적용한다. 최초 자동화 시작의 위임 범위
승인·진행 지시 조건은 기존 C-D34/C-AC-29를 따른다.

## 검토한 대안

| 대안 | 판단 |
|---|---|
| 매번 일반 prompt로 복귀한 뒤 PTY에 자동 명령 입력 | 현재 event 검사 보강만으로 실행 경계를 입증하지 못했다. 사용자도 자동 제어 대기로 바꾸는 방향을 선택했다. |
| 같은 interpreter에서 control 요청 대기 | PID·cwd·환경 보존과 prompt hook 경합 축소를 함께 목표로 할 수 있어 우선 검증한다. 실제 job·signal·수동 전환 동작은 미검증이다. |
| 별도 runner shell에서 자동 명령 실행 | 상태 복제와 same-shell 약속 변경이 필요하다. 이번 결정으로 채택하지 않는다. |
| Native shell 통합·수정 | 더 강한 실행권 소유가 필요할 경우 검토할 기술 후보다. 이번 결정만으로 새 dependency나 shell 배포 방식을 확정하지 않는다. |

## 결과와 검증 경계

Control 경로는 보안 격리나 위조 방지 자체를 뜻하지 않는다. 신뢰 모델을 좁혔다는
이유로 현재 실패를 통과로 바꾸지 않는다. 정상 handoff 성공, 일반 입력·job 경합,
지원하지 않는 설정에서 보류, stdin과 control 경로 분리, Ctrl-C·loop 이탈,
제어권 인수 중의 요청 상태를 Bash와 실제 sh에서 검증해야 한다.

구체 framing, shell builtin·함수 구성, process 관측 방식과 signal 처리는 후속 설계와
판별 실험에서 정한다. 해당 실행 경계가 성립하지 않으면 G2를 통과로 표시하지 않고,
실패한 가정과 필요한 사용자 동작 변경을 다시 제시한다.
