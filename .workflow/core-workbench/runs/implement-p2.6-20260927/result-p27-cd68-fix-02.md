# p27-cd68-fix-02 result (worker_senior, Opus, reused cd68-terminal2) — 2026-10-05

Resolved. OMP 18.6.1 gives no subagent signal at load/registration (pi, pi.runtime, pi.extension, flags identical; no agent). Registration kept; on subagent session_start (ctx.agent.kind === "sub") pi.setActiveTools removes terminal/to_manager. Measured: subagent provider request tools = glob, grep, read, web_search, yield; main worker/manager lists unchanged. Execute-time subagent_not_allowed kept as defence.
Checks: node handoff_tools red 1 fail -> 15 pass; live_omp_tools_independent_p27cd68 13 OK (incl. test_worker_subagent_is_not_offered_terminal); node bridge+g3_omp 64 pass; tests/bridge py OK (skip 1); test_workbench_skills OK.
Changed: omp_bridge/g3/bridge.ts 9086cacd85c5, tests/bridge/handoff_tools.test.ts 85f970af6a64. Evidence /tmp/cd68fix-probe.
