# p27-cw16-fix-05 — C17 review corrections (worker)

## Checks (serial, `env -i` fake HOME, proxies blocked, SigIgn SIGINT=0; cmd `PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest discover -s tests/<suite> -t tests/<suite> -p 'test_*.py'`; gates with `PYTHONPATH=src:.`)
| suite | exit | counts | time |
|---|---|---|---|
| backend (incl. live) | 0 | 1127 OK (skip 3, xfail 1) | 968 s |
| bridge py | 0 | 52 OK (skip 1) | 53 s |
| node `--test tests/bridge/*.test.ts` | 0 | 48/48 | - |
| contracts / integration / lifecycle / observation | 0 | 52 / 24 (skip 2) / 38 / 95 | 0-10 s |
| recovery_boot | 0 | 183 | 73 s |
| storage / tasks | 0 | 33 / 22 | <3 s |
| terminal | 0 | 221 | 607 s |
| ui | **1** | 843, 1 FAIL `test_product_pty.RestartExitedOmpPtyTests…new_session…` (`old["role"]` manager!=worker: record-file spawn-order race, outside this change) | 208 s |
| ui `test_product_pty.py` rerun | 0 | 41 OK (x1) + the single test x3 OK | 21 s |
| ui/status_workbench, workflow | 0 | 2 / 62 | 139 s |
| gates g1_vt / g2_shell / g3_omp / g4_evidence / g4_lifetime | 0 | 85 / 142 (skip 1) / 117 / 10 / 8 | - |
| policy pause_automation / recovery_manager | 0 | 129 / 26 | - |
| `live_cw16_gap.py` (WB_LIVE_CW16=1, 0 model requests) 1st run | 1 | SCROLL/COMPOSER/SESSION pass; new COLOR step failed: test helper `as_rgb` did not know pyte's `bfightmagenta` (SGR 105) | 139 s |
| `live_cw16_gap.py` COLOR after helper fix (WB_CW16_SCENARIOS=COLOR) | 0 | COLOR OK incl. all 32 new SGR cases | 34 s |
| `test_cw16_fix04.py` | 0 | 12 OK | 0.4 s |
| `test_cli_shutdown_wait_cw16.py` | 0 | 10 OK | 3 s |

## Changes
- P2-1: removed `OMP_UMASK` and `os.umask` in `src/workbench/backend/panes.py` (~L49, ~L440) and `launcher.py` (import, `umask=` at 2 Popen calls). `tests/backend/test_cw16_fix04.py` `OmpUmask`: pane child / helpers / project files keep the user umask (0002, files 0664, dirs 0775), `prepare_omp_home` makes omp-root and agent 0700. `test_live_start_independent.py` private-mode block: data dir 0700, sockets 0600, omp-root and omp-root/agent asserted 0700, Workbench-written entries (backend.json, automation.json, logs...) not group/other accessible; entries under omp-root skipped (comment: protected by the 0700 ancestor); test uses `seed_provider=False` so the product creates omp-root/agent.
- P2-2: `src/workbench/ui/terminal_g1/app.py` `_color_index`: `brightbrown`: 11 and `bfightmagenta`: 13 (pyte spells BG SGR 105 that way, found while checking the other bright names). Unit test (`test_cw16_fix04.py`) feeds every SGR 30-37/90-97/40-47/100-107 through pyte. Gap: new step `COLOR_sgr_30_37_90_97_40_47_100_107` in `live_cw16_gap.py` + `as_rgb` knows `bfightmagenta`.
- P3-1: `tests/backend/live_harness.py` `wire_provider`: omp-root/agent are no longer pre-created 0700; they get 0755 (the file must exist before OMP starts) so the product's 0700 repair is observable; documented in a comment.
- P3-2: `src/workbench/backend/cli.py` `cmd_shutdown`: no `input()` unless stdin is a tty (None/closed/non-tty -> active work + "refusing to shut down without confirmation: confirm with --yes", exit 1; EOF/Ctrl-C at the prompt -> "cancelled", exit 1); Ctrl-C while waiting for the result -> "종료 확인 실패…" message (+ `shutdown: null` / json), exit 1. Tests in `tests/recovery_boot/test_cli_shutdown_wait_cw16.py` (3 new).

## Gaps / residue
- The ui `test_product_pty` race is not caused by this change (module passes alone); no fix attempted (outside allowed scope).
- Full gap run was not repeated after the `as_rgb` helper fix; only COLOR was (the other three passed in the first run on the same src).
- `test_live_start.py` (non-independent copy) was not touched. Temp: /tmp/wbfix05 removed. No commit, no graphify update, docs untouched (COMPATIBILITY note on system-16 colours remains for Root).
