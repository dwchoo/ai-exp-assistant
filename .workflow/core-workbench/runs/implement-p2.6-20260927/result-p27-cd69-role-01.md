# p27-cd69-role-01 result (wb-worker, Sonnet) — 2026-10-06 — candidate_ready

- `analysis` (nullable "summary"|"detailed") in to_worker schema + normalize/validate; null/empty -> summary; invalid or on experiment -> field-named reject; stored in spec/scope.
- Worker TASK payload always carries `analysis` and `analysis_rule` ("Analysis: summary - …"), also on work follow-ups.
- Role boundary text in to_worker description/schema and both SKILL.md (procedure delegation with good/bad hardware example; worker executor rules; no length cap). Skill size limit in test 9000 -> 10000.
- analyst removed: omp_bridge/agents/analyst.md deleted, WORKBENCH_AGENT_ROLES updated, omp_home RETIRED_AGENT_FILES removes it from existing homes. Worker model table unchanged.
- Root-allowed follow-up: tests/backend/test_task_flow.py:319 payload assertion + ANALYSIS_RULES import (582279ecf758): 25 OK.
Checks: backend test_flow* 63 OK, test_launcher* 41, skills 37, omp_home_agents 6, paths_launcher 18, cw18_smoke_fixes 22, bridge py 52 (1 skip), node handoff_tools 15. Red not run (tests written after code).
Independent tests pinning analyst (28 fails): p27n 12, p27m 10, models_p27cd68 4, manager_rule_p27cd68 1, omp_home_p27u 1; live_omp_tools_independent_p27cd68 L17/285/306 (not run).
Changed: bridge.ts 6a393f61752e, to-manager/SKILL.md 78376c19be5e, to-worker/SKILL.md 1da9572986f1, agents/analyst.md deleted, flow.py 89e5c23280f8, flow_tasks.py 1c9da75148d4, launcher.py 015502b4d9d9, omp_home.py d0ee02a87e04, test_flow_analysis.py (new) e2cdb1245e9f, test_launcher_models.py f7186ecb4920, test_omp_home_agents.py d3d57cc5c574, test_workbench_skills.py 61d7bd529cdf, handoff_tools.test.ts 34ea89b15586, test_task_flow.py 582279ecf758.
