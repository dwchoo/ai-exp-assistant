# p27-cw16-c18-review-01 — CW-16 fix-05 delta review (read-only)

## 판정: **pass** (P1 없음, P2 없음, P3 2건)

범위: `git diff 81bebe6 -- src omp_bridge tests`(9 files), assignment-fix-05 adjudication 4건과 C17 finding 대조. 모델 요청 0, 자격 증명 미열람, 이 파일 외 쓰기 없음.
실행(`PYTHONDONTWRITEBYTECODE=1`, 저장소 직접): `test_cw16_fix04` 12 OK, `test_cli_shutdown_wait_cw16` 10 OK, live `test_live_start_independent -k private_modes`(실제 OMP 18.8.0) OK.

## Prior finding verdict
- P2-1 umask: 해소. `OMP_UMASK`/`os.umask`/`umask=` 참조가 src에서 전부 사라졌다(남은 umask는 `ui_server.py:87` 소켓용 임시 0177과 adapter 주석뿐). OMP child·launcher helper·tool 모두 사용자 umask를 유지하고, `OmpUmask` 테스트가 0002/0664/0775를 실제 자식 프로세스로 단정한다. 0700 보장은 `prepare_omp_home`의 `ensure_private_dir`(omp_home.py:441-442)가 담당하며 단위 테스트와 live 테스트가 둘 다 확인한다.
- private-mode independent 변경: adjudication과 일치하고 보장이 약해지지 않았다. data dir 0700, socket 0600, omp-root·agent 0700을 단정하고, omp-root 내부는 `dirs[:]=[]`로만 제외한다(omp-root 직하 파일은 계속 검사, 주석으로 사유 기재). `seed_provider=False`라 제품이 직접 생성한 mode를 관측한다.
- P2-2 색: 해소. pyte가 내는 이름 전부(FG/BG ANSI·AIXTERM)를 직접 대조했고 모두 올바른 index(brightbrown→11, bfightmagenta→13)이며 미해석 이름은 `default`뿐이다. 단위 테스트가 pyte를 통해 SGR 30-37/90-97/40-47/100-107 32개를 256/16색 모두에서 검사하고, gap COLOR에 같은 32 case가 추가됐다(`as_rgb`의 typo 매핑 포함). 기대값이 palette에서 독립 계산되어 약화 없음.
- P3-1 harness: 해소. `wire_provider`가 omp-root/agent를 0755로 만들어 제품의 0700 복구가 관측 가능해졌다(live_harness.py:297-305, 사유 주석). 다른 live 테스트에서 harness 때문에 mode가 가려지는 일이 없다.
- P3-2 shutdown CLI: 해소. stdin None/닫힘/non-tty는 활성 작업을 출력하고 `--yes` 안내와 함께 exit 1이며 `SHUTDOWN_CONFIRM`을 보내지 않는다(테스트가 sent 목록으로 확인). prompt의 EOF·Ctrl-C는 "cancelled (nothing was stopped)", 대기 중 Ctrl-C는 `lost` 설정 후 기존 "종료 확인 실패" 경로(`shutdown: null`, human/json 모두)로 exit 1이다. 거짓 성공 없음.

## Findings
- P3-a `cli.py:~455-487`: `KeyboardInterrupt` 처리가 `SHUTDOWN_CONFIRM` 요청 이후 구간만 감싼다. `SHUTDOWN_REQUEST`(pending token을 받는 첫 request) 도중 Ctrl-C는 여전히 traceback이 날 수 있다. 영향은 작다(아직 아무것도 중단되지 않은 상태). 필요하면 바깥 except로 넓힌다.
- P3-b `app.py:92`: `bfightmagenta`는 pyte 오타를 이름 그대로 매핑한 것이다. pyte가 고치면 `brightmagenta`가 이미 있어 무해하다. 현 상태 유지해도 된다.

## 참고 (Root)
- worker 결과대로 ui `test_product_pty` race는 이 delta 밖(단독 재실행 통과)이다. 전체 gap 시나리오 재실행은 COLOR만 했다(나머지 3개는 같은 src에서 1차 통과). docs에는 C-D73 system-16 색 한계 문구가 아직 없다.
