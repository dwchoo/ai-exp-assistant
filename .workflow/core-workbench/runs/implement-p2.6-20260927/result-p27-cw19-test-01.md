# p27-cw19-test-01 — CW-19 independent verification + L-CW19-RESTART (test_designer)

Base `a12e668` working tree (candidate f7091db8…, implementer p27-cw19-01). Source of truth: tickets/CW-19.md,
DECISIONS C-D71, root-accepted implementer decisions 1–8. No commit, no graphify update, no src/omp_bridge/docs
edit, no model/provider request, no credential read, temp only under /tmp, only own processes signalled.

## Checks (real exit codes)

Command: `PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest discover -s tests/<suite> -t tests/<suite> -p 'test_*.py'`;
`node --test tests/bridge/*.test.ts`. Run once, sequentially, after the runtime probe.

| Suite | Exit | Result | Attribution |
|---|---|---|---|
| recovery_boot | **1** | 133 run, 2 failures | both are new independent tests exposing product defects P3-1, P3-2 (below) |
| backend | 0 | 1095 OK (skip 30, xfail 1) | |
| bridge (python) | 0 | 52 OK (skip 1) | incl. updated `test_cw18_schema_independent_p27w.py` |
| contracts / lifecycle / observation / storage / tasks | 0 | 52 / 38 / 95 / 33 / 22 OK | |
| terminal | 0 | 221 OK (skip 2) | |
| ui | 0 | 836 OK | incl. new `test_product_model_independent_p27cw19.py` |
| workflow | 0 | 58 OK | |
| gates/g2_shell, g1_vt, g4_evidence, g4_lifetime | 0 | 142 / 85 / 10 / 8 OK | |
| policy/pause_automation, policy/recovery_manager | 0 / 0 | 129 / 26 OK | |
| integration | **1** | 6 run, 3 failures | `test_cw16_runtime_matrix` outer plain/tmux/herdr: probes pin `omp/18.2.10`, installed OMP is `omp/18.8.0` (`inconclusive_version`) — environment, not CW-19 |
| gates/g3_omp | **1** | 114 run, 1 error | `test_tui_extension_fault_integration` setUpClass "Unexpected OMP version" (same pin) — environment |
| gates/harness | 5 | 0 tests (no test modules) | |
| node `tests/bridge/*.test.ts` | 0 | 48 pass | incl. new `bridge_cw19_independent_p27cw19.test.ts` and the 2 updated independent tests |

The flaky-test fixer's concurrent edits did not show up as failures in any suite above.

## New / updated tests

New (all `*_independent_p27cw19*`):
- `tests/recovery_boot/test_reconcile_independent_p27cw19.py` (28): classify matrix (unknown marker never same
  boot, verified must be exactly `true`), BootStore durability (pending survives crash + same-boot restart,
  confirm needs the current marker, failed confirm write keeps hold, corrupt/group-readable/symlink/wrong-version
  record → pending), **boot compared before /proc** (spy on `_stat`/`observe`/session scan: an old-boot or
  unknown-boot ref with a live pid *and matching start ticks* is never read), recycled pid → `ended`, pid 1 (other
  uid) never a survivor, invalid refs ignored, session members stoppable only with live recorded leader, reconcile
  never signals, startup.json 0600 + backend.prev.json kept, lost-outbox listing (queued_not_sent vs
  submitted_outcome_unknown, terminal states excluded, only after the last `backend_start`, journal untouched,
  symlink not followed), AdmissionHold role order, `Backend.run` ordering (reconcile before `_open`, before the
  record is overwritten), reconcile exception → boot hold.
