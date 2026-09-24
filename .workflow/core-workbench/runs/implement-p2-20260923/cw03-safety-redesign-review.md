# CW-03 같은 shell 자동 입력 안전 경계 재설계 검토

상태: **진단 완료, G2 blocked 유지**. 2026-09-23 사용자 답변
`안전 경계 재설계 검토`에 따른 read-only 검토다. 제품 코드, 승인된
BRIEF/SPEC/PLAN, 수락 조건은 바꾸지 않았다.

## 승인된 경계

C-D48은 수동 입력 뒤 사용자가 **같은 shell**에서 직접 `wb-handoff`를
실행하고, 미제출 입력·job·cwd·상태를 확인한 뒤에만 자동 재개를 검토한다.
불명확한 상태는 자동 입력을 보류한다. G2는 실제 Bash 5.x와 host의 sh 구현인
dash에서 같은 shell의 cwd/export/선택 환경과 자동 실행 핵심 흐름을 보여야
한다. 정상 경로까지 항상 차단하는 방식으로 gate를 통과할 수 없다.

## 확인한 실패 원인

- 현재 `InputBoundary.can_dispatch()`는 FD 9에서 읽은 `READY`,
  `JOBS_END:READY`, `HOOK_OK:HANDOFF` 등으로 clean 상태를 만든 뒤 PTY에
  명령을 쓴다. 이 event를 내는 함수와 prompt hook은 사용자 명령이 실행되는
  같은 shell 안에 있다.
- 독립 실제 shell 테스트 네 개는 Bash `builtin` override, dash `jobs`
  override, dash parent job의 PS1 subshell 누락, Bash hook의 READY 선발행 뒤
  job 시작을 재현한다. 현재 checkout에서 26개 중 4개 테스트가 실패한다.
- 네 번째 후보의 Linux `/proc` 검사도 마지막 반례에서 job이 생기기 전
  `can_dispatch=True`가 됐다. Process snapshot의 빈 결과는 실행 중인 hook이
  **앞으로** job을 만들지 않는다는 보증이 아니다.
- 별도 작은 shell 확인에서 `command jobs`는 `jobs()` override를 우회했으나
  Bash·dash 모두 `command()` 자체를 재정의할 수 있었다. Bash `DEBUG` trap은
  별도 `guard` 뒤 실제 명령 전에 job을 시작할 수 있었다. 이 관측은
  `jobs` 호출 형태만 바꾸거나 검사와 실행을 두 명령으로 이어 붙이는
  수정을 안전 경계로 삼을 수 없음을 보여준다.

GNU Bash는 `PROMPT_COMMAND`를 primary prompt 출력 전에 실행하며,
`DEBUG` trap을 simple command 전에 실행한다고 명시한다:
[interactive shell](https://www.gnu.org/software/bash/manual/html_node/Interactive-Shell-Behavior),
[DEBUG trap](https://www.gnu.org/software/bash/manual/html_node/Bourne-Shell-Builtins).

## 재설계 판정

Stock shell의 내부 실행을 통제하지 않고 PTY 입력, shell 작성 event,
Linux `/proc` snapshot만으로는 다음 입력의 소비자가 top-level shell인지와
검사부터 명령 시작까지 hook·trap이 끼어들지 않는지를 일반적으로 보증할
수 없다. 한 줄 guarded command를 미리 PTY에 넣으면 실행 중인 hook의
`read`가 그것을 소비할 수 있다. Top-level에 도착해도 검사와 `eval`은
atomic하지 않으며 shell 함수·builtin lookup은 바뀔 수 있다. 이 판정은
현재 방식의 한계이지 모든 native shell integration의 불가능성 증명이 아니다.

필요한 불변식은 **같은 interpreter의 신뢰 가능한 실행기가 입력을 독점한
상태에서 owner/generation·미소비 입력·job·cwd를 확인하고, 확인부터 명령
시작까지 사용자 hook·trap·다른 입력 소비자가 끼어들지 못하게 하는 것**이다.
보호된 control 대기 지점과 실행 전환을 Bash/dash runtime 또는 그에 준하는
신뢰 경계에서 제공할 수 있는지 먼저 입증해야 한다. 현재 후보의 shell
function과 공개 FD 번호만으로는 보호가 성립하지 않는다.

## 다음 판별 실험과 중단 기준

보호된 실행 지점을 제안하는 경우, 실제 Bash와 dash에서 다음을 먼저
재현한다.

1. 같은 shell의 hook이 READY와 빈 job 결과를 보낸 뒤 PTY `read`에
   머무르게 한다. 이 시점에 job은 없다.
2. Manager가 무해한 자동 요청 하나를 시도한다. Hook이 실행 중인 동안
   **자동 PTY write 0회, 요청 수락·시작 0회**여야 한다.
3. 사용자 입력으로 hook을 해제하고 job을 시작한다. 오래된 요청이 자동
   실행되면 실패다.
4. 정상 대조군에서는 `cd`·`export`·선택된 환경을 거친 뒤 직접
   `wb-handoff`를 실행하고, 동일 shell PID/cwd/환경에서 자동 명령이
   시작돼야 한다.

이 시험을 통과해도 `DEBUG`/signal trap, 함수 override, 미소비 parser
입력, lifecycle event 위조를 별도로 검증해야 한다. 보호된 실행 지점이
제시되지 않으면 다섯 번째 제품 수정으로 넘어가지 않고 G2를 blocked로
유지한다.

## 요구사항 결정 경계

검증 불가능한 shell 변경 뒤 `USER`/`NEEDS_REVIEW`에 남기는 것은 C-D48의
기존 fail-closed 동작이다. 하지만 stock sh 자동 흐름을 제외하거나, 다른
shell로 실행하거나, 정상 `cd`/`export`/선택 환경 뒤의 자동 재개까지 없애는
것은 승인된 동작 축소이므로 별도 사용자 결정이 필요하다. `source`나
conda/venv activation은 임의 shell 코드를 실행할 수 있으므로 단순
`export`와 같은 신뢰 등급으로 간주하지 않는다.

근거: `tests/gates/g2_shell/test_g2_shell.py`,
`cw03-fourth-blocked-candidate.json`, `cw03-post-ready-independent-test.json`,
`resume-unsafe-candidate-recovery.json`. 이번 진단에서는 제품 테스트를
재실행하거나 제품 파일을 수정하지 않았다.
