# p27-cd68-o1-fix-03 result (worker_senior, Opus, reused cd68-terminal2) — 2026-10-06

_align is a loop: on ST it skips that and directly following STs; b"x"+b"\x1b\\"*200000 and the 256 KiB catch-up path return a clean tail without exceptions. A leading `\` is dropped only when the byte before the cut is ESC (half-cut ST); _safe_tail and catch-up pass that byte; callers without it keep the old behaviour.
Checks: red 3 new tests (RecursionError 2 error + 1 error + 1 fail) -> tests/ui vt_passthrough* + product_model* incl. p27o1 independent 330 OK; tests/terminal vt_string_sequences* incl. p27o1 59 OK.
Changed: src/workbench/ui/product/model.py 3cb68f2fbdda, tests/ui/test_product_vt_passthrough.py 3b4ada641716.
