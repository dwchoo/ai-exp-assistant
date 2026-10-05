# p27-cd68-fix-04 result (worker_senior, Opus, reused cd68-terminal2) — 2026-10-06

- C-D68 (10): command=null never waits; returns running (+ not-yet-returned output, elapsed) or the finished result; a returned finished result counts as received (no terminal_done); not a waiting call (checks continue); journal terminal_fetch; bridge wait limit for this call 10 s.
- smoke-02 text: running detail, check text, tool description, skill: end the turn with one short text line (no empty reply), no `wait` tool or repeated terminal calls to wait, no extra report for the same Task after done.
- wait tool finding: not needed by the worker (OMP 18.6.1 `wait` waits for the session's own background jobs/services; task subagent results auto-deliver; worker has no bash so no async jobs) -> candidate to drop from WORKER_TOOLS (launcher allowlist, leak rule, tests).
Checks: red new py 4 fail+1 error / node 2 fail -> green: flow_terminal*+skills 90 x3, backend flow*/automation*/task_flow/handoff* OK, node bridge+g3_omp 64 pass, bridge py OK, live_omp_tools_independent_p27cd68 OK; independent manager_rule+fix03: 1 fail (pinned blocking fetch).
Independent tests to update: test_terminal_fix03_independent_p27cd68.py:185-188 (null call waits as control); test_terminal_independent_p27cd68.py:397,451 comments.
Changed: flow_terminal.py 80365da6ace7, bridge.ts 5149f41cd906, to-manager/SKILL.md 59faea9fd3d6, test_flow_terminal.py c65aa2167db9, test_flow_terminal_notices.py bfbee6bc1e4d, test_workbench_skills.py 046e935bb6a4, handoff_tools.test.ts 0ae6ecc2cc7d.
