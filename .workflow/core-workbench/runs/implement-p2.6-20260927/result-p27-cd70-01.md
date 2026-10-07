# p27-cd70-01 result (worker_senior, Opus) — 2026-10-07

C-D70 implemented: Workbench watchdog (status_check ×2 → worker_stalled), manager notices (worker_stalled, report_delivery_unknown, worker_restarted, manager_recovery), editor-wait status line, manager tools `restart_worker` / `workbench_status`, full-Task re-send to a new worker session, report re-queue to a new manager session, manager-only `workbench-recovery` skill. No real model/provider request, no commit, no graphify update, temp only under /tmp, only PIDs this run started were signalled.

## Checks (final code; `PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest discover -s tests/<suite> -t tests/<suite> -p 'test_*.py'`, node `--test tests/bridge/*.test.ts`)

| suite | baseline (HEAD) | final | exit | failures |
|---|---|---|---|---|
| tests/backend | 928 OK (skip 30, xfail 1) | 985 run | **1** | 5, all `*_independent*` (below) |
| tests/bridge (py) | 52 OK (skip 1) | 52 run | **1** | 2, both `test_cw18_schema_independent_p27w` |
| tests/bridge node (all *.test.ts) | — | pass 38 / fail 1 | **1** | `bridge_terminal_independent_p27cd68.test.ts:89` |
| tests/terminal | 208 OK | 208 OK (skip 2) | 0 | — |
| tests/workflow | 58 OK | 58 OK | 0 | — |
| tests/gates/g2_shell | 142 OK | 142 OK (skip 1) | 0 | — |
| tests/contracts | 52 OK | 52 OK | 0 | — |
| tests/ui | 815 OK | 819 OK | 0 | — |
| new: test_cd70_* (53) + test_product_model_cd70 (4) + handoff_tools.test.ts (18) | red at HEAD (missing modules/APIs; node 3 fail) | green | 0 | — |

Every non-independent test passes. Red→green: new tests run against a `git archive HEAD` copy failed (ImportError for flow_recovery/REQUEUE_LIMIT/RESEND_NOTE/model constants; node: 3 fail; skills: 5 fail, 3 error), green on the final code.

