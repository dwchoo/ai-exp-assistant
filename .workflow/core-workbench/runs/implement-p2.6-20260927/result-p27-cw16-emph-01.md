# result-p27-cw16-emph-01 (G1-COLOR 강조 속성, frozen C18 a2e3daf2 / HEAD c47456a)

- 명령: `cw16-emph-c18/run.sh final` = `env -i`(TMUX*/HERDR_* 없음, fake HOME, proxy 127.0.0.1:9) + `WB_LIVE_CW16=1` + `/tmp/cw02-g1-venv/bin/python cw16-emph-c18/probe_emph.py -v`. exit 0, 1 test OK, 17.9 s. 보고서 `cw16-emph-c18/emph-report-final.json`, rig 보고서 `reports/c18-emph-final/`.
- 실제 모델 요청 0(provider requests {}). cleanup `residue_before_fallback {}`, root_removed true, tmux 서버(own -S socket)는 exact pid+start로 종료, /tmp 잔존 없음.
- 관측: OMP 18.8.0 / 제품 UI `python -m workbench attach` / host pane에서 `printf` SGR. (1) PYTE = tests/ui/support.rep_screen_classes 바깥 emulator, (2) TMUX = 별도 tmux 서버 안에서 attach, `capture-pane -e -p` 파싱.

| 케이스(SGR) | PYTE | TMUX |
|---|---|---|
| 0 (대조: 속성 없음) | pass | pass |
| 1 bold / 4 underline / 7 reverse 단독 | pass ×3 | pass ×3 |
| 1;31, 4;38;5;208, 7;44 | pass ×3 | pass ×3 |
| 1;4;7;32 (3 속성 결합) | pass | pass |
| 1;38;5;196;48;5;21 | pass | pass |
| 4;91, 7;38;5;240;48;5;250, 1;4;34;47 | pass ×3 | pass ×3 |

reverse는 flag로 유지됐다(fg/bg 맞바꿈 대체 없음). marker 전체 문자 셀이 동일 속성(uniform).

## probe 자체 오류 2건(제품 결함 아님, 수정 후 final 실행)
1. 색 비교를 pyte 팔레트 index로 해서 `38;5;196`(ff0000)이 index 9와 구분되지 않았다. RGB 비교로 바꿈.
2. tmux `capture-pane -e`는 이전 줄 마지막 셀과의 차이만 내보낸다(줄 시작에서 SGR 리셋 안 함). 줄별 초기화 파서가 1;31의 bold를 놓쳤다. 줄 간 상태 이월로 수정. 근거: tmux 단독 control(`1;31` → `\e[1m\e[31m`)과 제품 raw(`\e[0;1m \e[31m`, TERM=tmux-256color로 확인).
실패 run(r1~r4)의 report/log는 `cw16-emph-c18/`에 남겼다.

## snapshot (exclude run dir + graphify-out)
- before `db45fea3b618…` = after `db45fea3b618…` (동일). 코드/테스트/src 변경 0.
- C18 snapshot(`a2e3daf2…`)과의 차이는 `docs/features/core-workbench/COMPATIBILITY.md`, `VERIFICATION.md` 두 파일뿐이다. 둘 다 시작 시점부터 working tree에서 M이었고(다른 docs 작업), 내가 쓴 것이 아니다. `git status`로 확인한 tracked 변경은 ledger.json과 이 두 docs뿐이다.
