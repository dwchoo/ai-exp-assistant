# 실행용 worktree 운영 가이드

CW-10은 승인된 TaskSpec revision의 명시적 commit에서 별도 실행용 worktree를 한 번 준비한다. 개발용 경로의 dirty·untracked 파일을 삭제하거나 stash하지 않고, 실행 실패 시 개발용 경로로 우회하지 않는다. Worktree는 보안 sandbox나 공유 자원 격리가 아니다.

## Manager: 기준 commit 준비

Manager는 위임된 변경만 확인해 local commit한다. `git status --short`로 사용자 변경을 구분하고, 허용된 경로만 `git add -- <경로>`로 stage한 뒤 `git diff --cached --name-only`와 `git diff --cached`를 검토한다. 관련 없는 변경이 stage되어 있으면 commit을 보류하고 소유자와 조정한다. Commit 후 full OID(`git rev-parse HEAD`), TaskSpec revision, 실행 command·기준·필요한 외부 입력과 환경을 worker에게 전달한다. 이 권한에 push·merge는 포함되지 않는다.

## Worker: 실행 준비

Worker는 명시적 full commit OID와 원본 repository, 원본 밖의 비어 있는 실행 경로를 확인한다. 일반 Git 명령의 예는 `git -C <source> worktree add --detach <execution-path> <full-commit-oid>`이다. 준비 뒤 실행 경로의 `git rev-parse HEAD`와 실제 cwd를 확인하고 기록한다. CW-10 API의 `prepare_execution_worktree`는 이 준비 한 단계와 확인만 수행하며, 기존 worktree의 삭제·수정·자동 재사용은 하지 않는다.

원본에 dirty·untracked 파일이 있어도 원본 상태를 보존한다. Commit은 외부 데이터, untracked 설정, 환경 전체를 고정하지 않으므로 입력·출력 경로와 공유 자원 충돌을 별도로 기록한다. 실행 중인 소스·설정을 바꾸려면 먼저 그 실험의 종료를 확인한다. 실행용 worktree와 결과는 자동 삭제하지 않는다.

준비에 실패하면 worker는 실패 단계·경로·Git 오류·원본 상태를 manager에게 보고한다. Manager는 승인 범위 안에서 새 실행 경로 또는 허용된 local commit을 준비할 수 있다. 해결 불가하거나 새 권한이 필요하면 사용자 확인을 요청한다. `stash`, `reset`, `clean`, 임의 worktree 삭제, 개발용 경로 실행, 무단 push·merge는 대안이 아니다.

## 결과와 재지시

Workbench는 실제 host shell의 수락·시작·종료 또는 unknown, exit status, cwd·commit·환경, raw log와 결과 위치를 run에 연결한다. 종료와 입력 반환이 확인된 뒤에만 worker가 사전 기준과 파일 출처·내용·미확인을 대조해 성공·실패·판단 보류를 보고한다. Exit 0만으로 성공을 확정하지 않는다. 일시정지 중 종료·로그 수집은 계속하지만 새 자동 worker 판단·보고는 시작하지 않는다.

Worker가 코드 수정 필요성을 근거와 함께 보고하면 그 지시는 끝난다. 수정 뒤 같은 작업은 자동으로 되돌아오지 않으며 manager의 새 승인된 지시가 필요하다. Worker 직접 요청 중 범위 확대는 manager 확인 전 실행하지 않는다.

## Worker 응답·현재 권한·비밀값 경계

실제 `TaskMailbox` 경로에서는 `OMP_PROCESSED`를 worker의 실행 결정이나 판단으로 대체하지 않는다. 호출자는 공개 OMP assistant 응답을 관측하는 `WorkerRolePort`를 주입하고, Workbench는 Task/revision/run/message/delivery attempt/session/generation과 응답 event sequence가 맞는 실행 결정 뒤에만 host shell에 보낸다. 종료 후에는 worker에게 증거 사실을 별도 QUESTION으로 보내고, 결속된 assistant 판단이 확인된 경우에만 REPORT를 보낸다. 응답 누락·identity 불일치·전달 unknown은 성공으로 우회하거나 자동 재전송하지 않는다.

새 shell dispatch와 worker 분석·보고 직전에는 현재 `AutomationState` 및 CW-09의 active run·승인·철회 상태를 다시 읽는다. 일시정지 중에는 종료와 로그 수집을 계속할 수 있으나 새 분석은 보류한다. 별도 SQLite 상태와 PTY 전송 사이의 완전한 원자성은 보장하지 않으며, dispatch 이후 metadata 장애 처리는 CW-14 통합 경계에 남는다.

Worker 직접 요청은 `route_worker_request`에서 durable TaskSpec revision에 연결한다. 기존 승인 범위 요청도 새 revision으로 기록하고 manager의 재승인·새 proceed를 기다린다. 작은 새 요청은 새 Task를 만들고, 범위 확대는 기존 Task의 새 revision을 만들지만 어느 경우도 요청만으로 실행하지 않는다.

승인 TaskSpec에는 환경 변수 이름만 넣고 값은 실행 호출의 일시적 `environment_values`로 전달한다. `run.json`·SQLite shell event·worker REPORT에는 변수 이름만 기록하며 값을 기록하지 않는다. 실험 command가 비밀값을 직접 출력하면 raw terminal 출력에도 나타날 수 있으므로, 비밀값 출력 자체를 금지하거나 별도 데이터 취급 정책이 필요하다.
