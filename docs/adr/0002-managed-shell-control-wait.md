# ADR-0002: 부모 shell control 대기와 별도 실험 supervisor

상태: C-D50~C-D54의 기존 승인에 C-D55 환경·관측 경계 결정을 반영, 2026-09-26. 구현·runtime 검증은 미완료다.
기존 r1.12/s2.5/p2.5 구현 승인을 유지하고 [C-D55](../features/core-workbench/DECISION-2026-09-26.md)를 r1.13/s2.6/p2.6에 반영한다. 현재 문서화 작업은 구현 재개가 아니다.
근거: [결정 이력](../features/core-workbench/DECISIONS.md), [진단 기록](../features/core-workbench/DIAGNOSIS.md), [A 방식 토의](../../.workflow/core-workbench/notes/2026-09-24-local-lifetime-draft.md), [Oracle 검토](../../.workflow/core-workbench/runs/oracle-review-20260924/REVIEW.md).

## 배경

초기 G2 후보는 `wb-handoff` 뒤 같은 shell interpreter의 함수가 실험 script를 직접 `eval`했다. 이 후보는 부모 PID·cwd·환경을 보존했지만 Bash/dash의 `return`과 Ctrl-C에서 control loop를 벗어나거나 tail을 실행했고, queued PTY 입력이 부모 prompt 명령으로 유출됐다.

EOF·WAIT recovery channel, epoch/token, slave flush, ACK 뒤 fresh READY는 일부 정상 경로에서 통과했다. 이 근거는 보존하지만 RUN 직접 평가와 입력 반환까지 안전하다는 증거로 확대하지 않는다. `TCIFLUSH`도 shell parser가 이미 읽은 byte나 Workbench 송신 queue를 비웠다고 증명하지 않는다.

## 결정

다음 구조를 채택한다.

```text
persistent Bash/sh parent
└─ fixed supervisor/subreaper
   └─ experiment interpreter or executable
      └─ experiment descendants
```

- 사용자가 terminal에서 직접 준비한 부모 shell의 PID·cwd·PATH·exported environment를 유지하고 실험 자식에 상속한다. conda 전용 점검과 환경 관리자별 activation/deactivation 보장은 core 완료 조건에서 제외한다. 일반 환경 상속·비전파·app 환경 분리는 검증한다.
- 수동 작업 뒤 `wb-handoff`를 실행하면 부모 interpreter는 prompt로 돌아가지 않고 control 대기에 머문다.
- 부모 control 경로는 고정 helper만 시작한다. 실험 script 전체는 별도 interpreter 안에서 평가하고 단순 argv는 불필요한 shell 재해석 없이 실행할 수 있다. Pipeline·명령 치환·redirection도 supervisor가 관측하는 실험 실행 범위 안에 둔다.
- 실험 내부 cd·export·source 효과는 부모 shell에 자동 반영하지 않는다. 다음 실험에도 필요한 변경은 부모 준비 상태에 명시적으로 적용한다.
- Supervisor는 실험 시작 전에 subreaper 설정 성공을 확인하고 한 실험만 담당한다. 주 프로그램 반환과 전체 관리 대상 후손의 종료·회수 또는 수명 `unknown`을 구분한다.
- 인수 요청 즉시 새 자동 전송을 막고 요청과 확인을 구분한다. 기존 실험은 유지하며 확인된 foreground 대상에만 수동 입력을 보낸다. 오래된 요청은 replay하지 않는다.
- 준비된 host 환경에서 subreaper 경로를 우선 검증한다. 이번 Docker runtime 검증은 생략하고 container 지원 근거는 pending으로 둔다.

## 먼저 검증할 경계

이 결정은 아래 기술을 확정하지 않는다. CW-03의 최소 합성 probe가 한 후보에서 함께 판별한다.

