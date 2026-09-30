# UR-UX 사용자 검토 안내 (CW-06 제품 UI)

- 대상 후보: CW-06 통합 후보 `671b05d3…` ([통합 기록](../integration-p27-v-cw06.json))
- 근거: PLAN `user_review_checkpoints` UR-UX, [C-D57](../../../../../docs/features/core-workbench/DECISIONS.md#C-D57)
- UR-UX가 끝나기 전에는 CW-18을 시작하지 않는다.

## 1. 실행 방법

일반 terminal에서 가로 160열·세로 40행 이상을 권장한다. tmux·Herdr 안에서도 실행할 수 있다.

```bash
mkdir -p ~/wb-urux-sandbox && cd ~/wb-urux-sandbox        # OMP 작업 디렉터리(검토용 빈 디렉터리)
PYTHONPATH=/home/dwchoo/ai-exp-assistant/src \
  /tmp/cw02-g1-venv/bin/python -m workbench start --data-dir ~/.local/state/omp-workbench-urux
```

- backend가 따로 뜨고 제품 UI가 붙는다. 사용자의 기존 OMP 설정과 인증을 그대로 쓴다. OMP에 말을 걸면 실제 모델 호출이 일어난다.
- UI만 닫기(detach): `Ctrl-]` 다음 `d`. backend, 두 OMP, host shell은 계속 실행된다.
- 다시 붙기: `PYTHONPATH=… /tmp/cw02-g1-venv/bin/python -m workbench attach --data-dir ~/.local/state/omp-workbench-urux`
- 상태 보기: `… -m workbench status --data-dir ~/.local/state/omp-workbench-urux`
- 전체 종료: `… -m workbench shutdown --data-dir ~/.local/state/omp-workbench-urux` (확인 질문에 y). 지금은 CLI로만 종료할 수 있다.
- 최소 client로 비교하기: `attach --plain`

## 2. 키 (임시 배치, 이번 검토 대상)

| 키 | 동작 |
|---|---|
| `Ctrl-]` 뒤 `1` / `2` / `3` | manager OMP / worker OMP / host shell로 focus |
| `Ctrl-]` 뒤 `Tab` | 다음 pane |
| `Ctrl-]` 뒤 `t` / `c` | host shell 사용자 인수 요청 / 확인 |
| `Ctrl-]` 뒤 `h` | host shell을 manager에게 되돌리기(handoff). 먼저 shell에서 `wb-handoff` 실행 |
| `Ctrl-]` 뒤 `r` | focus pane 다시 그리기 |
| `Ctrl-]` 뒤 `d` | detach |
| `Ctrl-]` 뒤 `?` | 도움말 |
| `Ctrl-]` 두 번 | `Ctrl-]` 문자 자체를 pane에 전송 |

그 밖의 키는 focus된 pane으로 그대로 전달된다. OMP의 `/` 명령, `Esc`, `Ctrl-C`, `Alt-Enter`도 여기에 포함된다.

## 3. 확인 목록 (UR-UX 범위)

항목마다 **좋음 / 바꾸고 싶음 / 모르겠음** 중 하나와 한 줄 의견을 남겨 주면 된다.

1. **세 영역 배치와 크기**: 3열 배치가 읽기 좋은가. OMP 화면이 너무 좁지 않은가(스냅샷 01, 15, 16).
2. **focus와 host 입력 owner 구분**: 상단 두 줄만 보고 "지금 어디에 입력되는가"와 "host shell 입력권이 누구에게 있는가"를 구분할 수 있는가(03, 05–08).
3. **pane 전환과 단축키**: `Ctrl-]` prefix가 불편하거나 OMP·shell 키와 충돌하는가(02).
4. **붙여넣기**: 한글·여러 줄 붙여넣기와, 2 MiB를 넘는 붙여넣기의 거절 표시가 이해되는가(09, 10, 10b).
5. **detach/재attach**: 닫았다가 다시 붙었을 때 화면 복원이 자연스러운가(13, 14).
6. **상태 문구**: 자동화 상태, shell mode, 마지막 확인 시각, 거절·보류 사유 문구가 이해되는가(05b, 07a, 08, 11).

## 4. 결정이 필요한 질문

아래는 배치·키·문구가 아니라 제품 동작에 관한 질문이다. 답에 따라 새 결정 기록과 계획 변경이 필요할 수 있다.

- **Q1. background job 때문에 handoff가 막히는 경우**
  - 상황: host shell에서 사용자가 background/suspended job을 남긴 채 `wb-handoff`를 하면 handoff가 보류된다.
  - 문제: 이때 shell이 수동 입력과 인수 확인을 모두 거부해서, UI 안에서는 빠져나올 방법이 없다.
  - 배경: 보류 자체는 명세된 fail-safe이고, G2 문서에 미확인 항목으로 남아 있다.
  - 선택지:
    - (a) 보류 중에도 사용자 인수와 수동 입력을 허용한다.
    - (b) 보류 사유와 해결 방법(job 정리 후 재시도)만 안내한다.
    - (c) 지금처럼 두고 CW-18에서 다룬다.
- **Q2. UI 안의 전체 종료·boot 확인 조작**: 지금은 CLI(`shutdown`)로만 할 수 있다. UI에도 이 조작이 있어야 하는가? 재부팅 뒤 `confirm_boot` 화면은 CW-19에서 만든다.

## 5. 알려진 제한

검토 중 아래 현상을 만나도 결함으로 보고하지 않아도 된다. 의견은 환영한다.

- **여러 줄 입력 뒤 handoff 보류**: host shell에 여러 줄을 직접 입력하거나 붙여넣은 뒤, 또는 대량 출력 후 Ctrl-C 뒤에는 `wb-handoff`가 보류될 수 있다. 안전 경계를 확인할 수 없을 때 보류하는 설계이고, 화면에 `held:` 사유가 표시된다(07a).
- **대량 출력 중 화면**: 출력이 매우 많으면 pane이 "출력 따라잡음"을 표시하고 오래된 화면 출력을 건너뛴다. backend 기록은 그대로 남는다. 출력이 이어지는 동안에는 화면이 반복해서 새로 그려질 수 있다(11, 12).
- **자동화 연결 전**: Task 흐름, 60초 점검, 일시정지는 CW-18 전이라 연결되지 않았다. 상태는 `not_configured`로 보이고, 승인·진행·일시정지 조작도 아직 없다.
- **재attach 뒤 인수 요청**: 재attach하면 대기 중인 인수 요청은 따로 표시되지 않는다. owner와 mode는 표시된다.
- **좁은 화면**: 100열에서는 pane당 약 32열이라 host pane 제목이 잘린다(15).
- **렌더링 한계**:
  - 색은 256색 근사다.
  - scrollback(위로 스크롤) UI는 없다.
  - pane 안 프로그램의 DEC 선그리기 문자(`ESC(0`)는 문자로 보일 수 있다.
- **승인 prompt**: 현재 OMP 설정에서는 shell tool을 실행할 때 승인 prompt가 뜨지 않아 승인 조작은 검증하지 못했다.

## 6. 화면 스냅샷

[snapshots/](snapshots/)에 실제 OMP 18.2.10으로 모델 호출 없이 찍은 text 화면 19장이 있다. 크기는 160x45이고, 15·16만 다르다.

| 파일 | 상태 |
|---|---|
| 01-start-attach | 시작 직후: 세 pane, 상단 상태 두 줄, footer |
| 02-help-overlay | `Ctrl-] ?` 도움말 |
| 03-focus-worker-omp | worker로 focus, owner는 그대로 |
| 04-focus-host-shell | host shell 명령 실행 |
| 05b-takeover-request-already-user | 이미 user owner일 때 인수 요청 알림 |
| 05-takeover-request / 06-takeover-confirm | manager owner에서 인수 요청과 확인 |
| 07a-handoff-refused-before-wb-handoff | `wb-handoff` 없이 handoff: 보류 사유 표시 |
| 07-handoff-owner-manager | handoff 후 owner=manager |
| 08-input-refused-owner-manager | manager owner일 때 입력 거절 알림 |
| 09-paste-over-2mib-refused | 2 MiB 초과 붙여넣기 거절 |
| 10 / 10b-korean-multiline-paste | 한글 3줄 붙여넣기, Enter 전과 후 |
| 11-flood-catchup / 12-flood-after-ctrl-c | 대량 출력 중 "따라잡음" 표시와 Ctrl-C 후 |
| 13-after-detach / 14-after-reattach | detach 메시지와 재attach 후 복원 화면 |
| 15-narrow-100x30 / 16-wide-200x50 | 좁은 화면과 넓은 화면 |

## 7. 피드백 처리

- 배치·키·문구 수준의 피드백은 CW-06 수정으로 반영하고, 독립 test와 review를 다시 거친다.
- 4절의 결정이나 제품 동작·수락 기준 변경은 새 결정 기록과 계획 변경을 거친다.
- 검토 결과는 사용자 인용과 함께 이 run의 UR-UX 기록에 남긴다.
