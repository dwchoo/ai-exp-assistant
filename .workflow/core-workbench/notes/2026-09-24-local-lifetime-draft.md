# Docker 환경의 로컬 실험 종료 관측 — 1차 토의 초안

- 작성일: 2026-09-24
- 갱신일: 2026-09-24. 후속 토의의 A 방식 선택·환경 전제·기술 선택 방향을 반영했다.
- 상태: 주요 사용 기준과 우선 검증 방향 결정. 구체 구현·지원 matrix는 미검증이며 이번 요청은 문서 갱신이다.
- 목적: 이번 대화의 사용자 요구와 결정, 설계 제안, 검증 과제, 남은 질문을 보존한다.
- 위치: `.workflow/core-workbench/notes/`. 승인된 요구·계획 문서와 구분하는 임시 보관 위치다.
- 작업 범위: 이 초안을 갱신한다. 기존 BRIEF·SPEC·PLAN·DECISIONS·workflow 상태를 개정하지 않는다.

2026-09-24 구현 재개 중 사용자는 현재 Docker 검증은 생략해도 된다고 지시했다.
따라서 이번 검증은 host에서 가능한 subreaper·terminal 경계부터 진행하며,
Docker/container 동작은 미검증으로 남긴다. 이 일정 변경은 container 사용 목표나
추가 특권을 요구하지 않는 기준을 폐기한 결정이 아니다.

## 1. 사용자가 확인한 범위와 결정

| 구분 | 근거와 의미 |
|---|---|
| 실행 위치 | 사용자는 “외부에서 작업이 들어와서 로컬로 구동할 일만 있지. 원격 작업은 없는데”라고 설명했다. 외부 요청 접수와 로컬 프로그램 실행을 구분한다. 원격 실행은 이번 문제의 범위가 아니다. |
| 주요 사용 환경 | 사용자는 “docker container 내부에서도 이 omp-workbench를 설치해서 해야하는 경우가 많아”라고 밝혔다. Container 내부 사용성을 고려해야 한다. 구체적인 Docker 실행 옵션·버전·rootless 지원 범위는 아직 정하지 않았다. |
| 환경 준비 전제 | 사용자는 “사용자가 미리 설정한 환경을 기준으로 하는데, 도커 컨테이너나 실행할 기본적인 환경은 미리 셋팅이 되어 있다고 가정해줘”라고 지시했다. Workbench는 준비된 실행 환경을 사용한다. 이번 문제 해결에 container 생성·기본 환경 구축을 포함하지 않는다. |
| A 방식 선택 | 사용자는 “A가 좋겠는데”라고 선택했다. 사용자가 준비한 cwd·환경을 실험에서 사용하고, 실험 내부의 상태 변경은 해당 실험에 적용한다. 실험 내부 cd·export 등의 효과를 다음 실험의 부모 shell 상태로 자동 반영하지 않는다. |
| 실험의 종료 기준 | “자동 실험이 끝난 뒤에도 그 실험이 띄운 서버·background 프로세스를 의도적으로 계속 살려 두어야 하나요?”라는 질문에 **“기본적으로 모두 종료되어야 함”**을 선택했다. |
| 기술 선택 방향 | 사용자는 “가능하면 subreaper를 사용하면 좋겠는데, 기능 차이가 맣이나면 supervisord를 설치하고 사용하는 것도 좋을거 같아”라고 밝혔다. Subreaper 기반 관리 프로세스를 우선 검증한다. 외부 도구의 필요성과 실질적인 이점이 확인되면 설치 의존성도 허용하는 방향이다. Supervisord 채택·설치 자체를 결정한 것은 아니다. |
| 기록 요청 | 사용자는 토의 후 “1차 문서를 어딘가에 남기면 좋겠는데 임시 장소로 어디가 좋을까? 그리고 거기에 남겨줘.”라고 요청했다. |
| 후속 정리·갱신 요청 | 추가 필수 결정 없이 기존 원칙으로 검증 기준을 잡고, 사용 조건 변경이 필요할 때 다시 결정받는다는 설명 뒤 사용자는 “좋아 이제 결정된 것을 아까 만든 문서에 업데이트 해줘.”라고 요청했다. |

종료 기준의 해석: 실험이 만든 관리 대상 로컬 프로세스가 모두 종료되어야 그 실험의 실행 종료로 인정한다. Multiprocessing·background 사용 자체를 금지하는 결정은 아니다. 주 프로그램 종료 즉시 남은 자식을 강제 종료하라는 결정도 아니다.