1. **입력 반환 장벽:** 전체 실험 수명이 끝난 뒤 supervisor가 반환 대기에 남고, backend가 key·paste·부분 write·terminal query reply를 포함한 이전 writer를 회수·차단한 다음 부모 PTY 읽기를 재개한다. 같은 부모 shell의 실제 읽기·prompt 복귀와 새 정상 입력 처리 뒤에도 이전 marker가 생기지 않아야 하며, 알려진 유출 후보에서는 marker가 생겨야 한다. `wb-handoff`와 후속 줄을 한 write/paste로 보내는 반례와 foreground 종료 직후 write 경합을 포함한다.
2. **Signal과 process group:** 시작·실행·반환 장벽·부모 WAIT에서 SIGINT·SIGQUIT·SIGTSTP·SIGCONT를 시험한다. 먼저 공유 foreground process group 후보를 검증하고, 관측자 정지나 job-control 충돌이 확인될 때만 supervisor/experiment process group 분리를 제한적으로 비교한다.
3. **Subreaper 수명:** 단일 reaper 권한, wait 범위, SIGCHLD disposition, 자식 signal disposition 상속·정규화, 추가 spawn 폐쇄 조건을 기록한다. 주 프로그램보다 오래 사는 double-fork/setsid 후손, 빠른 fork/exit, 주 프로그램 종료와 후손 생성 경합, supervisor 사망과 control/event FD 손실을 시험한다. `WNOHANG=0`이나 무조건적인 `ECHILD`를 전체 종료로 해석하지 않고 관측 범위 밖 생성을 완료로 숨기지 않는다.
4. **수동 작업 공백:** Supervisor 이전 수동 단계에서 생성된 daemon을 subreaper가 소급 관리한다고 가정하지 않는다. 관측 불명 잔존이 있으면 이유를 표시하고 자동 handoff를 보류하며 사용자가 정리한 뒤 재확인한다. 정상 수동 준비 사례는 성공해야 한다. 모든 수동 daemon 발견을 보장하지 않고 사용자 확인만으로 unknown을 종료 완료로 바꾸거나 귀속 불명 프로세스를 자동 종료하지 않는다.

공유 foreground process group과 분리 process group 중 하나를 이 ADR만으로 확정하지 않는다. 정확한 signal 전달, `tcsetpgrp`, SIGTTIN/SIGTTOU, suspend/resume 동작은 합성 probe 결과로 정한다.

## 대안과 이력

| 대안 | 판단 |
|---|---|
| 일반 prompt에 자동 명령 입력 | Prompt/readiness 관측과 실제 명령 수신 사이의 경합을 해결하지 못해 채택하지 않는다. |
| 같은 interpreter control 대기에서 실험 script 직접 eval | C-D51 당시 우선 검증 방향이었다. RUN 탈출과 입력 유출이 관측됐고 C-D54의 A 방식이 실험 평가 부분을 대체한다. 부모 control 대기 자체는 유지한다. |
| 별도 실험 interpreter/executable + 고정 supervisor/subreaper | 부모 준비 환경을 상속하면서 실험 상태 변경과 수명 관측을 실행 단위 안에 묶을 수 있어 채택한다. 입력 반환·signal 결합 검증은 남아 있다. |
| 실행별 delegated cgroup | C-D55: core 필수 조건으로 추가하지 않는다. 이번 결정으로 선택적 cgroup 기능도 추가하지 않으며 실제 한계 때문에 필수 권한·사용자 동작 변경이 필요할 때 근거와 대안을 제시한다. |
| supervisord | Subreaper 우선 경로보다 실질적인 기능 이점이 확인될 때만 검토한다. 채택 자체로 PTY·입력·전체 후손 종료가 입증되지는 않는다. |

## 결과와 검증 경계

기존 CW-01 계약·ticket ID와 변경 영향이 없는 G1/G3 근거는 보존한다. 기존 G2 recovery 통과도 해당 입력과 후보에 한정해 유지한다. 직접 eval 실패, 입력 유출, signal 오류와 수동 잔존 공백은 새 후보의 음성 대조로 남긴다.

CW-03은 입력 반환·signal·subreaper를 결합한 최소 판별을 먼저 통과한 뒤 환경·인수·장애·launch matrix로 확장한다. CW-05는 CW-02~04의 현재 통합 후보와 required gate item을 집계하기 전 production frontier를 열지 않는다. CW-07은 검증된 부모 control 대기와 별도 실험 실행 계약만 소비한다.

같은 PID 복구나 수동 작업 관측이 불가능해 사용자 입력 방식·shell 지원·필수 권한을 바꿔야 할 때만 실패 근거와 대안을 다시 결정받는다. 기존 승인과 C-D55 범위의 다음 세션 구현을 위해 현재 상태·회계·근거를 먼저 대조한다.
