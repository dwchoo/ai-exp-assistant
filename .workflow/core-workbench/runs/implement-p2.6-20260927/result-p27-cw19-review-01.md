# p27-cw19-review-01 — CW-19 frozen-candidate review (fresh reviewer, read-only)

Scope: `git diff a12e668 -- src omp_bridge` and the untracked new src files (`app/recovery.py`, `backend/boot.py`,
`policy/recovery_manager/port_sqlite.py`). Checked against CW-19, C-D71 (1)–(5) and the 8 decisions Root accepted in
result-p27-cw19-01.md. Test churn from other agents was ignored. Check run (temp files only in /tmp):
`PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest discover -s tests/recovery_boot -t tests/recovery_boot -p 'test_*.py'`
→ exit 0, 80 tests OK. No model or provider request was made, no process was signalled, and nothing was written
except this file.

## Verdict: **block** (2 × P2)

## What holds (verified in code)

- **Exact identity before any kill.** `SurvivorRegistry.stop` (`app/recovery.py:291`) checks in this order:
  `stoppable`, then `state`, then that the boot is unchanged (`boot != survivor.boot_id` → refused, :313) before any
  `/proc` or pid use, then a pidfd pin with start ticks and uid (`_pin` :327). For a session member, the live leader
  is pinned and its session is checked again. Members of a leader's session are signalled only through pidfds, and
  the leader must still be alive after the scan (:376). `_signal_exact` (`automation.py:240`) uses a pidfd and then
  checks the start ticks. The shutdown never signals survivors or left-session processes; they only make
  `verified=false` (`service.py:284-292`).
- **Boot checked before pids.** `StartupReconciler.add` (`recovery.py:545`) reads `/proc` only when the
  classification is same-boot and the ref's boot equals the current boot. Otherwise the result is
  `ended_by_reboot` or `not_probed_boot_unknown`.
- **Fail closed and durable.** `BootStore.begin` (`boot.py:152`) writes pending=true before anything is served.
  `confirm` writes first and only then lifts the hold (`service.py:968-989`). A reconcile exception gives
  `reconcile_failed` together with a metadata hold, and the boot record is rewritten before the hold can open
  (`service.py:326`).
- **Startup order.** `run()` takes the lock, then `_reconcile_startup`, then `_open` (`service.py:207-221`). The
  `backend_start` marker is written after the reconcile, and `lost_outbox` reads only what comes after the last
  marker.
- **No replay.** The outbox, notices and TaskFlow only hold or keep entries. A kept message is never submitted, so
  holding it causes no resend.
- **Durable pause.** Restore, save and resume all go through `automation.json`; an unreadable file means paused.
- **RecoveryPort.** `BEGIN IMMEDIATE` CAS, `UNIQUE` decision id, `synchronous=FULL`, 0600 on create.
- **Raw log.** The 64/512 MiB caps go through RawLogStore. `_drain` never raises. The judgment is `indeterminate`
  when the log is incomplete. Worker terminal logs are unchanged (C-D71 (5)).
- **Truthful shutdown (C-AC-22).** The CLI prints "종료 확인 실패" and exits 1.

## Findings

### P2-1 — A worker `to_manager` that is not a free-work report returns `queued` but is silently dropped under any hold
- Code: `src/workbench/backend/flow.py:1263-1268` together with `src/workbench/backend/flow_tasks.py:1083-1100`
  (`keep_across_pause=report`, and `report` is true only for free-work `done`/`blocked`).
- What happens: `_check` holds only `to_worker` (`flow.py:1088`), so a worker's `to_manager` is decided and the
  worker gets `{"status": "queued"}`. In the outbox lane, `_hold_reason(MANAGER)` covers boot, metadata,
  `model_hold:manager` and shutdown. If `keep_across_pause` is false, the entry ends as
  `_finish(..., "held_paused", None, hold)`. There is no listener, the worker is not told, and the entry is not
  listed later: `held_paused` is in `OUTBOX_TERMINAL`, so `lost_outbox` and `startup.outbox_lost` skip it.
- Failure scenario: during a free-work Task, the manager OMP's turn ends with `stopReason=error`, for example a
  provider overload, and `model_hold:manager` is set. The worker sends `to_manager` with `kind: "answer"` (or
  `done` on an experiment Task). It receives `queued`, and the message is dropped. When the user later prompts the
  manager and the hold lifts, the answer is gone and nothing in the UI, CLI or status shows it.
  The same thing happens before `confirm-boot`: the user prompts the worker directly (an allowed action), and the
  worker's reply to the manager disappears.
- Contradicts accepted decision 2: "A worker's `to_manager` call is not refused …; its delivery waits in the
  outbox while the manager is held." In the code, only free-work done/blocked waits.
- Fix direction: keep worker→manager entries across a CW-19 hold (`_hold_paused`) whatever the kind. Or refuse
  them at `_check` with `held:<reason>` so the worker knows.

