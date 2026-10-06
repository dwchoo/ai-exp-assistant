# p27-cd69-review-02 (delta reviewer, fresh) — 2026-10-06 — verdict: APPROVE (no P1/P2)

review-01 #1 fixed (FlowTask.message, first TASK `message or summary`, old records load with "" and fall back); #2 fixed by diff reading (OSC 52 bodies excluded from catch-up; 300 KB/1 MiB delivered; unseen-start bodies skipped); #3 fixed (8192 limit + progress split in skill); test-01 _out append fixed.
Runaway check (scratch sim): never-terminated `ESC]52;c;` + 40 MiB -> body abandoned at STRING_MAX (8 MiB), rest counted as normal output, catch-up engages; stream buffer flushed at clipboard_max (~1.4 MB). Body processing ~250 MB/s (24 MiB backlog in 0.09 s).
P3 model.py count/clip_bytes unbounded: repeated `ESC]52;c;`+8 MiB+BEL could accumulate body backlog faster than consumed (24 MiB in sim without catch-up); fix: cap clip_bytes (e.g. 2x STRING_MAX) then drop/merge into catch-up.
P3 flow_tasks.py:529 proceed `instruction` and :652 follow-up records still truncated to 1024 (audit only, not sent to the worker) -> document.
Tests: test_product_omp_copy* 52 failed 8 only on the first run, then 3x OK (PTY flake, details not captured); test_vt_osc52* 27 OK; task_flow + skills 65 OK.
