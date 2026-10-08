# p27-cw19-review-02 — fix-01 delta review (fresh reviewer, read-only)

Scope: the fix-01 corrections in the uncommitted tree (`git diff HEAD -- src omp_bridge tests` and the untracked CW-19
files), checked against every Root adjudication in assignment-p27-cw19-fix-01.json. Nothing was written except this
file. No model or provider request was made. No process outside this reviewer's own test runs was signalled.

Checks run (temp files only under /tmp):
- `PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest discover -s tests/recovery_boot -t tests/recovery_boot -p 'test_*.py'`: exit 0, 155 OK.
- The same command for `tests/workflow`: exit 0, 62 OK.
- The same command for `tests/backend`: see the last section.
- `node --test tests/bridge/*.test.ts`: exit 0, 48 pass.

## Verdict: **block** (1 × P2, 2 × P3)

## Adjudications closed (verified in code)

- **Review P2-1 (hold).** `flow.py:1272-1281` keeps a worker's own `to_manager` entry (`_worker_call_to_manager`
  :1253) through any CW-19 hold of the manager (`_hold_paused`: pending, re-checked every `retry_interval`).
  - No replay: the hold check runs before `_create`, so a held entry is never submitted. An entry that was already
    created (an earlier deferral) keeps its single message. Nothing is duplicated when the hold lifts.
  - Order: kept entries get their `due` in lane order, and `_next_due` scans in queue order, so kept messages keep
    their order. A message queued less than `retry_interval` (0.5 s) after the lift can overtake a kept one. The
    `_defer` backoff already has this behaviour, so it is not a new defect.
  - Shutdown: a kept entry that is still pending when the backend shuts down is not terminal. It shows up in the next
    start's `outbox_lost`.
- **Review P2-2.** `service.py:296-307` `_hold_reason` applies the `model_turn_result` events already received when the
  answer is `model_hold:*`. In `mailbox.py:411-423` `_receive` appends the `omp_event` under `_condition`, and only
  after that does it start the thread for a later `tool_request` from the same socket, so the lifting event is always
  seen first.
  - Lock order: `_model_lock` → bridge `_condition` → `AdmissionHold._lock`. None of these calls back into the flow or
    the bridge, so there is no inversion.
  - Effects such as `model_changed` stay on the loop thread (`_poll_model_events` :787).
  - Test: `ModelHoldLiftingTurnCallsTerminal`.
- **Review P3-1 / test P3-1 (`stop_unconfirmed`).** `recovery.py:261-283` `refresh` re-observes the pending
  `(pid, ticks)` identities. An identity with an `unknown` answer is kept, which is conservative. `stop` checks the boot
  again before any signal (:324-331). A retry pins the exact identities again with pidfd, ticks and uid (`_pin`), so
  a recycled pid is never signalled. See P3-2 for one gap.
- **Experiment report under a manager hold.** `run.py:358-368` creates the report but does not submit it (`deferred`,
  `held`). The deferred loop in `flow_tasks.py:1787-1800` waits until `_held_now(MANAGER)` is clear (synced) and then
  sends it once with `retry_report`. `retry_report` clears `held` (`run.py:579`).
- **`TaskWorkflow.start` raw-store error.** A `fail_run` with stage `artifact_setup` follows any exception.
- **Journal larger than 64 MiB.** `iter_jsonl` (`recovery.py:469`) streams the journal with `readline(1 MiB)`. The
  over-long-line skip is correct, including the last chunk. `lost_outbox` resets at `backend_start`, forgets entries
  in a terminal state and tracks at most 4096 entries, so memory stays bounded. `read_jsonl` now reads the tail.
- **Texts.** The to-worker skill names the new `held:` reasons and how to handle them (10 994 / 11 000 chars), and its
  text for `backend_restarted` matches `BACKEND_RESTARTED_HINT`. `reconcile_failed` has its own text in the UI
  (`model.py:135,2035`) and in the CLI (`cli.py:171`).
- **VM F1.** `flow_recovery.py:553-564` builds the notice again before each attempt. `None` drops the notice unsent and
  journals the drop. `build` runs under `_tick_lock`, and the tick already calls `flow.task_view` there, so it adds
  no new lock order.
- **VM F2.** The boot wait text comes first on the status line.
- **Test P3-2.** A failed pause save leads to `persistence_error`, a metadata hold and `faults.metadata`, and the save
  is retried every 5 s. A durable write from another source does not clear the `automation_pause` latch
  (`service.py:316-326`). See P3-1 for one race.

