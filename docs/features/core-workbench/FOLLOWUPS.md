# Core Workbench 후속 과제

상태 기준: 2026-10-09, 최종 후보 C18(`c47456a`) 위에 최종 검증 문서 커밋 `9a70f36`. CW-16은 `done_verified`로 판정했다([VERIFICATION.md](VERIFICATION.md) §1). 아래 항목은 완료를 막지 않지만, 다음 작업에서 다룰 후보다. 착수 전에 사용자 결정이 필요한 항목은 따로 표시했다.

## 1. 기능 후속(사용자 결정 필요)

| 항목 | 내용 | 근거·출발점 |
|---|---|---|
| 24-bit 트루컬러 표시 | 지금은 C-D73에 따라 256색으로 근사한다. 원본 RGB를 그대로 보이게 하려면 렌더링 방식을 바꿔야 한다. | [FOLLOWUP-TRUECOLOR.md](FOLLOWUP-TRUECOLOR.md), [C-D73](DECISIONS.md#C-D73) |
| Phase 2: Task Inbox & Web Board | AGENTS.md와 BRIEF가 1차 core 이후로 미룬 기능이다. core가 완료됐으므로 계획 단계부터 시작할 수 있다. | `AGENTS.md`, [BRIEF.md](BRIEF.md) |
| 실험 raw log 프로젝트 한도 순환 | 512 MiB 프로젝트 한도가 차면 그 뒤 run의 로그를 저장하지 않는다. 오래된 로그를 지우는 순환 규칙이 없다. | `.workflow/core-workbench/runs/implement-p2.6-20260927/integration-p27-cw19.json` carried |

## 2. 관찰 중인 간헐 실패(원인 미확정)

| 항목 | 관측 | 기록 |
|---|---|---|
| manager OMP slash menu가 Esc로 닫히지 않음 | C18 compat 첫 실행에서 6셀 중 3셀(plain/bash, plain/dash, tmux/dash)이 실패했다. 같은 argv의 r2는 6/6 통과했다. | `run/result-p27-cw16-c18-formal-01.md`, `run/check-p27-cw16-compat-c18.log` |
| `test_product_pty` RestartExitedOmp 순서 race | 전체 실행에서 가끔 실패하고, 모듈만 다시 돌리면 통과한다. | `run/result-p27-cw16-fix-05.md`, `run/result-p27-cw16-b4b-01.md` |
| backend `connection_cap` BlockingIOError | 단독 실행 9회 중 2회 실패했다. listen backlog와 테스트의 비차단 connect 20개가 겹치는 테스트 쪽 타이밍으로 추정한다. | `run/result-p27-cw16-rerun-01.md` |
| `test_concurrent_kill_and_restart` | 전체 실행에서 1회 실패했고 단독 3/3 통과했다. | `run/result-p27-cw16-rerun-01.md` |

(`run/` = `.workflow/core-workbench/runs/implement-p2.6-20260927/`)

## 3. 실제 모델로 다시 볼 것(확신용)

- 210초 이상 걸리는 worker 명령: terminal_check, terminal_done, 감시 억제를 실제 모델로 확인한다. C16의 RM2는 요청 예산 때문에 실행하지 못했다. 이전 코드에서는 150초 명령으로 확인했다(`run/result-p27-cd70-smoke-04.md`).
- 자동화 run이 진행 중일 때 일시정지하면 manager turn 중단 요청·확인이 표시되는지 확인한다. C18에서는 scripted provider로만 관측했다.
- `shutdown --yes`를 활성 turn과 함께 실행했을 때의 실제 소요 시간. fix-03 수정은 fake OMP로 확인했다.

## 4. 작은 개선 후보

- `to-worker` skill이 크기 한도(11000자)에 거의 찼다(약 10994자). 문구를 더하려면 먼저 줄여야 한다.
- `held:retry_limit`에서 실제 모델이 원인·근거·시도·남은 문제를 빠짐없이 보고하는지 확인하지 않았다(VERIFICATION §7 review P3-1).
- SH5: 사용자 PROMPT_COMMAND/PS1 hook이 원인일 때 사유를 단정하지 못한다(review P3-5).
- worker가 가끔 다른 언어로 답한다. 사용자 판단으로 지금은 무시한다.
- manager가 재시작 복구 전후로 두 번 답하는 경우가 있었다(모델 행동).

## 5. 환경 메모

- 로컬 OMP는 18.8.0이다. OMP 업데이트 뒤에는 격리 점검(시작 시 isolation)과 `tests/backend` live 테스트(sandbox, 버전 기록)를 다시 돌린다.
- 격리 재부팅 VM: `~/.local/share/wb-vm/wb-reboot`(start.sh/stop.sh/ssh.sh). guest OMP는 18.2.10이고, VM 시험은 fake OMP로 모델 요청 없이 돌린다.
- 이 기록을 남긴 Claude 세션은 사용자 tmux와 herdr 안에서 돌았다. 검증용 tmux와 herdr는 항상 별도 socket과 설정 디렉터리로 격리했다.
