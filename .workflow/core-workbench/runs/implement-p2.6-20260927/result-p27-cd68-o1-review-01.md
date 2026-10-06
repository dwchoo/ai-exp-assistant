# p27-cd68-o1-review-01 (delta reviewer, fresh) — 2026-10-06 — verdict: approve with P2 (Root: correct before integration)

P2 screen.py:~219-224 CAN/SUB right after ESC inside a string is consumed as an ESC pair, not an abort (`A ESC P x ESC | CAN B` -> _in_string stays True, all later output hidden); ECMA-48: CAN/SUB always abort.
P2 unterminated strings have no bound: lost ST (`ESC P x ESC c`, crashed process) blanks the pane until the next ST/CAN/SUB; RIS swallowed; state reset only when the stream is recreated at catch-up (model.py:599). Memory does not grow. Fix: length cap and/or abort on ESC + non-`\` that is not tmux doubling.
P3 screen.py:~236 outside a string `ESC ESC P ...` passes both ESC as a pair so `P` does not start the string (`APtmux;xB` drawn); real terminals cancel the first ESC.
P3 byte loop ~70 ns/B (5 MB 0.35 s); could use find. 8-bit C1 / U+0090 not filtered (by design).
Verified: chunk splits incl. ESC/P/\ boundaries, tmux ESC ESC doubling, OSC/CSI ST pass-through, UTF-8 safe (only 0x1b inspected), G1 copy/resize/colors untouched. Existing tests not run (no pytest; own scripts only).
