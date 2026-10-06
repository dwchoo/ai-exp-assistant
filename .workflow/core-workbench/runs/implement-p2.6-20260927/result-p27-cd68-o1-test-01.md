# p27-cd68-o1-test-01 (wb-test-rerun) — 2026-10-06 — needs_reroute

terminal 153 OK (skip 2, xfail 2); ui 752, 4 fail (new independent tests only); gates/g1_vt 85 OK; backend *pane* 47 + test_panes 10 OK. No flakes. Frozen hash identical (screen.py dc9846fc697d).
P2 (impl): ProductModel._apply resets only the screen on session/generation change (model.py:513-516), not the stream; TerminalByteStream._in_string/_pending_esc (screen.py:196-197) carry over -> after restarting a pane that died mid-string, new session output is hidden until CAN/SUB; a pending ESC can eat the first char (`[31mnew` -> `new`); all three panes. Failing: test_unterminated_string_does_not_blank_a_replaced_session (3 subtests), test_pending_esc_of_a_dead_session_does_not_corrupt_the_next_session.
P3 (pinned expectedFailure): ESC CAN inside a string treated as payload (screen.py:216-223); ESC ESC P not recognised as DCS (screen.py:231-233); replay/catch-up tail cut mid wrapped notification shows `]777;notify…` and `\` (model.py:209-221 _align).
P3 (not pinned): C1 ST U+009C does not end strings; memoryview input AttributeError (operational path uses bytes).
Passed: every byte split + 300 seeded random splits, 6 MiB payload flat memory, 8 MB linear time, Korean UTF-8 splits (11172 syllables), plain-output parity with pyte, OSC 0/2 titles, OSC 52 non-forward, three panes, enqueue_display/feed_pending splits.
New: tests/terminal/test_vt_string_sequences_independent_p27o1.py 4ac34a653d0a, tests/ui/test_product_vt_passthrough_independent_p27o1.py 001ad83d6f86.
Residue note: pid 654231 (/tmp/cw03-g2-d11klsdi) not started by this agent; some old /tmp/cw0* dirs remain.
