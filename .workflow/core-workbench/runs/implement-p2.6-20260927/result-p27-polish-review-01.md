# p27-polish-review-01 (reviewer)

Verdict: **pass** (integrate). No P0/P1/P2.

## Findings
- P3 `flow.py:1248-1252`: the receipt-window end is recorded `delivered/api_returned/receipt_window_ended`, but the mailbox ledger attempt stays `unknown` (mailbox.py ~1068) and a later `delivery_omp_processed` is never written back. The outbox "outcome pending" never resolves. Display/journal only; no consumer branches on it. Fix direction: if needed, an `omp_processed` follow-up journal line.
- P3 `flow_terminal.py:1041`/`:1242`: `submitting` is set before `port.submit`; if submit raises before any write, a spill script stays on disk (harmless leftover; the log is removed).
- P3 `flow_terminal.py:692`: an abandoned (bridge-answered) waiter no longer restarts the check window at its deadline. This is intended and tested; noted only as a behavior change.

## Key risks
1. Narrow condition only: `UNKNOWN` + `entry.submitted` + `BridgeTimeout` + `stage=omp_processing_observation` (set only after `ledger.finish(api_accepted)` and `on_submitted`). Never-submitted `BridgeTimeout` (stage api_return), `BridgeDisconnected` and aborted turns stay `unknown` (tests). No replay (1 request with the real TaskMailbox); `report_delivery_unknown` path (`_report_lost`) untouched; the listeners treat `submitted`/`delivered`/`unknown`-after-submitted the same (`flow_tasks.py` settled/submitted guards).
3. DECSC: each savepoint y is shifted by `drop`, clamped 0..lines-1; alt screen state swaps `__dict__`, so the primary savepoints shift on `_leave_alternate`. Correct.
4. peer_gone: `_gone`/abandoned are set before the wake, and the waiter re-checks flags after `clear()`, so no lost wake. `_refresh_notice` excludes abandoned keys, so no double notice. Check/unseen output are kept (tested).
5. `_unbound_counted` is per delivering session; once per session, dropped when delivered/ended elsewhere, no double count with the requeue union (3 tests).

## Runs (real exit codes)
backend 1095 OK (skip 30, xfail 1) exit 0; bridge py 52 OK exit 0; node 43 pass exit 0; terminal OK exit 0; new tests (6+10+3+13 resize) OK. Not re-run: ui/workflow/g2_shell/contracts (the diff touches no code they import beyond the covered suites).