- `tests/recovery_boot/test_survivors_holds_independent_p27cw19.py` (27): stop_survivor refusals (unknown id,
  display-only, boot changed/None/raising, recycled pid, member with changed leader) with no signal; TERM→stopped,
  TERM-ignoring → KILL after grace, leader with members, recorded reason/requester, not repeated; 3 concurrent stops
  → exactly one signal; manager-only, argument validation, env value in reason rejected and not journaled, refused
  while shutting down, one answer per call key; workbench_status `backend.survivors`; hold matrix (to_worker held
  for boot/metadata/model_hold:worker, cancel allowed, manager model hold does not hold to_worker, raising hold
  callable holds; notices answer `paused` without touching the bridge; worker `terminal` held, nothing typed);
  confirm_boot lifts only the boot hold, old boot id refused, second confirm refused; backend_restarted: none for
  fresh / no Task / closed Task, one with survivors for crash / clean stop / reboot, waits through boot and manager
  model holds and is sent once.
- `tests/recovery_boot/test_faults_independent_p27cw19.py` (15): model hold per role (worker error does not hold
  manager, other role's ok does not lift, toolUse lifts, aborted/malformed/foreign-role events ignored, error+ok in
  one poll, bridge failure); metadata fail-soft (data dir 0500 → `_write_record` False, hold + fault + record_error +
  metadataHealthy false; restore → reopened), handoff journal OSError → `journal_unavailable` + metadata event;
  raw log: defaults 64/512 MiB, **real 68 MiB drained → 64 MiB stored, cap_source run, drain never raises**, project
  cap reported `project`, store failure → missing bytes + storage_error, UI/CLI text; durable pause across a new
  controller, unreadable pause file → paused, failed pause store shown (fails: P3-2).
- `tests/recovery_boot/test_cli_independent_p27cw19.py` (11): confirm-boot exit 3/1/1/1/1/0 matrix (not running,
  nothing pending, unreadable marker, no tty/--yes sends nothing, tty "n", backend refusal), sends exactly the
  current boot id; shutdown unverified / missing result → exit 1 + "종료 확인 실패", verified → 0, no confirm sent
  without --yes, active work (previous_survivor) printed.
- `tests/ui/test_product_model_independent_p27cw19.py` (6), `tests/bridge/bridge_cw19_independent_p27cw19.test.ts` (2):
  UI shows only the waits with the CLI command; survivors counted only alive/stop_unconfirmed; modelTurnOutcome table
  (aborted beats an error message, outcome carries only {ok, stopReason}); stop_survivor manager-only, strict.
- `tests/recovery_boot/live_restart_independent_p27cw19.py`: L-CW19-RESTART probe (opt-in script, not discovered).

Updated (one name each, C-D71 (1)): `tests/bridge/bridge_recovery_independent_p27cd70.test.ts:91`,
`tests/bridge/bridge_terminal_independent_p27cd68.test.ts:101`, `tests/bridge/test_cw18_schema_independent_p27w.py:64`
— manager tool set now includes `stop_survivor`.

## Findings

**P3-1 — a `stop_unconfirmed` survivor is never re-checked, so it stays "left running" after it has ended.**
`SurvivorRegistry.refresh` (`src/workbench/app/recovery.py`) only re-probes `state == "alive"`; `alive()` returns
`alive` + `stop_unconfirmed`. A survivor whose KILL was not observed within `kill_wait` keeps that state forever:
it is listed in `_active_work`, makes every later full shutdown `verified=false` /
`previous_backend_survivors_alive` (exit 1) although it is dead, is carried into the next record, and — if it is
really still alive — the manager cannot retry (`not_alive` refusal). Safe direction (never a false success) but a
wrong C-AC-22 display. Repro: `test_survivors_holds_independent_p27cw19 … test_stop_unconfirmed_survivor_that_ends_later_is_not_left_running`
(grace=0, kill_wait=0 on a TERM-ignoring child; 5/5 runs fail). Fix direction: re-probe `stop_unconfirmed` too
(exact pid+ticks → `ended`/`stopped`).

**P3-2 — a pause that cannot be stored is only logged.** `AutomationController._save_pause` logs
"could not be stored durably" but sets no `persistence_error`/fault and reports no metadata event, so the user sees
"paused" while a crash would lift the pause (R4) — and with an otherwise healthy data dir (e.g. transient ENOSPC
on that one write) no metadata hold appears either. Repro:
`test_faults_independent_p27cw19 … test_pause_that_cannot_be_stored_is_shown` (`persistence_error=None`).
Fix direction: surface it (automation `persistence_error` and/or `_metadata_event("automation_pause", …)`).

