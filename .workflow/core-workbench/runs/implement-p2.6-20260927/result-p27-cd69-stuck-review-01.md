# p27-cd69-stuck-review-01 (reviewer, Opus, fresh) — 2026-10-06 — verdict: APPROVE (P3 x3; Root: fix before integration)

OK: G2 invariants (size check before begin, lifecycle unchanged, request_id None; replay blocked by adapter._seen; pre-existing sent_id/_approval_hash set before reject, harmless). terminal precheck byte-identical to dispatch (same argv/wrapper, executable, 36-char uuid, start_timeout, ensure_ascii JSON). C-D58 user handoff unchanged; experiment idle_wait only when Workbench held and typed; double hold release filtered by reason; replace/kill during return -> "unconfirmed".
P3-1 flow.py:386 experiment size check assumes a 64-char shell path; longer paths (e.g. 76-char nix store) undercount -> commands within 32 B of the limit pass creation and fail at start (shell still returned). Fix: larger margin or recheck with the real path right before start.
P3-2 flow_terminal.py:1001-1012, flow_tasks.py:1209-1228: wb-handoff typed but control_wait not reached within 3 s -> claim fails; _give_back snapshot not yet control_wait -> no takeover; shell then sits in control_wait (owner user). Text says "unconfirmed" truthfully but (5)(a) "always returns" not met. Fix: while polling _returned_state, on control_wait with request_id None call request_takeover once more.
P3-3 bridge.ts:370 + to-manager SKILL "about 2,900 plain ASCII" too high: real limit /usr/bin/bash 2,812, /bin/sh 2,830 -> "about 2,800".
Note: run_start_failed does not state whether the shell was returned.
Ran test_oversize_request_return, test_flow_terminal, test_task_flow: 57 OK.
