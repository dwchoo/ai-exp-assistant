# p27-cd70-review-01 result (reviewer, Opus, fresh, read-only) — 2026-10-07

Verdict: **block** (P2 ×1 on the normal `restart_worker` recovery path; P3 ×4). No repo file edited except this one; no provider request; temp only under /tmp (scratchpad); no process signalled; no commit; no graphify update.

Scope: `git diff HEAD` (20 files) + untracked `flow_recovery.py`, `omp_bridge/skills/workbench-recovery/`, `tests/backend/test_cd70_*.py`, `tests/ui/test_product_model_cd70.py`, against C-D70 (1)–(5) and the listed invariants. Sanity: `test_cd70_*` 63 OK, `test_product_model_cd70` 4 OK.

## Findings

### P2-1 — Duplicate full-Task re-send / redundant follow-up after `restart_worker` (confirmed)
- `src/workbench/backend/flow_tasks.py:786-796` `_needs_resend` only guards the first TASK (`_task_message_state` queued/delivering). For a Task whose TASK was already sent, a re-send that is queued but not yet submitted leaves `task.worker_session` at the old session, so every further follow-up is again a full re-send (`_resend`, :799) with a `commands_already_run` list frozen at decide time.
- `src/workbench/backend/flow_recovery.py:313-330` + `RESTARTED_HINT` (:77-80): `worker_restarted` is queued for every cause, including the manager's own `restart_worker`. The manager is mid-turn when it calls the tool, so the notice is delivered only after that turn — in which the tool result (`RESTART_DETAIL`, :89-92) and the skill (`workbench-recovery` step 2, "call restart_worker …, then send the follow-up") already made it send the follow-up. The notice then says "send a follow-up to_worker on this task_id to continue" with no "if you already did, end your turn".
- Scenario: restart_worker → follow-up F1 (re-send R1 queued; new worker still starting/busy) → turn ends → `worker_restarted` notice → F2. Reproduced (scratchpad test with the real TaskFlow/HandoffService fixture of `test_cd70_task_resend.py`, worker deliveries deferred, two follow-ups after a session change): `R1 queued resent_task=True, R2 queued resent_task=True`, **2 full-Task re-sends created** (expected 1). The new worker receives the whole Task twice; R2's `commands_already_run` omits what it ran after R1, and C-D69 (6) still permits those listed commands, so a re-run is possible. If R1 was already submitted, F2 is a plain duplicate "continue" (extra worker turn, confusing after a done report).
- Fix direction: in `_needs_resend` treat a re-send queued/delivering for the current session as "has it" (plain follow-up); and/or for `cause == restart_worker, requester == manager` word the notice as confirmation ("you already restarted it; if you sent the follow-up, just end your turn") or skip the follow-up instruction.

### P3-1 — Unknown TASK delivery: watchdog inert and a false "you were restarted" re-send
`flow_tasks.py:1286-1294` sets `worker_session` only on submitted/delivered. A TASK whose delivery ended `unknown` (the worker may have it) keeps `worker_session=None`: `flow_recovery.py:373-375` then never status-checks that Task, and the manager's next follow-up becomes a full re-send (`_needs_resend` compares None ≠ current) whose `RESEND_NOTE` (:148-152) tells a worker that may hold the Task "the session that had it ended (restart) … you are a new session" — false text and re-sent content of an unknown delivery.

### P3-2 — `_resend(first=True)` held at queue leaves `_task_message_state="queued"` forever
`flow_tasks.py:819-821` sets the state to `queued` (and journals `task_resent`) inside `decide`; `HandoffService._queue` (`flow.py:1001-1007`) can still return `held("target_not_connected")`/`mailbox_unavailable` with no listener event. Afterwards `_needs_resend` (:793) returns None for every follow-up, so the never-delivered Task is never re-sent and follow-ups go as plain QUESTIONs to a worker without the Task. Narrow (worker disconnect between decide and queue, e.g. during a restart).

### P3-3 — `restart_worker` with `registered: false` still says "send a follow-up now"
`service.py:396-411` returns `status: restarted, registered: false` with `RESTART_DETAIL` ("Send a follow-up to_worker … Then end your turn"). With no worker peer the follow-up is `held: target_not_connected` (`flow.py:1006-1007`), which invites a retry loop; the detail should say to end the turn and wait for `worker_restarted` when not registered (as the `restarting` branch at :388-390 does).

### P3-4 — Shutdown race leaves a job unserviced
`service.py:375-386` vs `_close` (:465-466): a tool thread that passed `_shutting_down()` and acquired `_restart_lock` but appends its job after `_abort_restart_jobs` ran is never processed; the tool waits 8 s and answers `restarting … a worker_restarted notice follows`, which is false during shutdown. Only at shutdown; low impact.

## Checked, no finding
- CW-08: re-send kinds TASK/QUESTION pass `roleAllows`; `_SESSION_CHANGED` requeue only on `BridgeBoundMismatch`/`target_session_changed`, both raised before any bytes are sent (`mailbox.py:495-519, 868-874`); unknown deliveries never requeued; worker-bound messages still dropped.
- Session binding: bridge rejects tool frames with a mismatched session/generation (`mailbox.py:317-319`); notices use `expected_peer`; generation changes only on `session_switch` (`bridge.ts:1119`), not on reconnect, so no false `worker_restarted` on a socket reconnect.
- Pause: watchdog skips when paused (port failure = paused); bridge defers notices when paused.
- C-D62: user `restart_pane` still refuses a live pane (`service.py:709-711`); restart lock shared; isolation re-check blocks a second restart.
- Kill scope: `OmpPane.terminate` only signals the proven unreaped OMP group; `close` KILLs only pinned own-session members; host shell untouched.
- Backend loop: restart grace is a non-blocking state machine; tool handlers run on their own threads (`mailbox.py:330`); `workbench_status` ≤1 s probe; tool budgets 8/9 s < bridge 10 s.
- Locks: no lock-order cycle found (watchdog `_lock` → terminal lock / journal; nothing calls back into the watchdog under those). Watchdog thread joined in `_close`.
- OMP 18.7.0 (`omp --version` = omp/18.7.0, compiled binary `~/.local/bin/omp`; global `node_modules/@oh-my-pi` is a stale 18.2.10): embedded sources still provide ExtensionContext `isIdle`, `hasPendingMessages`, `ui.getEditorText`, `agent` (`kind: "sub"`), `pi.setActiveTools/getActiveTools`, `pi.sendUserMessage(text, opts)` with `attribution`, events `tool_approval_requested/resolved`, `tool_execution_start`, `before_provider_request`, `after_provider_response`, `session_switch/shutdown`, tool `loadMode`, skill frontmatter `hide`/`disableModelInvocation` (both hide from the model) and `skills.enableSkillCommands`/`includeSkills`. No API used by `bridge.ts` is missing. The implementer's frontmatter conclusion (no per-skill slash-menu-only hide) matches.
- Model-facing text otherwise concise; `status_check` and stalled/unknown/recovery hints are unambiguous apart from P2-1/P3-1/P3-3.

Not run: full suites, real OMP smoke (no provider requests). Independent tests listed in result-p27-cd70-01 are being updated by the test_designer and were ignored.