실험이 사용한 persistent shell과 Workbench 자체는 실험 종료 때 함께 종료할 대상이 아니다. 관측 범위 밖의 작업이 섞여 전체 종료를 확인할 수 없으면 unknown으로 남긴다.

이제 A 방식과 subreaper 우선 검증 방향을 미결정 질문으로 반복하지 않는다. 구체적인 실행 API·패키징·cgroup 병용 방식은 이 기준 안에서 검증할 구현 사항이다. 사용 기준의 결정은 기술 검증 통과를 뜻하지 않으며, 이번 문서 갱신 요청만으로 설치·구현·runtime 시험을 실행하지 않는다.

## 2. 기존 요구와 문제의 원인

[C-D53](../../../docs/features/core-workbench/DECISIONS.md)에 따라 shell의 명령 평가 반환, 관리 대상 로컬 프로세스 종료, worker·manager의 실험 결과 판단을 구분한다. 실제 종료 확인 전에는 해당 실험의 소스·설정 수정과 재실행을 진행하지 않는다.

로컬에서 `python experiment.py &`를 실행하면 shell은 먼저 반환해도 프로그램은 계속 실행할 수 있다. 프로그램이 자식을 남기고 종료하면 부모 관계도 바뀔 수 있다. 따라서 shell 반환이나 shell job 목록만으로 전체 종료를 추정할 수 없다.

기존 [G2 기록](../../../docs/features/core-workbench/gates/G2.md)에는 재부모화된 로컬 daemon이 살아 있는데 완료로 표시된 반례가 있다. [Host capability 기록](../runs/implement-p2.3-20260924/cw03-cgroup-capability.json)은 임시 delegated systemd user scope에서 같은 shell PID를 유지하며 cgroup 이동과 populated 변화를 관찰한 결과다. 모든 Linux·Docker 환경의 지원 증거는 아니다.

이번 토의에서는 runtime 시험을 추가 실행하지 않았다. G2의 shell 제어권 복구 등 남은 검증을 통과한 것으로 취급하지 않는다.

기존 [ADR-0002](../../../docs/adr/0002-managed-shell-control-wait.md)와 [제품 동작 규약](../../../docs/features/core-workbench/OPERATING-CONTRACT.md)의 terminal 제어·환경 보존 요구를 기준으로 삼는다. 후속 A 결정은 실험 내부의 상태 변경을 부모 shell에 반영할 필요가 없음을 명확히 한다. 정식 문서와의 실행 방식 정합성은 후속 반영 때 대조한다.

- 같은 persistent Bash/sh의 PID·cwd·export·검증된 conda/venv 상태를 유지한다.
- 수동 인수 요청 즉시 새 자동 전송을 막고, 인수 요청과 실제 확인을 구분한다.
- 실행 중 실험을 유지하며 확인된 foreground 대상에 수동 입력을 제공한다.
- 인수나 재접속 뒤 오래된 실행 요청을 자동 replay하지 않는다.
- 같은 사용자 권한의 고의적인 내부 함수·event 변조를 막는 보안 격리는 요구하지 않지만 정상 입력·job 경합은 검증한다.

## 3. 결정된 전제에 따른 지원 목표와 검증 기준

사용자가 준비한 Linux·Docker 환경과 A 방식을 기준으로 다음 구현 목표를 검증한다. 아래 환경에서 이미 동작함을 입증한 목록은 아니다. Docker·kernel·shell별 지원 matrix는 실제 시험 결과로 정한다.

| 항목 | 제안 |
|---|---|
| 기본 환경 | Linux host와 일반 Docker container를 주요 지원 대상으로 검증한다. |
| 환경 준비 | 사용자가 미리 설정한 container·실험 환경을 사용한다. Workbench는 필요한 기능의 사용 가능 여부를 검사한다. |
| Container 검증 기준 | cgroup read-only, systemd 없음, 기본 seccomp, 추가 특권 없는 환경에서 시작한다. |
| 설치 조건 | 기본 자동 실행에 `--privileged`, host cgroup 쓰기 권한, Docker socket 마운트를 요구하지 않는다. |
| 외부 도구 | Subreaper 기반 내부 관리 프로세스를 우선한다. 외부 도구가 필요한 기능과 실질적인 이점을 제공하면 설치 의존성을 허용한다. |
| 자동화 | cgroup이 없다는 이유만으로 정상적인 모든 실험을 unknown에 남기는 방식은 기본 사용성 목표를 충족하지 못한다. |
| cgroup | 사용할 수 있는 환경에서 선택적으로 활용한다. 실제 생성·이동·관측 capability를 확인한다. |
| 종료 불명 | 해당 실험의 자동 완료·수정·재실행을 보류하고 이유를 표시한다. 로그 수집과 확인 가능한 수동 조작을 유지한다. |
| 결과 | Exit 0이나 로컬 프로세스 종료만으로 실험 성공을 확정하지 않는다. |