### P2-2 — A worker `terminal` call in the turn that clears a model hold is very likely refused as `held:model_hold:worker`
- Code: `omp_bridge/g3/bridge.ts:1343-1347` sends `model_turn_result` at assistant `message_end`, which includes
  `toolUse`, before the tool runs. The hold is lifted only when `Backend._poll_model_events` runs on the 50 ms loop
  tick (`service.py:531`, `_tick(0.05)`). The worker `terminal` request is decided right away on the bridge
  request path: `TerminalService._start_unlocked` → `_held_now()` (`flow_terminal.py:1005`), with
  `hold=lambda: self._hold_reason(ActorRole.WORKER)` (`service.py:238`).
- Failure scenario: a worker turn fails with a model error, so `model_hold:worker` is set. The user types
  "continue" into the worker pane, which C-D71 allows. The worker's first assistant message ends with `toolUse`
  (`terminal`). The `ok` event and the `tool_request` frame come in on the same socket within a few ms, before the
  next loop poll. The command is refused with `HELD_DETAIL` ("End your turn; do not retry in a loop"). The worker
  ends its turn, the hold is already gone, and the user has to prompt again. The user's direct action fails once
  and a model turn is wasted. The tests do not cover this sequence: `test_backend_restart.py:310` only checks
  `to_worker` and notices.
- Verdict: PLAUSIBLE. This follows from the ordering and timing; I did not reproduce it at runtime.
- Fix direction: process `model_turn_result` when the event is received (a bridge event hook), or call
  `_poll_model_events` before evaluating the hold for that role's own tool calls.

### P3-1 — `stop_unconfirmed` is never observed again
- `SurvivorRegistry.refresh` only refreshes entries whose state is `alive` (`recovery.py:256`). `alive()` includes
  `stop_unconfirmed` (:273), and `stop()` refuses with `not_alive` when the state is not `alive` (:305).
- Scenario: a process in D-state does not die within 1 s after KILL and exits a few seconds later. It stays
  `stop_unconfirmed`, and the full shutdown keeps reporting `previous_backend_survivors_alive` with
  `verified=false` and exit 1, which is false. The manager cannot retry the stop.

### P3-2 — During `model_hold:manager`, the experiment report still reaches the manager
- The pre-judge check only holds the `WORKER` role (`flow_tasks.py:1743`). `run.judge` then sends the report to the
  manager through the mailbox directly, not through the outbox lane, so the manager-role hold is bypassed. Only
  `retry_report` checks `MANAGER` (:1787).
- Scenario: the manager's model has an error and the experiment finishes. A report is delivered to the manager, and
  a new manager turn (another model request) starts.

### P3-3 — Raw-log store errors at experiment start are not caught
- `TaskWorkflow.start` (`workflow/run.py:719-727`) catches only `OSError` around `raw_store.append(run_id, b"")`.
  `RawLogStore` raises `RuntimeError("RawLogStore is closed")` or `StoreIntegrityError(RuntimeError)`.
- Scenario: when that happens, `fail_run` is not called, and the run created by `start_run` is left in the repository
  without an `artifact_setup` failure record.

### P3-4 — `lost_outbox` reads the start of the journal, not the end
- `read_jsonl` (`recovery.py:416-423`) reads the first 64 MiB of `handoff-journal`.
- Scenario: if the journal grows beyond 64 MiB, the last `backend_start` marker and the latest outbox records are
  never read. `outbox_lost` then shows old entries, or none.

### P3-5 — Model-facing and user-facing text gaps
- `omp_bridge/skills/to-worker/SKILL.md:47-52` does not list the new `held:` reasons:
  `boot_confirmation_required`, `metadata_unavailable`, `model_hold:worker`, `shutdown_closing` and `hold_unknown`.
  The bare `held()` result has no detail (`flow.py:221`), so the manager gets no instruction such as "tell the user
  to run `confirm-boot`" or "do not loop".
- The same skill says to wait or cancel for `held:backend_restarted`, while `BACKEND_RESTARTED_HINT` says to send
  one follow-up `to_worker`. The two disagree.
- The UI shows `BOOT_WAIT_TEXT` ("재부팅 확인 대기") for `reconcile_failed` (`ui/product/model.py:2028`), which
  presents a reconcile failure as a reboot.

## Not findings (checked)

- Lock order: `AutomationController` calls coordinator methods without holding `_lock`, so `_linear_lock` →
  `_lock` cannot invert.
- Bridge events take `role` from the authenticated peer.
- A subagent session sends no events because it has no bridge connection.
- `stop_survivor` is checked for the manager role on both the backend side and the bridge side.
- The env-value check happens before `reason` is journaled.
- Clearing every metadata latch on any durable write is Root-accepted decision 7.
