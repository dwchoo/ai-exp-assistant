# p27-cd69-copy-01 result (worker_senior, Opus, cd69-copy) — 2026-10-06

Repro: exact OMP bytes `ESC]52;c;<b64>BEL` -> outer b'' before / same OSC 52 after. Root cause: pyte 0.8.2 drops every OSC except titles (0/1/2); the O1 filter drops DCS whole -> pane OSC 52 (plain and tmux-wrapped) vanished.
Fix: TerminalByteStream extracts OSC 52 bodies (plain: pyte gets only `ESC]52;`+BEL; other OSC unchanged); handles OSC 52 inside a `tmux;` DCS, chunk splits, CAN/SUB/ESC abort, size cap, 8 MiB backstop; callback on BEL/ST; nothing reported while stream.quiet (replay/catch-up tail). model wires the callback only for manager/worker panes; `?`, invalid base64, empty ignored; > 1 MiB rejected with a notice; emitted via the drag-copy _copy (selection c, TMUX passthrough). app loop uses take_output after feed_pending too.
Checks (exit 0): tests/terminal test_vt* 72 (incl. p27o1 independent fuzz), g1_vt 85, backend test_panes* 10, tests/ui 777 (incl. private tmux -L PTY); red on HEAD: vt_osc52 244 errors, omp_copy 38 fail + 1 error.
Changed: vt_g1/screen.py cfd1b5e33609, ui/product/model.py 4b68b643234e, ui/product/app.py cc0dfa2367e5, tests/terminal/test_vt_osc52.py (new) 67255d21965c, tests/ui/test_product_omp_copy.py (new) a672d6113cfa.
Note: uv standalone python 3.13 lacks os.pidfd_open (backend/PTY tests need the venv python).