Docker가 container를 cgroup으로 관리하는 것과 내부 프로그램이 실행별 하위 cgroup을 만들 수 있는 것은 별개다. runc의 [기본 예시 설정](https://github.com/opencontainers/runc/blob/main/libcontainer/specconv/example.go)은 `/sys/fs/cgroup`을 read-only로 마운트한다. 실제 환경의 mount·권한·seccomp 설정은 따로 확인해야 한다.

## 4. 솔루션 후보 비교

| 후보 | 장점 | 한계 | 추천 용도 |
|---|---|---|---|
| 실행별 cgroup | 부모 관계가 바뀐 프로세스를 같은 그룹에서 관측 | Container 내부 생성·이동 권한이 보장되지 않음 | 사용 가능한 환경의 종료 관측 |
| 실험별 supervisor + subreaper | 재부모화된 후손을 받아 종료까지 기다릴 수 있음 | 실험 진입점을 supervisor 아래로 모아야 함 | 기본 방식의 우선 검증 후보 |
| supervisord | 설정된 프로그램의 실행·중단·재시작·로그 관리 제공 | 기본 프로세스 상태만으로 모든 후손 종료를 보장하지 않으며 PTY 통합 필요 | 필요한 기능과 통합 이점이 확인될 때 검토 |
| Persistent shell 전체의 supervisor | 기존 shell 구조를 유지하기 쉬움 | Shell이 계속 살아 있고 수동 작업도 섞여 실행별 종료 구분이 어려움 | 단독 해결책으로 확정하지 않음 |
| jobs·process group·PID 목록 | 진단·입력 대상 확인·중단에 활용 | 재부모화·생성·종료 경합으로 전체 종료를 보장하기 어려움 | 보조 관측 |

`cgroup.events`의 `populated=0`은 해당 그룹과 하위 그룹에 살아 있는 프로세스가 없다는 뜻이다. 실행 전에 대상을 넣고, 관측 범위를 벗어나는 이동이 없다는 조건이 필요하다. 그룹 삭제·읽기 실패를 빈 그룹으로 해석하지 않는다. [Linux cgroup v2 문서](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html#un-populated-notification)

`/proc/.../children`은 읽는 도중 자식이 종료하면 살아 있는 다른 자식도 누락될 수 있다. 여러 번 빈 목록을 관측했다는 사실만으로 종료를 증명하지 않는다. [Linux 매뉴얼](https://man7.org/linux/man-pages/man5/proc_tid_children.5.html)

여기서 supervisor는 Workbench에 포함할 실험 관리 프로세스의 역할 이름이다. 외부 제품인 `supervisord`를 뜻하지 않는다. Subreaper는 Linux kernel 기능이므로 별도 패키지 설치가 필요하지 않다. 다만 container 보안 설정에서 호출이 허용되는지는 검사해야 한다. [Linux 매뉴얼](https://man7.org/linux/man-pages/man2/PR_SET_CHILD_SUBREAPER.2const.html)

Supervisord 공식 문서는 관리 프로그램을 daemon으로 분리하지 않고 foreground에서 실행하도록 안내한다. `stopasgroup`·`killasgroup`은 process group에 signal을 보내는 설정이며, 그룹을 벗어난 후손까지 포함한 전체 종료 증명은 아니다. [프로세스 관리](https://supervisord.org/subprocess.html), [그룹 중단 설정](https://supervisord.org/configuration.html#program-x-section-values)

현재 supervisord 구현은 자식의 표준 입출력을 pipe에 연결하고 별도 process group을 만든다. Workbench의 PTY·수동 인수 요구에 맞춘 통합을 별도로 검증해야 한다. 외부 도구를 채택하더라도 종료 판정·입력 안전 검증을 대신한 것으로 보지 않는다. [Supervisor 구현](https://github.com/Supervisor/supervisor/blob/main/supervisor/process.py)

## 5. 우선 검증할 구조 — 실험별 supervisor

Persistent shell이 사용자가 준비한 환경을 유지하고, 실험을 시작할 때 전용 supervisor를 실행한다. Supervisor는 해당 실험의 실행 파일 또는 명시적인 script를 자식으로 시작한다. A 방식에 따라 실험 내부의 상태 변경은 해당 실행 범위에 적용한다. 기존 persistent shell은 유지한다.

아래 `wb-run`은 설명용 가칭이며 현재 구현된 제품 명령이 아니다.

```sh
# 사용자가 기존 persistent shell에 미리 준비한 환경의 예
cd /workspace/experiment
export DATA_DIR=/data
conda activate experiment

# supervisor가 실험을 실행하고 후손의 종료까지 기다리는 개념
wb-run -- python train.py
```

설계 시 확인할 조건:

1. Supervisor는 프로그램 실행 전에 `PR_SET_CHILD_SUBREAPER` 설정 성공을 확인한다.
2. Supervisor는 실험 하나만 담당한다. 다른 실험·모델 프로세스를 같은 자식 관리 범위에 넣지 않는다.
3. 실험은 현재 shell의 cwd·exported environment와 선택한 실행 파일 경로를 사용한다. Workbench의 Python 환경이 실험 환경을 덮어쓰지 않게 한다.
4. 실험 stdin·stdout·stderr는 기존 PTY 동작을 유지하고 lifecycle event는 별도 경로로 전달한다.
5. 주 프로그램이 끝나도 남은 자식·인수한 후손을 기다린다. 자식 종료 회수 주체를 명확히 한다.
6. 지원 범위의 자식이 모두 종료·회수되고 새 실행을 시작할 작업이 없을 때 로컬 수명 종료를 보고한다.
7. Supervisor 종료 후 같은 shell이 검증된 control 대기로 돌아온 사실을 확인하고 다음 자동 실행을 검토한다.

Subreaper는 부모가 종료한 후손을 가장 가까운 살아 있는 ancestor subreaper로 넘겨받는 Linux 기능이다. Double-fork daemon 관측에 활용할 수 있다. [Linux 매뉴얼](https://man7.org/linux/man-pages/man2/PR_SET_CHILD_SUBREAPER.2const.html)

종료 판정에는 `waitpid`/`waitid`의 대상 범위, signal disposition, 다른 reaper와의 경합, Linux clone 자식 유형을 고려해야 한다. 특히 `WNOHANG`의 “지금 회수할 종료가 없음”과 “살아 있는 자식이 없음”을 혼동하지 않는다. `ECHILD`만 무조건 완료로 매핑하지 않는다. [wait 매뉴얼](https://man7.org/linux/man-pages/man2/waitpid.2.html)

중간 부모가 이미 회수한 자식의 exit status까지 supervisor가 모두 수집한다고 약속하지 않는다. 전체 수명 종료와 확인 가능한 개별 exit status를 구분한다.

## 6. 자동 명령 실행 — A 방식 선택, 세부 구현 검증

환경 준비는 기존 persistent shell에서 유지하고 실험은 독립 실행 단위로 관리한다. 실험 안의 cd·export·source 효과를 이후 실험의 부모 shell에 자동 반영하지 않는다. 다음 실행에도 필요한 환경 변경은 기존 shell의 준비 상태에 명시적으로 적용한다.

아래 명령 전달 형식은 A를 구현하기 위한 후보이며 사용자에게 새로운 CLI 형식이나 수동 wrapper 작성을 강제하는 결정은 아니다. 자동 실험의 supervisor 실행은 Workbench가 구성하는 방향이다.

| 종류 | 제안 처리 | 의미·제약 |
|---|---|---|
| cd·export·conda/venv 전환 | 기존 persistent shell에서 수행 | 준비 과정의 hook·자식 작업과 handoff 안전성도 확인해야 함 |
| 실행 파일과 인자 | 구조화된 argv를 supervisor에 전달 | Shell 문자열을 임의로 재해석하지 않음 |
| 복잡한 실험 절차 | 명시적인 script를 supervisor 아래에서 실행 | Script 내부 cd·변수 변경은 부모 shell에 남지 않음 |
| Pipeline·함수·source | 실험 전체를 관측 범위에 넣는 실행 형식 검증 | 부모 shell 상태에 효과를 남기는 동작은 기본 A 범위가 아님. 비수출 변수·함수를 자동 복제한다고 약속하지 않음 |
| 사용자 수동 명령 | 기존 terminal 입력 유지 | Supervisor 밖 작업의 전체 종료를 자동 확인할 수 있다고 가정하지 않음 |

예를 들어 `prepare_data && python train.py | tee result.log`에서 Python만 감싸면 나머지 프로세스는 관측 범위 밖에 남는다. `$()`는 supervisor가 시작되기 전에 실행될 수 있다. 임의의 shell 문자열을 자동 wrapper로 바꾸는 단순 변환은 채택하지 않는다.

A 선택을 다시 승인받을 필요는 없다. 기존 persistent shell의 PID·준비된 환경·terminal 제어를 유지하는지 검증한다. 실험 script의 실행과 persistent shell 교체를 구분하고, 정식 반영 때 기존 ADR·SPEC의 실행 방식 표현을 새 결정과 대조한다. 추가적인 명령 제한이나 사용자 동작 변경이 필요하면 그 차이를 제시한다.

수동 작업이 남아 있어 안전한 실행 경계를 확인할 수 없거나 기존 실행과의 소속을 구분하지 못하면 자동 재개를 보류한다. 로그 확인과 가능한 수동 조작은 유지한다. 이는 기존 종료 확인·handoff 원칙의 적용이므로 별도의 새 정책 선택을 요구하지 않는다. Supervisor 경계를 도입하는 것만으로 수동 단계의 daemon 추적 문제가 해결되지는 않는다.

## 7. 상태·중단·장애 처리 제안

주 프로그램 종료, 로컬 프로세스 전체 종료, shell 평가 반환, terminal 입력 owner, 실험 결과 판단을 별도로 기록한다. Supervisor 방식에서는 로컬 종료가 shell 반환보다 먼저 올 수 있으므로 기존 cgroup 방식과 같은 event 순서를 강제하지 않는다.

| 관측 | 처리 |
|---|---|
| 주 프로그램·후손 모두 종료 | 로컬 종료 확인. 자동화 활성 상태 등 기존 조건에 따라 worker 결과 분석 가능 |
| 주 프로그램만 종료 | 남은 후손의 종료 대기, 해당 실험의 자동 수정·재실행 보류 |
| 자식이 background·daemon으로 전환 | 계속 관측. 전환 자체를 종료로 처리하지 않음 |
| 자식이 끝나지 않음 | 상태 조사 후 기존 위임 범위의 중단 정책 적용. 무출력·시간 경과만으로 종료 처리하지 않음 |
| Supervisor 장애·관측 근거 손실 | `LIFETIME_UNKNOWN`. 요청 replay와 자동 후속 실행 보류 |
| UI 연결만 손실, 관측 프로세스 유지 | 살아 있는 관측 주체에 재접속해 확인 가능한 상태 복구 |

중단은 자연 종료 관측과 별도로 검증한다. Process group을 벗어난 daemon까지 일괄 종료할 수 있다고 가정하지 않는다. `pidfd`는 이미 확인한 프로세스의 관측·signal에 도움이 되지만 모든 후손을 발견하는 기능은 아니다. [pidfd 매뉴얼](https://man7.org/linux/man-pages/man2/pidfd_open.2.html)

Ctrl-C가 supervisor만 죽여 관측을 끊지 않도록 signal·foreground process group 동작을 설계한다. 인수는 실험 종료를 의미하지 않는다. 확인된 foreground에 입력을 보낼 수 있어야 하며, foreground 종료 후 남은 PTY 입력이 shell 명령으로 실행되지 않아야 한다.

Supervisor 자체가 죽은 경우 PID 목록만 대조해 완전 복구를 선언하지 않는다. UI/backend와 관측 프로세스의 수명 분리, 재접속 식별자·기록·control channel 설계는 후속 검증 사항이다.

## 8. 검증 순서와 수용 기준 제안

| 단계 | 가장 작은 검증 | 통과 기준 |
|---|---|---|
| 1. Container capability | cgroup read-only·systemd 없음·기본 seccomp 환경에서 subreaper 설정과 자식 실행·대기 | 추가 특권 없이 성공. 차단되면 기능·오류 원인을 보고하고 실행 전 판별 |
| 2. 수명 관측 | 단일 프로그램, fork/spawn multiprocessing, 부모보다 오래 사는 자식, double-fork·setsid, 빠른 생성·종료·종료 직전 fork | 살아 있는 관리 대상이 있으면 완료 없음. 모두 종료한 뒤 종료 관측. Zombie 정리 |
| 3. Shell·PTY | Bash와 실제 sh 구현에서 환경 유지·foreground stdin·Ctrl-C·Ctrl-Z/재개·인수·남은 입력·control EOF | 같은 shell 상태 유지, 잘못된 입력 전달·유출 없음, 관측 유지 또는 명시적 unknown |
| 4. 장애·실사용 | Supervisor 강제 종료, UI 재접속, control 손실, 중단 실패, 실제 사용 실험 | 거짓 완료·요청 replay 없음. 확인 가능한 상태 복구와 잔존 프로세스 보고 |

기본 seccomp profile의 공개본은 `prctl`, `wait4`, `waitid` 등을 허용한다. 실제 배포 환경의 profile과 kernel에서 capability를 시험한다. [Docker 기본 profile](https://github.com/moby/profiles/blob/main/seccomp/default.json)

테스트 프로그램의 별도 PID·생존 신호를 대조해 “프로세스가 살아 있는데 완료를 표시함”을 검출한다. 반복 성공뿐 아니라 부모·자식 관계와 대기 규칙이 누락을 막는 이유를 검토한다. Docker·kernel·shell·사용자·seccomp 설정과 skip·미검증 항목을 결과에 남긴다.

실패 시 해당 가정에 의존하는 구현 확대를 멈추고 원인을 조사한다. 결정된 기준 안의 구현 수정·후보 비교로 해결할 수 있는 기술 문제는 기술 검증으로 다룬다. 추가 container 권한, 기존 shell 교체, 필요한 명령·수동 조작의 제한처럼 사용 조건 변경이 필요할 때 근거와 대안을 제시하고 사용자에게 결정받는다.

## 9. 남은 기술 검증과 사용자 재결정 조건

현재 추가로 반드시 답해야 할 사용자 결정 질문은 없다. A 방식·준비된 환경 사용·전체 프로세스 종료 기준·subreaper 우선 및 필요시 외부 도구 허용 방향으로 검증 기준을 잡을 수 있다.

남은 기술 과제:

- Docker 호환성: 실제 준비된 환경에서 subreaper·자식 실행·대기가 추가 특권 없이 가능한지 확인한다.
- 전체 종료 관측: 부모의 선행 종료·daemon 분리·빠른 fork/exit에서도 살아 있는 후손을 놓치지 않는지 확인한다.
- Terminal 제어: PTY 입력·Ctrl-C·suspend·수동 인수·shell 복귀를 안전하게 처리한다.
- 중단·장애 처리: Supervisor 장애·control 손실·중단 실패를 완료로 오판하지 않고 잔존과 unknown을 보고한다.
- 수동 단계 복귀: 기존 종료 확인 원칙에 따라 안전한 경계를 확인할 때만 자동 재개한다.

Docker 사용 형태(rootful/rootless, non-root 사용자, 기존 container에 설치하는 방식, kernel·seccomp)는 검증 환경 정보다. 확보 가능한 설정을 먼저 확인하고 필요한 경우 실제 사용 환경 정보를 요청한다. 이미 준비된 환경을 새로 구축하도록 요구하는 전제로 바꾸지 않는다.

외부 도구는 설치 회피 자체보다 기능·통합 이점으로 판단한다. Subreaper 기반 후보의 부족분을 확인하고, supervisord 등이 그 부족분을 해결하는지 비교한다. 이미 허용한 의존성 검토를 반복 승인 항목으로 만들지 않으며, 추가 권한이나 사용 방식 변경이 수반되면 그 변경만 결정받는다.

장기 실행 서버를 실험 종료 후 남기는 동작은 기본 요구가 아니다. 추후 예외가 필요하거나 검증 결과 사용 조건을 바꾸어야 할 때만 정책을 다시 토의한다.

이 기록의 후속 반영 단계에서는 BRIEF·SPEC·PLAN·ticket·결정 기록과의 변경 영향을 정리한다. 기술 선택 방향의 합의와 구현·시험 통과를 구분한다. 이번에는 이 임시 문서만 갱신하며, 이 초안을 정식 문서 개정·새 구현 실행 또는 G2 통과의 근거로 취급하지 않는다.
