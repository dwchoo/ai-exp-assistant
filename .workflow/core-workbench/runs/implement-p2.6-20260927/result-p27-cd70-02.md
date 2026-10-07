# p27-cd70-02 result (worker_senior, Opus) — 2026-10-07

Root adjudication of p27-cd70-01 Q1–Q3 applied. No commit, no graphify update, no provider request, temp only under /tmp, no independent test edited.

## Checks (final code; same commands as p27-cd70-01, real exit codes)

| suite | result | exit | failures |
|---|---|---|---|
| tests/backend | 995 run (skip 30, xfail 1) | 1 | 5, all independent (same as cd70-01: p27m ×4, p27n ×1) |
| tests/bridge (py) | 52 run (skip 1) | 1 | 2, independent (test_cw18_schema_independent_p27w :61, :121) |
| node tests/bridge/*.test.ts | pass 38 / fail 1 | 1 | independent (bridge_terminal_independent_p27cd68.test.ts:89) |
| tests/terminal | 208 run (skip 2) | 1 | 1 transient: test_review_fixes_independent_p27c DefaultPipeXfsz (sh) "bg blocks SIGPIPE/SIGXFSZ" (SigBlk sampled during parallel load); file re-run alone 3× → OK (2 tests each), exit 0; passed in both cd70-01 full runs; code under test (shell signal dispositions) not touched |
| tests/workflow / gates/g2_shell / contracts / ui | 58 / 142 / 52 / 819 OK | 0 | — |
| new test_cd70_followups.py (9) + WiringTests (1) + node handoff_tools (18) | green | 0 | — |

Red: same tests against a /tmp copy with the Q2/Q3 logic neutralised (= cd70-01 behaviour): 5 failures + 1 error; node 1 fail. Green on the final code.

## Item status
- Q1 — no change (decision recorded). `workbench-recovery` stays visible in the slash menu; description already says "Agent-only" and names the Workbench notices. OMP 18.7.0 has no menu-only hide key (`hide`/`disableModelInvocation` hide from the model; `skills.enableSkillCommands` is all-or-nothing).
- Q2 — done. `TaskFlow.follow_up_submitted` hook (flow_tasks.py:381) called once per manager follow-up (plain or re-sent Task) when the worker OMP accepted it (`_follow_up_listener` :886, events submitted/delivered; rejected/unknown before acceptance → not called). Backend wires it to `Watchdog.worker_acted("manager_follow_up")` (service.py:272): count 0 and `worker_stalled` re-armed; journaled `watchdog_reset` with tool `manager_follow_up`.
- Q3 — done. `TerminalService` gets `notify_manager` + `done_target` (flow_terminal.py:877, `_target` :908). Per attempt the backend rule `_done_target` (service.py:953): command started by the current worker session, or no worker connected → worker (as before); started by another session and its Task not in the current session (`TaskFlow.task_session` :554; also a command without Task) → manager notice `worker_terminal_done` {command_id, short command, task_id, status, exit_code, signal, log_path, duration, instruction "information… do not run it again"}, no output. A deferred manager attempt that becomes worker-routed keeps the same notice_id (sent once). The re-sent Task's `commands_already_run` comes from the terminal journal and includes it (test). Bridge accepts `worker_terminal_done` for the manager only (bridge.ts NOTICE_TYPES); added to `MANAGER_NOTICE_TYPES` and the recovery skill.

## Changed paths (sha256, first 12)
bridge.ts 0fcde171a1be · skills/workbench-recovery/SKILL.md ba59f865ea5d · flow_recovery.py 8208b8058437 · flow_tasks.py f9597f2ba458 · flow_terminal.py e9377c25d5ab · service.py a938e2e34011 · tests/backend/test_cd70_followups.py fd3310309ff2 (new) · tests/backend/test_cd70_restart_worker.py 97453e5df44b · tests/bridge/handoff_tools.test.ts 21e43e452208

## Independent tests needing updates
Unchanged from p27-cd70-01 (manager tool list / skill set). No new ones.

## Open questions
None.
