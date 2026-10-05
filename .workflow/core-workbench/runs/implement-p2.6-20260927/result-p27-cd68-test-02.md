# p27-cd68-test-02 result (wb-test-rerun) — 2026-10-05 — candidate_ready

Frozen candidate 39acc4d9 (17 changed src/omp_bridge files identical at start and end; 0 mismatches vs p27-snapshot-cd68-candidate2.json, 1123 files).
backend 726, 1 fail (test_a_foreground_program_refuses, p27cd68 idle matrix) -> module rerun 23 OK; bridge py 51 OK; node 27 pass; contracts 52 OK; workflow 56 OK; terminal 108, 10 fail (manual_input_boundary_independent / manual_input_target, pty/shell wait timeouts) -> rerun 40 OK; g3_omp 37 pass; live_omp_tools_independent_p27cd68 (OMP 18.6.1, fake provider) 13 OK.
Flakes: all 11 pass on module rerun (pty/shell wait timeouts under full-suite load) -> CW-16 flake list.
Coverage: P2-1 test_tool_request_bridge (replaced session undelivered / delivered not), test_flow_terminal_notices:547; P2-2 + test-01 P3 handoff_tools.test.ts:537, live subagent tests; P3-3 test_launcher_models:219/:230, test_models_independent_p27cd68:191; P3-4 test_flow_terminal_notices:393/:406; P3-5 test_workbench_skills:297/:304.
Not done: Root's late request for a live launcher isolation check (dumpTools false-positive check) was not reported -> covered by the real-model smoke start (isolation result recorded there).
