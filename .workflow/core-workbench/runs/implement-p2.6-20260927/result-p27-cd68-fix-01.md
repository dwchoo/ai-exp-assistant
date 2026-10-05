# p27-cd68-fix-01 result (worker_senior, Opus, reused cd68-terminal2) — 2026-10-05

All five review-01 findings fixed:
- P2-1: set_tool_handler(undelivered=...): session replacement / socket close / write timeout -> the terminal call counts as not received; terminal_done sent once.
- P2-2 (measured on OMP 18.6.1, fake local provider): the bridge extension runs anew in every subagent session; ctx.agent = {kind:"sub", depth:1, parentId, name} (main {kind:"main", depth:0}). Bigger defect found: subagent instances sent a worker hello with the same token, replacing the backend worker peer and cutting the main connection. Now subagent sessions do not connect; to_manager/terminal -> subagent_not_allowed. Re-measured explorer + analyst: only the main connection. Missing ctx.agent is treated as main (re-check on future OMP versions).
- P3-3: worker leak = any tool outside WORKER_TOOLS + bridge tools (MCP/browser reported separately as before).
- P3-4: abandon checked before typing and before submit -> aborted, nothing typed, shell returned.
- P3-5: held reason host_terminal_busy:worker_terminal_command; to-worker skill says wait, no takeover request. (UI model.py shows the raw reason; out of scope.)
Checks: red py 3 fail+5 error / bridge py 2 error / node 1 fail -> green: backend flow*/automation*/task_flow/launcher*/omp_home*/skills OK; tests/bridge py OK; node bridge+g3_omp 64 pass; tests/workflow intermittent 1 error (WorkflowHeld: parent shell cwd could not be verified, different test each time, under concurrent load; untouched code) -> CW-16 flake list.
Changed: flow_terminal.py 1c2ab990fda1, flow_tasks.py f3c0e26a503f, service.py 81df4bc3a893, launcher.py 105a24a43106, mailbox.py cb8efa1850ff, bridge.ts c811492faf53, to-manager/SKILL.md 655ae37c6fff, to-worker/SKILL.md e9fcbd3d91de, test_flow_terminal.py bce3f1abe8e2, test_flow_terminal_notices.py b72cdaa2a4aa, test_workbench_skills.py 2585f654c1fa, test_launcher_models.py 31faf276e286, tests/bridge/test_tool_request_bridge.py ab0925b50165, handoff_tools.test.ts 112d09cd6e21.
Evidence: /tmp/cd68fix-probe (probe scripts, probe.log, instrumented bridge copy).