## Findings

### P2-1 — The worker's `queued` text still promises a delivery that a pause (or a `rejected` ending) cancels
- Code: `src/workbench/backend/flow.py:104-105` `QUEUED_DETAIL` says "it is delivered to the other OMP once, in order.
  Do not send it again". `omp_bridge/skills/to-manager/SKILL.md:56` says "it is delivered once, so never send the
  same report again". On the other side, `flow.py:1263-1270` drops a queued worker message (`held_paused`, C-D65)
  when it reaches the lane while the user has paused.
- Failure scenario:
  1. The worker sends `to_manager` with `kind: "answer"` while the manager OMP is in a turn. The entry is deferred and
     its backoff goes up to 2 s. `editor_not_empty` can make it wait much longer.
  2. The user presses pause. The lane's next attempt ends the entry as `held_paused`.
  3. The worker was told the message would be delivered once and that it must not send it again. The worker is never
     told about the drop.
  4. After the resume, the answer never reaches the manager. The only trace is a `not_sent` row in
     `workbench_status`, which the manager reads only if it calls that tool for some other reason.
  The same holds for a worker message that ends `rejected`: the requeue limit, a mailbox error, or `prepare_failed`.
- This is pre-existing text, but fix-01 left it as a known residual. The fix-01 adjudication for P2-1 requires "the
  worker result is truthful", and this assignment requires that the wording must not promise delivery. That item is
  therefore not closed.
- Fix direction (no to-worker skill budget involved):
  - Change `QUEUED_DETAIL` to something like "sent at most once, never re-sent; if the user pauses before it is
    delivered it is not sent (the manager sees it as not_sent in workbench_status). Do not send it again unless
    asked".
  - Make the same change in to-manager `SKILL.md:56` (8 739 chars, so there is room).
  - Check the independent tests that pin the phrase. A grep for the exact text in `tests/` found none.

### P3-1 — A pause-store retry can write a stale value over a newer pause or resume
- Code: `src/workbench/backend/automation.py:384-391` `retry_pause_store` reads `_pause_desired` under `_save_lock`,
  releases the lock, then calls `_save_pause(bool(desired))`, which takes the lock again and writes that value
  (:354-357). There is no check that the wanted value is still the same.
- Failure scenario: a resume is not stored (`resume_not_stored`, desired=False), and the store then recovers. The loop
  thread reads desired=False. Before it saves, the user pauses: `request_pause` → `_save_pause(True)` succeeds. The
  retry then writes `paused=false` into `automation.json` and sets `_pause_desired=False` with no error. The pause
  stays in memory, but a backend crash now lifts it on restart. That breaks R4 ("recovery never lifts the pause").
  The window is small (one lock gap).
- Fix direction: keep `_save_lock` held across the retry save, or have `_save_pause` skip the save when
  `_pause_desired` has changed since the retry read it.

### P3-2 — A survivor-stop retry treats an identity it could not pin again as ended
- Code: `src/workbench/app/recovery.py:370-382` `_signal_pending` keeps only the identities whose `_pin` returned a
  pidfd. A failure for `owner_changed` or `pidfd_unavailable:*` (for example EMFILE) drops that identity silently.
  It is then missing from `remaining` and `pending`. When no identity was pinned, the result is `already_ended` and
  the survivor becomes `stopped` (:333-340).
- `refresh` handles the same case conservatively: `observe` → `unknown` keeps the identity (:265).
- Failure scenario: the retry runs out of fds while one old-backend process is still alive. The survivor is reported
  `stopped`, and the full shutdown's C-AC-22 check then shows `verified=true` while that process still runs. This is
  fail-open in a rare case.
- Fix direction: drop an identity only when `why == "ended"`. Keep every other one in `pending`, so the result is
  `stop_unconfirmed` with the reason.

## Not findings (checked)
- Pause before hold in `_deliver_once`: a worker message that is both paused and held follows the pause rule (C-D65).
  This is intended and is now listed as `not_sent`.
- `lane_busy` is true while a worker message is kept under a manager hold, so the watchdog waits. The same was true
  for kept reports before this change.
- In the deferred report loop, the manager session can change while the report is held. That path is the same as
  the existing `manager_busy` path.
- The `metadata_event(None)` from the pause store clears every latch. This is Root-accepted decision 7.

## Backend suite
- `PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest discover -s tests/backend -t tests/backend -p 'test_*.py'`: exit 0, 1095 tests, OK (30 skipped, 1 expected failure).
