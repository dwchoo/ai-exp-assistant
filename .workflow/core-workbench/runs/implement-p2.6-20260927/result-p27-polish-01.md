# p27-polish-01 result (worker_senior)

base 72e7d18. No commit, no graphify update, no real model/provider requests. Temp only under /tmp (`/tmp/p27-polish-01` logs, test temp dirs cleaned).

## Checks (final tree, real exit codes)

| suite | result | exit |
|---|---|---|
| tests/backend | 1095 OK (skip 30, xfail 1) | 0 |
| tests/bridge (py) | 52 OK (skip 1) | 0 |
| tests/bridge node `node --test tests/bridge/*.test.ts` | 43 pass, 0 fail | 0 |
| tests/terminal | 221 OK (skip 2) | 0 |
| tests/ui | 826 OK | 0 |
| tests/workflow | 58 OK | 0 |
| tests/gates/g2_shell | 142 OK (skip 1) | 0 |
| tests/contracts | 52 OK | 0 |

The first backend run (before the item 4 narrowing below) had 1 fail: `test_flow_terminal_notices.CompletionNoticeTests.test_an_aborted_waiting_call_does_not_swallow_the_notice`, a real contract break by my first version, fixed. The second full run was green. bridge.ts was not changed, and the node run was on the unchanged file.

New tests (red on the base, green after the fix): `tests/backend/test_delivery_polish_p27.py` (6; 3 red), `tests/backend/test_terminal_polish_p27.py` (10; 6 red in the first version), `tests/backend/test_cd70_resent_count_p27.py` (3; 2 red), `tests/terminal/test_vt_resize_savepoint.py` (5; 4 red).
Changed existing test (not independent): `tests/backend/test_cw18_review_corrections.py` RealMailboxTests. It expected `unknown` after the receipt window ended and now expects `delivered`/`receipt_window_ended` and no `unknown`.

## Items

### (1) BridgeTimeout unknown noise
- **Cause:** after `api_accepted`, `TaskMailbox.deliver` waits for `delivery_omp_processed` only for `timeout=deliver_timeout` (20 s, `flow.py` HandoffService). That covers the target OMP's whole turn. A worker's first turn with a 120 s terminal wait, or a manager turn that reads a report, takes longer. `wait_any_event` raises BridgeTimeout, which becomes `UNKNOWN {reason: BridgeTimeout, stage: omp_processing_observation}` (mailbox.py ~1080), and `_TERMINAL_STATES` writes `unknown` to the outbox journal. `on_submitted` had already fired, so the listeners and C-D70 already treated it as submitted (no notice, no resend). Only the record was wrong.
- **Fix:** `src/workbench/backend/flow.py:97-99` adds `RECEIPT_WINDOW_ENDED`. `flow.py:1248-1252` `_deliver_once`: if `entry.submitted`, reason is `BridgeTimeout`, and stage is `omp_processing_observation`, the outcome becomes `API_RETURNED` (state `delivered`, reason `receipt_window_ended`), meaning "submitted, turn outcome pending". The module docstring is updated to match.
- **Unchanged:** no-replay (ledger/receipt unchanged; resend count 1 checked with the real TaskMailbox). BridgeDisconnected during the turn, an aborted turn, and timeouts with no known submission all stay `unknown`. C-D70 `report_delivery_unknown` (never-submitted unknown only) is unchanged. The mailbox/SQLite delivery evidence and workflow stage semantics (`OMP_PROCESSED` required) are unchanged. The 10 s tool budget is untouched because `handle` never waits.

### (2) Empty 0600 .log on abort before submit
- **Cause:** the `_CallAbandoned` path already removed the log (fix-03). The `start_failed` path (pause right before submit, shell cwd changed, unknown executable, submit exception) left an empty, unreferenced log because no output follower runs for it.
- **Fix:** `src/workbench/backend/flow_terminal.py:318-327` adds `_drop_unrun_files` (removes only an empty log). `:1032` calls it on the start_failed path. `:1231` sets `command.submitting` before `port.submit`, so a script the shell may already have received is kept. The abandon path at `:1018` uses the same helper.

### (3) DECSC savepoint on shrink
- **Fix:** `src/workbench/terminal/vt_g1/screen.py:189-192`: when `_resize_screen` drops rows, the y of every savepoint moves up by `drop`, clamped to `0..lines-1`. This also covers the primary screen resized when leaving the alternate screen.

### (4) Dead waiter after worker disconnect
- **Cause:** `peer_gone` (fix-03) marked the waiter abandoned so checks resumed, but the waiting thread slept until its deadline (≤120 s). When it woke, it ran the "call saw the output" bookkeeping: `_drop_check("superseded")`, `check_base` reset, `take_since`/`take_unseen`. That dropped the check pending for the restarted worker, delayed the next check by one more interval, and consumed the unseen output.
- **Fix:** `flow_terminal.py:666-700` `_await` waits on a per-call wake event set by the command's end (`:1268-1271`), `peer_gone` (`:752-762`, `_gone`) or `close`. When the waiter ends with its key abandoned, the check, its window and the unseen output stay as they are (`:692`). A call the bridge answered itself (`terminal_wait_abandoned`) still waits for the backend answer as before. My first version also released those early, which broke the existing contract test, so I narrowed it.
- **Not done:** the review-03 P3 (1) same-key reconnect race (connection serial) needs plumbing through the bridge peer and is outside this ticket's paths.

### (5) reports_resent skew
- **Cause:** `_unbound_for` counted an unbound report once, in whichever `manager_recovery` came first, then forgot it. If S2 counted it, went away before it was created, and it was then created for S3, the S3 notice said `reports_resent 0`.
- **Fix:** `flow.py:787-789` replaces `_unbound_uncounted` with a `handoff_id → last counted session` map. `flow.py:954-985` `_unbound_for` counts per delivering session: once per session, counted again by the session it is created for, no longer tracked once delivered to or ended at another session. Entries created for an old session are still counted by the existing requeue path (set union, so no double count).

## Residue
`git status`: src 3 files, tests 1 modified + 4 new, and this result file. The pre-existing `.workflow` M/?? files are not mine. No processes left running; I killed only my own test PIDs, and there were none to kill.
