# 후속 과제: OMP pane 24-bit 색(트루컬러) 원본 그대로 표시

상태: 미해결(후속). 결정 근거: [C-D73](DECISIONS.md#C-D73) (2026-10-08, 사용자 "256색 근사 수용"). 이 문서는 나중에 원본 색 fidelity를 구현할 때 출발점으로 쓴다. 현재 제품 동작(256색 근사)은 C-D73으로 확정된 정상 동작이며, 이 문서의 내용은 승인된 범위가 아니다. 착수하려면 사용자 결정과 PLAN 변경이 먼저 필요하다.

## 1. 현재 동작과 한계

- 제품 UI는 curses로 화면을 그린다. 각 pane 셀의 색은 curses color pair(전경·배경 index 쌍)로 바뀌어 출력된다.
- OMP가 보내는 24-bit RGB(`ESC[38;2;R;G;Bm`)는 pane VT 화면(pyte 기반 `TerminalScreen`)에 hex 문자열(`"303030"` 등)로 저장된다. 이 값은 그릴 때 6×6×6 cube의 가장 가까운 index(16–231)로 근사된다.
- 256색 index(`ESC[38;5;Nm`)도 pyte가 hex로 바꿔 저장하므로, 원래 index 정보가 사라진다. grey ramp(232–255)와 system 16색(0–15)은 cube로 다시 근사되어 원래 색과 달라질 수 있다.
- 결과: 색 구분과 강조(bold/underline/reverse)는 유지된다. 그러나 원본 OMP 테마의 정확한 RGB와 grey 계조는 보장되지 않는다.
- 실물 GUI terminal(글꼴·테마·RGB)에서의 표시 fidelity는 검증하지 않았다(COMPATIBILITY.md 미검증 목록).

## 2. 관련 코드

| 위치 | 역할 |
|---|---|
| `src/workbench/ui/terminal_g1/app.py` `_color_index` | 이름·10진 index·6자리 hex를 curses index로 바꾼다. RGB는 cube 근사. CW-16 gap D1 수정 전에는 `303030`처럼 숫자로만 된 hex를 10진수로 먼저 해석해 255(흰색)로 그렸다. |
| `src/workbench/ui/terminal_g1/app.py` `_ColorPairs` | color pair 할당. `COLOR_PAIRS` 한도를 넘으면 0(기본색)으로 떨어진다. |
| `src/workbench/ui/terminal_g1/app.py` `_attributes` | 셀 속성을 curses attr로 바꾼다(제품 `ui/product/view.py`가 사용). |
| `src/workbench/ui/terminal_g1/app.py` `_rgb`, `_rgb_cell_style`, `_draw`의 `rgb_output` | **선행 시도**. G1 feasibility UI는 curses가 화면을 그린 직후에 RGB 셀만 커서 이동과 `38;2`/`48;2` SGR로 직접 다시 써서 정확한 RGB를 보존했다. 제품 UI는 이 경로를 쓰지 않는다. |
| `src/workbench/ui/product/view.py` `draw` | 제품 pane 셀 그리기(`_attributes` 사용). |
| `src/workbench/ui/product/app.py` | `_ColorPairs()` 생성, outer 출력 루프(OSC 52 등 raw 출력 경로가 이미 있음). |
| `src/workbench/terminal/vt_g1/screen.py` | pane VT 화면. 색을 pyte 방식(이름/hex)으로 저장한다. |

## 3. 해결 방향 후보

1. **curses 뒤 RGB overlay(권장 출발점):** G1 feasibility의 `_rgb_cell_style` 방식을 제품 view에 옮긴다.
   - 바깥 terminal이 truecolor를 지원할 때만 쓴다(`COLORTERM=truecolor|24bit` 또는 terminfo `Tc`/`RGB`).
   - curses `refresh` 직후, 바뀐 RGB 셀만 커서 이동과 `38;2`/`48;2` SGR로 다시 쓴다.
   - 장점: 변경이 작다.
   - 위험: curses가 모르는 출력이 섞인다. 다음 refresh의 diff 계산과 커서 위치가 어긋날 수 있다. 줄 지움, 스크롤, resize, 선택 강조(드래그 복사), 포커스 테두리와도 겹친다.
2. **원래 index 보존:** VT 화면이 256색 index를 hex로 바꾸지 않고 원래 index를 함께 저장하게 한다(pyte `Char` 확장 또는 별도 속성 표). 0–255 index는 curses pair로 정확히 그릴 수 있다. RGB만 1번 경로로 보낸다. grey ramp와 system 16색 손실이 없어진다.
3. **curses 대체 렌더러:** pane 영역을 직접 diff 렌더링한다(자체 셀 버퍼와 SGR 출력). fidelity가 가장 높다. 대신 UI 전체(입력, resize, 선택, 상태 줄, IME/jamo)의 회귀 범위가 크다.

## 4. 바깥 환경 주의

- tmux: `default-terminal`과 `terminal-overrides`의 `Tc`/`RGB`가 없으면 tmux가 RGB를 256색으로 다시 줄인다. 이때 제품은 RGB 출력이 의미 없다는 것을 알 수 없다. 그래서 `COLORTERM`만으로 판정하지 말고, 바깥 tmux 여부와 설정을 기록한다.
- herdr 0.9.3: truecolor passthrough 여부를 실제로 확인한 적이 없다.
- 일반 terminal: 대부분 지원한다. 단, `TERM=xterm-256color`만으로는 RGB 지원을 보장할 수 없다.

## 5. 완료 판정(원래 G1-COLOR 기준으로 되돌릴 때)

- 같은 rows/columns·version·theme·TERM/COLORTERM·font에서 OMP의 RGB 텍스트 색과 강조를 원본과 대조한다.
- pyte truecolor 파싱이나 정적 RGB 축소 확인만으로는 통과시키지 않는다.
- 커서, SGR, redraw 회귀를 함께 확인한다. 대상은 resize, scroll, 드래그 선택, 포커스 전환, 상태 줄 갱신, IME/jamo 입력이다.
- 바깥 환경별(일반 terminal/tmux `Tc` 있음·없음/herdr) 결과를 COMPATIBILITY.md에 기록한다.
- 실물 GUI terminal 1종 이상에서 화면 캡처나 색 샘플로 대조한다(pyte만으로는 부족).

## 6. 참고 기록

- CW-16 gap 검증: `.workflow/core-workbench/runs/implement-p2.6-20260927/result-p27-cw16-gap-01.md`(D1 색 해석 오류, D2 256색 근사 한계).
- G1 gate 문서: `docs/features/core-workbench/gates/G1.md`(curses 256색 근사가 fidelity 완료 근거가 아니라는 기존 기록).
- PLAN `G1-COLOR`의 이전 기준 문구는 C-D73 개정 문구 끝에 보존되어 있다.
