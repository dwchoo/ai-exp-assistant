# Core Workbench 최종 검증 기록

상태: **미완료 / local gate와 CW-16 compatibility runtime은 통과했으나 최종 integration gate는 pending**.

이 기록은 `$implement docs/features/core-workbench`의 CW-16 결과다. 상태 flag만으로 완료를 추론하지 않고, frozen candidate의 check와 실제 확인하지 못한 항목을 함께 기록한다.

## 이번 실행의 결론

- CW-02~15의 formal local gate는 모두 통과했다. 마지막 CW-15 frozen base candidate는 `865fc473ddae3f49ed15ebc9e99e6a8bbe4f41a66aa9cf187557dca0fd840a3c`이며 lifecycle check 38개와 독립 non-live 회귀 398개가 통과했다.
- CW-16에서 actual Bash/dash canonical shell matrix와 plain PTY/external tmux/Herdr outer matrix를 실행해 모두 통과했다.
- 최종 제품 형태의 manager OMP → Task/revision → worker OMP → persistent host run → evidence → manager 완료/복구 흐름을 실행할 단일 entrypoint와 실제 UI/backend 조합 근거는 없다.
- 실제 reboot 및 backend를 유지한 UI detach/reconnect도 실행하지 않았다. 따라서 I-FLOW, I-SHELL, I-POLICY, I-FAULT, I-COMPAT을 모두 충족했다고 판정할 수 없고, C-AC-01~34의 production acceptance도 완료가 아니다.

## 재실행한 check

| Check | 결과 | 범위 |
|---|---|---|
| `python -m unittest tests.gates.g2_shell.test_canonical_matrix_integration -v` | 3/3 통과 | actual Bash/dash, lifecycle identity, takeover ACK 손실·지연, missing exec fail-closed |
| `live_outer_compat_probe.py` | 통과 | plain PTY, 실제 OMP 2개+host, 입력/paste/resize/복원/cleanup |
| `live_outer_tmux_probe.py` | 통과 | external tmux 3.4 격리 socket, 입력/paste/resize/복원/cleanup |
| `live_outer_compat_probe.py --herdr` | 통과 | Herdr 0.9.1 격리 session, non-nested attach, 기존 상태 불변, cleanup |
| `python -m unittest discover -s tests/lifecycle -p 'test_*.py' -q` | 38/38 통과 | CW-15 frozen lifecycle candidate |

CW-16 재현 명령은 [test_cw16_runtime_matrix.py](../../../tests/integration/test_cw16_runtime_matrix.py)에 묶었다. 상세 환경과 지원 범위는 [COMPATIBILITY.md](COMPATIBILITY.md)를 따른다.

## 최종 integration item

| Item | 현재 근거 | 판정 | 남은 필수 근거 |
|---|---|---|---|
| I-FLOW | Task/mailbox/workflow/review/recovery의 local gate와 production port 단위 근거 | pending | 실제 manager·worker OMP에서 승인 목표부터 host run, worker 근거, manager 완료·실패 복구까지 한 흐름 |
| I-SHELL | actual Bash/dash canonical matrix, persistent shell local gate | pending | 최종 UI/backend가 같은 persistent shell을 사용해 control 대기·수동 prompt·unknown/no replay를 표시하는 end-to-end 근거 |
| I-POLICY | 승인·pause·review·recovery policy의 formal local gate | pending | 실제 OMP/UI에서 60초 점검, turn 중단 요청/확인/unknown, 명시적 resume 대조를 결합한 runtime |
| I-FAULT | CW-15 lifecycle runtime, exact RunIdentity, no replay, shutdown confirmation, storage fault gate | pending | 실제 backend/UI detach/reconnect와 real reboot 후 확인 전 새 실행 금지 |
| I-COMPAT | plain PTY/tmux/Herdr outer runtime과 Bash/dash matrix | pending | backend 유지 detach/reconnect 및 최종 product UI/backend와의 결합 |

## C-AC-01~34 추적

`local`은 해당 행동 seam의 formal gate가 통과했음을 뜻하며 production acceptance 완료를 뜻하지 않는다. `final`은 PLAN의 integration item 상태다.

| Acceptance | local supplier | final |
|---|---|---|
| C-AC-01 | final-only (CW-06 manager pane) | I-FLOW pending |
| C-AC-02 | CW-08, G3 | I-FLOW pending |
| C-AC-03 | CW-10, G2 | I-FLOW/I-SHELL pending |
| C-AC-04 | CW-13 | I-FLOW pending |
| C-AC-05 | CW-10 | I-FLOW pending |
| C-AC-06 | G1 | I-COMPAT pending |
| C-AC-07 | CW-13 | I-POLICY pending |
| C-AC-08 | CW-07, G2, G4 | I-SHELL pending |
| C-AC-09 | CW-10 | I-FLOW pending |
| C-AC-10 | CW-13, G2 | I-POLICY pending |
| C-AC-11 | CW-10 | I-FLOW pending |
| C-AC-12 | CW-11, G4 | I-POLICY pending |
| C-AC-13 | CW-13 | I-POLICY pending |
| C-AC-14 | G1, G3 | I-COMPAT pending |
| C-AC-15 | CW-15, G4 | I-FAULT pending |
| C-AC-16 | CW-15, G3 | I-FAULT pending |
| C-AC-17 | CW-15 | I-FAULT pending |
| C-AC-18 | CW-15, G4 | I-FAULT pending |
| C-AC-19 | G1 + CW-16 outer matrix | I-COMPAT pending |
| C-AC-20 | G1/G2 + CW-16 shell/outer matrix | I-COMPAT/I-SHELL pending |
| C-AC-21 | CW-11 | I-POLICY pending |
| C-AC-22 | CW-15 | I-FAULT pending |
| C-AC-23 | CW-15 | I-FAULT pending |
| C-AC-24 | CW-14 | I-FAULT pending |
| C-AC-25 | CW-12, G3/G4 | I-POLICY pending |
| C-AC-26 | CW-07, G2/G4 | I-SHELL pending |
| C-AC-27 | CW-11, G3 | I-POLICY pending |
| C-AC-28 | CW-14, G4 | I-FAULT pending |
| C-AC-29 | CW-09, G4 | I-POLICY pending |
| C-AC-30 | CW-12, G3/G4 | I-POLICY pending |
| C-AC-31 | CW-13 | I-POLICY pending |
| C-AC-32 | CW-07, G2 | I-SHELL pending |
| C-AC-33 | CW-12, G3/G4 | I-POLICY pending |
| C-AC-34 | CW-13, G2 | I-POLICY pending |

Formal local gate 결과는 `.workflow/core-workbench/runs/implement-p2.6-20260927/gate-cw*-result.json`에 있고, 실패했던 중간 G2 slice 결과는 최종 `gate-cw03-g2-integrated-final-result.json`으로 대체한다. 중간 실패 파일을 최종 통과 근거로 사용하지 않는다.

## 완료를 막는 확인 항목

1. 최종 production UI/backend entrypoint에서 실제 manager·worker OMP와 persistent shell을 함께 연결한 I-FLOW/I-SHELL 실행.
2. 같은 entrypoint에서 pause/interrupt/resume와 60초 review를 결합한 I-POLICY 실행.
3. backend를 살린 UI detach/reconnect와 real reboot 복구를 포함한 I-FAULT 실행.
4. 위 detach/reconnect를 plain terminal, external tmux, Herdr에서 재실행해 C-AC-19/I-COMPAT을 닫는 것.

이 네 항목이 확인되기 전에는 CW-16 또는 전체 Core Workbench를 `done_verified`로 닫지 않는다.
