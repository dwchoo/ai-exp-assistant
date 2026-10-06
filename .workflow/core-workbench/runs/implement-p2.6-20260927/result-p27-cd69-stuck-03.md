# p27-cd69-stuck-03 result (worker_senior, Opus, reused cd69-fix) — 2026-10-06

run.py waits within the existing 3 s deadline for a complete newline-terminated first line of the parent-cwd file (_complete_line) before comparing (exact line compare, no strip); different path or unterminated -> WorkflowHeld as before. flow_terminal reads cwd from /proc (no such pattern).
Red->green: test_run.py +2 (fake shell creates an empty file then fills it over polls, whole/4 chars: before both failed "cwd could not be verified"; different/unterminated keeps holding).
Repeats under concurrent load: p27cd69c DashExperimentTests 10/10 (6 OK each), tests/workflow 3/3 (58 OK each); p27cd69c + test_task_flow + test_flow_terminal 70 OK. Before: Dash 2/5 failed.
Changed: workflow/run.py bbf22e64a34c, tests/workflow/test_run.py 24a54b3d3cb5.
