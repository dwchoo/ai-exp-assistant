# p27-cd68-fix-05 result (worker_senior, Opus, reused cd68-terminal2) — 2026-10-06

wait dropped from WORKER_TOOLS (worker wait = tool:wait leak); to-manager skill wait mention removed ("Do not wait with repeated terminal calls" kept). Remaining harmless "wait tool" wording in bridge.ts description and flow_terminal running detail (outside paths) -> polish.
Live (OMP 18.6.1, fake provider, throw-away HOME): isolation ok both roles 0 leaks; worker tools read, grep, glob, edit, write, web_search, todo, task, to_manager, terminal (no wait); manager keeps wait; worker provider request has no wait; explorer and analyst ran, yielded results auto-delivered into the next worker request; task result text "Results auto-deliver; NEVER poll."
Checks: red launcher_models/skills 2 fail -> OK; launcher*/omp_home*/omp_isolation/paths_launcher/skills/models_independent 324 OK; live_omp_tools_independent_p27cd68 13 OK. No independent test pins wait.
Changed: launcher.py 5a47997534be, to-manager/SKILL.md fd815661b8fe, test_launcher_models.py 9cbc00b34ed9, test_workbench_skills.py 4538f7681184. Evidence /tmp/cd68fix05.