Observations (not defects; for Root/CW-16):
- O1 Real OMP 18.8.0 (L-CW19-RESTART): after SIGKILL of the backend both OMPs and the host shell ended with the PTY
  hangup; the user's `nohup` loop, the foreground loop and the `setsid` sleep survived. The two loops are listed as
  `session_member` with `identity: unverified` (leader gone, decision 4) → display-only; nothing was stoppable, so
  `stop_survivor` cannot act in this real case; the `setsid` process is not listed at all (not identifiable) and is
  not in the shutdown list. A stale-token reconnect of an old OMP could not be observed (no OMP survived; no auth
  lines in backend.log).
- O2 Short-lived children (the loops' `sleep 0.5`) are listed as extra survivors and then shown `ended` — display
  noise only.
- O3 A previous backend.json without `boot_id` while boot.json says the current boot → classification
  `boot_unknown` but no confirmation (boot.json is authoritative); refs are not probed. Label inconsistency only.
- O4 Raw-log caps and model error were not exercised at runtime: both need a Task / model turn, i.e. provider
  requests (forbidden here). Unit-verified instead (68 MiB real drain; project cap with reduced limits).
- O5 No OMP session file was written (no prompt), so "zero assistant messages" holds vacuously (0 files); the
  counting endpoint (0 requests) is the primary no-model evidence.

## L-CW19-RESTART (host, real OMP, no prompt) — 56/56 checks pass

Report: `result-p27-cw19-test-01-restart.json` (commands with exit codes, pids, boot_id, observations).
`PYTHONPATH=src /tmp/cw02-g1-venv/bin/python tests/recovery_boot/live_restart_independent_p27cw19.py <report>`;
boot_id `30e0b94c-32c5-4014-8441-0ebcc6537118`, `omp/18.8.0`, fake HOME, local counting endpoint, proxies closed.

| Step | Observed |
|---|---|
| A start (exit 0) | backend pid 525617 (ticks 297110867), classification `fresh`; host shell: nohup loop 526495, setsid sleep 527662, foreground loop 528905; pause via UI |
| B SIGKILL backend (exact pidfd) | backend, host_shell 525666, manager_omp 525669, worker_omp 525670 ended; nohup/setsid/foreground alive |
| C start (exit 0) | backend 529290; `same_boot_crash`, previous pid 525617, probed, all 4 refs `ended`; survivors: nohup + foreground listed (unverified, display-only), none signalled; pause kept; no notice (no Task); 0 outbox lost; no new outbox record; new OMPs are the bridge peers; `status` prints reconcile + survivors |
| D metadata fault | data dir 0500 + host shell `exit` → `metadata_unavailable` (backend_record: PermissionError), fault shown in status, backend `degraded` (not stopped), both OMP bridges connected; 0700 → hold lifted |
| E shutdown | no --yes/tty → exit 1, active work incl. `previous_survivor`; `--yes` → `verified:false`, problems `previous_backend_survivors_alive`, exit 1, "종료 확인 실패" |
| F start (exit 0) | `same_boot_unverified_stop`, survivors proven again; probe ended its own survivors; `shutdown --yes` → verified, exit 0 |
| G boot change (records rewritten to another boot) | `reboot`, confirmation required, refs `ended_by_reboot` not probed, `boot_confirmation_required` hold, status shows the wait; user resume allowed while held (hold stays); `confirm-boot` no --yes → 1, shows both ids + data dir; `--yes` → 0, hold lifted, boot.json `pending:false`; second confirm → 1; shutdown → verified, 0 |
| No model | 0 requests at the local endpoint; 0 OMP session files / 0 assistant messages; no residual process or /tmp dir |

L-CW19-REBOOT is not covered here (VM tester).