## Item status
- (1) Watchdog — done. `flow_recovery.py:173 Watchdog`, `_worker_step` :383. One thread (1 s tick), worker probe ≤ every 5 s, turn events via `G3BridgeServer.events_since` (mailbox.py:589). Conditions: open *work* Task `running` whose TASK reached the worker's *current* session, probe idle/no pending/no approval/0 tools, no worker terminal command running and no pending terminal_done (`TerminalService.watch_state`, flow_terminal.py:498), no outbox entry to/from worker (`HandoffService.lane_busy`, flow.py:847), not paused. 60 s → `status_check`; max 2; +60 s → one `worker_stalled`; `worker_acted` (service `_tool_request`, terminal/to_manager) resets. Deferred notices retried with the same notice_id; journaled `workbench_notice`. Constants overridable via kwargs.
- (2) Manager notices / editor wait / unknown — done. bridge.ts:346 per-role `NOTICE_TYPES`, :750. Mailbox deferral carries `blockers` (`state_blockers`, mailbox.py:704) → outbox `editor_since` → `Watchdog.report_wait` (:238) → snapshot `recovery.report_wait` → UI `recovery_text` (model.py:1996, "worker 보고 대기 중: manager 입력창을 비우면 전달됩니다"). Unknown (never submitted) worker report → one `report_delivery_unknown` (task_id, message_id, kind; no content).
- (3) `restart_worker` — done. service.py:1034/1108. Tool thread validates (reason required, non-blank, ≤500, env-value check), takes `_restart_lock` non-blocking (refuses `restart_in_progress`, also during the worker's isolation re-check; `backend_shutdown`), queues a job; the backend loop sends SIGTERM to the worker OMP's own group (`OmpPane.terminate`, panes.py:414, only while the unreaped child is proven), waits `RESTART_GRACE`=3 s without blocking the loop, then `_respawn(close_grace=0)` (existing close: KILL of its own session members; same home/overlay/model table/isolation re-check, generation+1). Host shell untouched; Task not cancelled. Restart entry/pane info/record carry `cause, requester, reason, at`; UI status line shows "worker 재시작됨 (manager 요청: …)" for 120 s. Result: new pane generation/pid, bridge session/generation (waits ≤9 s total), Task state, terminal state, "no memory … follow-up on the same task_id or cancel". Worker gets `tool_not_allowed_for_role`; C-D62 user restart still refuses a live pane (tested).
- (4) Worker new session — done. `worker_restarted` (cause from restart history: restart_worker/user_restart/omp_new_session/unknown) for an open busy or blocked Task. FlowTask `worker_session` recorded at TASK submission; follow-up to another session → full TASK payload (`_task_payload`, flow_tasks.py:1222) + `commands_already_run` (terminal journal) + `follow_up` + `resend_note` (`_resend` :789). Old-session queue still dropped. Enforcement unchanged (`active_commands`).
- (5) Manager new session — done. `manager_recovery` (first in queue) with Task/worker/terminal summary, `reports_resent`, `reports_unknown`, unknown list without content. Never-submitted worker→manager entries follow the new session (`requeue_for_new_session` flow.py:871, lane-level `_requeue` :1162 and at `_create`), ≤ `REQUEUE_LIMIT`=5; worker-bound messages still dropped.
- (6) `workbench_status` — done. service.py:1145; task (message, analysis, commands, commands_run+notes), worker (state, omp idle/busy via 1 s probe, session/generation, restarts with reasons), terminal (`watch_state`), reports pending/deferred(reason, blockers)/unknown with text ≤2000 chars ×10; measured < 3 s in tests.
- (7) Skill — done. `omp_bridge/skills/workbench-recovery/SKILL.md`, manager only (`ROLE_SKILL_PATTERNS` launcher.py:136); to-worker skill, to_worker description, to-manager skill (status_check, resent Task) updated; README updated. Frontmatter investigation: installed OMP is 18.7.0 (18.6.1 not available locally). Binary source shows `hide`/`disableModelInvocation` hide a skill from the *model*; slash commands are registered for every skill when `skills.enableSkillCommands !== false`. No per-skill "hide from slash menu, keep model-visible" key → not used; skill description says "Agent-only".

## Design choices
- Watchdog in its own module/thread (flow_recovery.py) with ports; only work Tasks are checked (experiment runs keep the CW-18 U3 60 s review). A new worker session suspends checks until it got the Task.
- `restart_worker` runs on the backend loop as a non-blocking state machine (the loop owns the pane table; a tool thread replacing panes could close fds under select). Bridge-thread code never calls `poll()` (no reaping off the loop).
- Re-send to a new session: QUESTION reply-able follow-up; if the run's TASK message was never created, the re-send is the TASK message itself (reports must reply to a TASK). Experiment follow-ups are not re-sent.
- Re-queue also on `BridgeBoundMismatch` (mailbox: session changed before submission).
- `worker_stalled` only once per Task until the worker itself calls terminal/to_manager (a manager follow-up does not reset it), as specified.

## Changed paths (sha256, first 12)
bridge.ts ccf4b19282b7 · skills/README.md 704c2d266be4 · to-manager/SKILL.md 14482c17fdb1 · to-worker/SKILL.md 081a28ba4475 · workbench-recovery/SKILL.md e8e9d0ad0150 (new) · flow.py e4277417965b · flow_recovery.py 4ea5932f16ca (new) · flow_tasks.py 82b1c0b506be · flow_terminal.py 3faadc51da6a · launcher.py 146486cf5139 · panes.py 5e5f95da82bb · service.py c1aac923589c · contracts/ui_v1.py 234c50d1462a · bridge_g3/mailbox.py 6296c87fd5d9 · ui/product/model.py 1cbff65f9a5c · tests: test_cw18_fixes.py 640cce65047f · test_paths_launcher.py 418b079f9fb3 · test_workbench_skills.py 5b4cd8e15a0b · handoff_tools.test.ts b6ab777045df · new test_cd70_handoff_recovery.py 81c7f0148985 · test_cd70_restart_worker.py c029ace442e7 · test_cd70_task_resend.py a019fe4620f1 · test_cd70_watchdog.py b3a34cf78625 · tests/ui/test_product_model_cd70.py dc9a76fedb58

## Independent tests needing updates (not edited)
All assert the pre-C-D70 manager tool list / skill set:
- tests/bridge/bridge_terminal_independent_p27cd68.test.ts:89 (manager tools == [to_worker]; now + restart_worker, workbench_status)
- tests/bridge/test_cw18_schema_independent_p27w.py:61 test_bridge_tools_per_role; :121 test_front_matter_and_directories (skill dirs)
- tests/backend/test_omp_isolation_independent_p27m.py:368, :392, :678 and test_workbench_skills_directory_holds_exactly_the_two_role_skills
- tests/backend/test_omp_isolation_independent_p27n.py:201 (manager includeSkills)

## Open questions
1. Slash-menu hiding is not possible in OMP 18.7.0 per skill; accept the visible `/skill:workbench-recovery`, or turn `skills.enableSkillCommands` off for the manager (hides to-worker too)?
2. Should a manager follow-up delivered to an idle worker re-arm the status checks / allow a second `worker_stalled` (spec: only worker terminal/to_manager resets)?
3. `terminal_done` for a command started by the old worker session goes to the new worker session (unchanged C-D68 behaviour); keep, or route it to the manager after a restart?
4. Live verification with a real OMP (`restart_worker` against real OMP SIGTERM handling, real notice injection into the manager) not done (no provider requests allowed).
