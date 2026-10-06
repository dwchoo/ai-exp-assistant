# p27-cd68-o1-test-02 (wb-test-rerun) — 2026-10-06 — candidate_ready

All first-run green, no flakes: tests/terminal 167 OK (skip 2, xfail 0); tests/ui 759 OK; gates/g1_vt 85 OK; backend *pane* 47 OK; test_restart_pane* 25 OK. Hashes identical (screen.py 0d015dc977b8, model.py 0eb9f3c13de8).
Unpinned 3 edges (CAN, ESC ESC P, replay tail) to positive asserts; fuzz (600) + exhaustive short sequences (with splits) against an independent reference state machine for the new rules (bytes of _drop_strings and rendered cells); 8 MiB cap boundary; P2 tests: lost ST does not blank, RIS after abort, session restart mid-string (three panes), catch-up cut inside a large notification hidden.
No implementation defect. pyte quirk: a read boundary right after CAN/SUB changes rendering (unrelated to the filter) -> rendering comparisons use single reads.
Changed: tests/terminal/test_vt_string_sequences_independent_p27o1.py f8bebc6c0d5e, tests/ui/test_product_vt_passthrough_independent_p27o1.py 0749ccd77eb3.
