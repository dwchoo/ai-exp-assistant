# Core Workbench 호환성 검증

이 문서는 CW-16에서 실제 실행한 조합과 아직 확인하지 못한 경계를 구분한다. 외부 설정은 변경하지 않았고, tmux socket과 Herdr session은 실행마다 격리해 정리했다.

## 검증 환경

| 항목 | 검증 값 |
|---|---|
| OS | Linux |
| Python | 3.12.3 |
| OMP | 18.2.10 |
| Bash | 5.2.21 |
| sh | `/usr/bin/sh` (dash) |
| tmux | 3.4 |
| Herdr | 0.9.1 |
| terminal protocol | `TERM=xterm-256color`, `COLORTERM=truecolor`, PTY |

## Outer terminal matrix

| 조합 | 실제 관측 | 상태 |
|---|---|---|
| plain outer PTY | 실제 OMP 두 개와 host shell의 세 pane, 대상별 입력과 bracketed paste, 240x45 → 210x39 → 240x45 resize, PID 유지, alternate-screen 복원, child 정리 | 통과 |
| external tmux 3.4 | 격리 socket에서 세 pane 입력/paste, tmux window 240x44 → 210x38 → 240x44, child TTY resize, EOF drain 뒤 화면 복원, server·app·child·socket 정리 | 통과 |
| Herdr 0.9.1 | 기존 `HERDR_*` context를 child에서 제거하고 격리 config/session으로 non-nested attach, 세 pane 입력/paste/resize/복원, 기존 Herdr 상태 불변, test session 삭제 | 통과 |

위 세 조합은 [test_cw16_runtime_matrix.py](../../../tests/integration/test_cw16_runtime_matrix.py)에서 재실행한다. 실제 두 OMP는 `--no-session --no-tools --no-pty --no-extensions --no-skills --no-rules --no-title`로 실행해 외부 모델 작업이나 사용자 session 변경 없이 TUI 경계를 확인한다.

다음은 이 matrix로 확인되지 않았다.

- backend를 유지한 채 Workbench UI client를 detach한 뒤 같은 화면에 reconnect하는 end-to-end 동작
- 특정 실물 terminal emulator의 font·RGB rendering fidelity
- 실제로 queue 공간이 부족한 child에 대한 사용자 오류 표시를 outer 세 조합 각각에서 재확인하는 것

따라서 outer 조작 matrix는 통과했지만 C-AC-19 전체는 아직 통과로 판정하지 않는다.

## Shell matrix

| 조합 | 실제 관측 | 상태 |
|---|---|---|
| Bash 5.2.21 | 부모 shell·별도 supervisor·experiment child identity, foreground, exec/start/return/input/control 경계, takeover ACK 손실·지연, unknown/no replay | 통과 |
| `/usr/bin/sh` (dash) | Bash와 같은 canonical matrix 및 실패-폐쇄 negative control | 통과 |
| login shell이 zsh인 선택 경계 | `SHELL=/bin/zsh`여도 Bash를 우선 선택하는 fixture | 통과(선택 fixture) |
| Bash 미존재 | 실제 sh 경로를 선택하는 fixture와 실제 dash runtime의 결합 | 통과 |
| Bash와 sh 모두 미존재 | `Workbench needs Bash or sh on PATH` 안내 fixture | 통과 |

현재 host의 실제 login shell은 Bash이므로 실제 zsh login session 자체는 실행하지 않았다. Workbench가 user login shell을 바꾸지 않는다는 선택 경계만 검증했다. C-D55에 따라 conda 전용 activation/deactivation 검증은 범위 밖이다.

## 지원 판정

- 현재 검증된 outer 조합: Linux + Bash 5.2.21 host pane + plain PTY/tmux 3.4/Herdr 0.9.1.
- dash는 별도 G2 canonical shell matrix에서 검증했다. dash host pane과 plain PTY/tmux/Herdr의 2x3 cross-product는 실행하지 않았다.
- 내부에서 tmux를 실행하거나 의존하지 않는다. tmux는 사용자 outer 환경의 한 조합으로만 사용했다.
- shell runtime 자체의 C-AC-20 핵심 동작은 검증됐지만, 최종 UI/backend와 결합된 I-SHELL이 아직 없으므로 제품 전체 C-AC-20은 pending이다.
- macOS, Windows, inline image, 다른 OMP/tmux/Herdr 버전은 검증 범위 밖이다.
