# p27-cw19-fix-02 — review-02 corrections (worker_senior)

No real model/provider request. Only processes this run started were signalled (test children via `Owned`). Temp under
/tmp only. No commit, no graphify update. Independent tests and docs/.workflow (except this file) untouched.

## P2-1 — `queued` no longer promises delivery
- `src/workbench/backend/flow.py`: `QUEUED_DETAIL` (manager `to_worker`) now says "sent at most once, in order, never
  re-sent; if the user pauses automation before it is delivered, or it is rejected, it is not sent (workbench_status
  shows not_sent); decide again after the resume. Do not send it again otherwise". New `QUEUED_WORKER_DETAIL` for a
  worker → manager message: "... it is not sent and the manager decides again after the resume. Do not send it again
  unless asked". `_queue` picks it by sender/target role. `QUEUED_WAITING_DETAIL` unchanged.
- `omp_bridge/skills/to-manager/SKILL.md` Results line: "sent at most once, so never send the same report again. It is
  not delivered if the user pauses automation first or it is rejected; then the manager decides again after the
  resume, and you resend only if asked." (8 739 → 8 896 chars). The to-worker skill is not touched.
- Pause drop rule (C-D65 `held_paused`) and the fix-01 CW-19 hold-keep are unchanged. Existing pins kept:
  "Accepted", "Do not send it again", "never send the same report again".

## P3-1 — pause-store retry writes the value wanted at write time
- `src/workbench/backend/automation.py`: the store write is split into `_store_pause_locked` (sets `_pause_desired`,
  saves, updates the error/retry time; caller holds `_save_lock`) and `_pause_store_reported` (log + metadata report,
  outside the lock as before). `retry_pause_store` now re-reads `_pause_desired`, checks due and writes in one
  `_save_lock` section, so a concurrent `request_pause`/resume store waits and its value lands last. Lock scope for the
  metadata callback is unchanged (still outside `_save_lock`).

## P3-2 — an identity that cannot be pinned again is unknown, not ended
- `src/workbench/app/recovery.py` `_signal_pending`: an identity whose `_pin` fails is dropped only when
  `observe(pid, ticks) == "ended"` (the same rule as `refresh`). Otherwise (owner_changed, pidfd_unavailable:*, ...)
  it is kept: result `stop_unconfirmed`, reason `not_pinned:<whys>`, it is in `remaining`/`pending`, never signalled.
  Pinned identities are still terminated; their remaining ones merge with the kept ones. All-ended still gives
  `already_ended`. The survivor stays in `alive()`, so the full shutdown check stays unverified.

## Tests — `tests/recovery_boot/test_cw19_fix02.py` (8)
- P2-1: worker and manager `queued` details (no "it is delivered … once", "at most once", pause/rejected → not sent,
  "manager decides again after the resume", "unless asked"); to-manager skill line.
- P3-1: race made deterministic with a `_save_lock` stand-in that runs `request_pause` right after the retry's first
  lock release; the disk's last value must be `True`. Plus a not-due control.
- P3-2: real `sleep` children; `_pin` forced to fail with `owner_changed` / `pidfd_unavailable:OSError` → stays
  `stop_unconfirmed`, not signalled, listed in `alive()`; mixed retry stops the pinned one and keeps the other; ended
  identity still `already_ended`.
- Red check: same tests against a /tmp copy with the three changes reverted → 7 FAIL (the not-due control passes);
  with the changes → 8 OK.

## Suites (exit codes, `/tmp/cw02-g1-venv/bin/python -m unittest discover`, PYTHONPATH=src)
- tests/recovery_boot: 163 OK, exit 0
- tests/workflow: 62 OK, exit 0
- tests/bridge (py): 52 OK (1 skipped), exit 0; `node --test tests/bridge/*.test.ts`: 48 pass / 0 fail, exit 0
- tests/ui: 836 OK, exit 0
- tests/backend: 1095 OK (30 skipped, 1 expected failure), exit 0
