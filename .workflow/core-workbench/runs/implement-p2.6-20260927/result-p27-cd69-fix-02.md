# p27-cd69-fix-02 result (worker_senior, Opus, reused cd69-fix) — 2026-10-06

model.py CLIP_BACKLOG_MAX = 2 x STRING_MAX per pane: live body bytes above it count toward catch-up like normal output; catch-up dropping them shows "복사 건너뜀"; 300 KB / 1 MiB copies unchanged. Red->green: 4 x (ESC]52;c; + 8 MiB + BEL): before 32 MiB body backlog without catch-up; after clip_bytes <= 16 MiB, catch-up, remaining backlog <= 64 KiB, no base64, next copy ok.
Checks (exit 0): ui test_product_omp_copy* 53, test_product_model* 311, ui full 815; terminal test_vt_osc52* 36; backend *independent_p27cd69* 30; node bridge_message_limit_p27cd69b + bridge_analysis_p27cd69 5. Independent tests untouched.
Changed: ui/product/model.py bdba8e541f1e, tests/ui/test_product_omp_copy.py b5b0968f8c31 (screen.py unchanged 35de47288a24).
