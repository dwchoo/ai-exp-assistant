"""Independent tests for the C-D69 corrections in the backend and the skills (p27-cd69-test-02).

Expectations come from DECISIONS.md C-D69 (2) and the review findings (p27-cd69-review-01 P2-1/P3), not from the
implementation:
- the manager's ``to_worker`` message (up to 8192 characters) is the executable procedure; the FIRST TASK the worker
  receives carries all of it, not the 1024-character status summary; the status views keep the short summary; a
  persisted record from before this change (no full message) falls back to the summary instead of failing;
- ``to_manager``/``to_worker`` ``message`` holds 8192 characters, one more is rejected; the skills say so, tell the
  worker how to split a longer report and the manager that a long procedure is fine.
No OMP, no provider, no credential: temp dirs under /tmp only.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))

from test_task_flow import FlowFixture, wait_until  # noqa: E402
from workbench.backend import flow  # noqa: E402
from workbench.contracts.v1 import MessageKind  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SKILLS = REPO / "omp_bridge" / "skills"
LIMIT = 8192  # the C-D69 / flow contract limit of one message
SPEC = {"goal": "hardware facts", "paths": ["notes/"]}


def procedure(size: int, mark: str = "p") -> str:
    out, i = "", 0
    while len(out) < size:
        i += 1
        out += f"{mark}{i}: run `cmd-{i} --flag 한글` and record the exit code and the first line\n"
    return out[:size]


class FirstTaskCarriesTheWholeMessageTests(FlowFixture):
    def dispatch(self, message, **extra):
        result = self.to_worker({"kind": "work", "message": message, "spec": dict(SPEC), **extra})
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(wait_until(lambda: len(self.mailbox.created) == 1), "no TASK message was created")
        created = self.mailbox.created[0]
        self.assertIs(created.kind, MessageKind.TASK)
        return result, created.payload

    def test_every_length_up_to_the_limit_arrives_whole(self):
        for size in (1, 100, 1023, 1024, 1025, 2048, 4096, 8191, LIMIT):
            with self.subTest(size=size):
                self.mailbox.created.clear()
                text = procedure(size)
                self.assertEqual(len(text), size)
                result, payload = self.dispatch(text)
                self.assertEqual(payload["message"], text, "the worker received a different procedure")
                self.assertEqual(len(payload["message"]), size)
                self.assertEqual(payload["task_id"], result["task_id"])
                self.assertEqual(payload["goal"], SPEC["goal"])
                view = self.flow.task_view()
                self.assertEqual(view["summary"], text[:1024], "status keeps the short summary only")
                self.assertLessEqual(len(view["summary"]), 1024)
                self.to_worker({"kind": "work", "message": "cancel", "task_id": result["task_id"], "cancel": True})
                self.assertTrue(wait_until(lambda: self.flow.active_task() is None, 10))

    def test_the_tail_of_a_long_procedure_is_not_lost_and_the_json_payload_shows_it(self):
        text = procedure(LIMIT - 40) + "\nSTOP CONDITION: LAST-LINE-SENTINEL"
        _, payload = self.dispatch(text)
        self.assertTrue(payload["message"].endswith("LAST-LINE-SENTINEL"))
        self.assertIn("LAST-LINE-SENTINEL", json.dumps(payload, ensure_ascii=False))  # the bridge hands it as JSON

    def test_a_long_message_with_the_detailed_level_and_instructions_keeps_all_of_them(self):
        text = procedure(5000)
        _, payload = self.dispatch(text, analysis="detailed")
        self.assertEqual(payload["message"], text)
        self.assertEqual(payload["analysis"], "detailed")
        self.assertTrue(payload.get("analysis_rule"))

    def test_the_status_views_never_carry_the_full_message(self):
        text = procedure(LIMIT)
        self.dispatch(text)
        busy = self.new_work("another")
        self.assertEqual(busy["status"], "worker_busy")
        self.assertEqual(busy["task"]["summary"], text[:1024])
        self.assertNotIn(text[1100:1200], json.dumps(busy))
        self.assertNotIn(text[1100:1200], json.dumps(self.flow.task_view()))

    def test_one_character_over_the_limit_is_rejected_and_nothing_is_sent(self):
        result = self.to_worker({"kind": "work", "message": procedure(LIMIT + 1), "spec": dict(SPEC)})
        self.assertEqual((result["status"], result.get("reason")), ("rejected", "invalid_arguments"), result)
        self.assertTrue([e for e in result["errors"] if e.startswith("message")], result)
        self.assertIn(str(LIMIT), " ".join(result["errors"]))
        self.assertEqual(self.mailbox.created, [])
        self.assertIsNone(self.flow.active_task())

    def test_a_follow_up_message_is_delivered_whole_too(self):
        first = procedure(1500, "a")
        result, _ = self.dispatch(first)
        self.assertTrue(wait_until(lambda: (self.flow.task_view() or {}).get("status") == "running"))
        follow = procedure(3000, "b")
        self.to_worker({"kind": "work", "message": follow, "task_id": result["task_id"]})
        self.assertTrue(wait_until(lambda: len(self.mailbox.created) == 2))
        self.assertEqual(self.mailbox.created[1].payload["message"], follow)


class OldRecordFallbackTests(FlowFixture):
    """A Task record from before this change has no full message: its TASK falls back to the summary."""

    def held_dispatch(self, text):
        """Accept a Task while the worker's OMP is not connected: it waits, unsent, for the runner."""
        connected = [False]
        self.flow._omp_idle = lambda role: True if connected[0] else None
        result = self.to_worker({"kind": "work", "message": text, "spec": dict(SPEC)})
        self.assertEqual(result["status"], "dispatched", result)
        task = self.flow.tasks[result["task_id"]]
        self.assertTrue(wait_until(lambda: task.held_reason == "worker_not_connected", 5), task.held_reason)
        self.assertEqual(self.mailbox.created, [], "the TASK was sent while the worker was not connected")
        return result, connected

    def test_a_task_without_a_full_message_sends_its_summary_and_does_not_fail(self):
        for size in (200, 1500):
            with self.subTest(size=size):
                self.mailbox.created.clear()
                text = procedure(size, "old")
                result, connected = self.held_dispatch(text)
                with self.flow._lock:
                    task = self.flow.tasks[result["task_id"]]
                    self.assertEqual(task.message, text, "the new record keeps the full message")
                    task.message = ""  # exactly what a record written by the earlier version loads as
                connected[0] = True
                self.assertTrue(wait_until(lambda: len(self.mailbox.created) == 1, 10), "the TASK was never sent")
                payload = self.mailbox.created[0].payload
                self.assertEqual(payload["message"], text[:1024])
                self.assertTrue(payload["message"])
                self.assertEqual(payload["task_id"], result["task_id"])
                self.to_worker({"kind": "work", "message": "cancel", "task_id": result["task_id"], "cancel": True})
                self.assertTrue(wait_until(lambda: self.flow.active_task() is None, 10))

    def test_a_new_task_held_for_a_while_then_sent_still_carries_the_full_message(self):
        text = procedure(LIMIT)
        result, connected = self.held_dispatch(text)
        connected[0] = True
        self.assertTrue(wait_until(lambda: len(self.mailbox.created) == 1, 10), "the TASK was never sent")
        self.assertEqual(self.mailbox.created[0].payload["message"], text)

    def test_the_new_record_roundtrips_and_an_old_one_loads_without_a_message_key(self):
        text = procedure(3000)
        result = self.to_worker({"kind": "work", "message": text, "spec": dict(SPEC)})
        self.assertEqual(result["status"], "dispatched", result)
        self.assertTrue(wait_until(lambda: len(self.mailbox.created) == 1))
        records = [r for r in self.ledger() if r.get("type") == "task" and r["task"]["task_id"] == result["task_id"]]
        self.assertTrue(records)
        self.assertTrue(all(r["task"].get("message") == text for r in records), "the full message is persisted")
        self.flow.close()
        self.service.close()
        path = self.root / "workflow" / "tasks-flow.jsonl"
        stripped = []
        for line in path.read_text().splitlines():
            if line.strip():
                record = json.loads(line)
                if record.get("type") == "task":
                    record["task"].pop("message", None)
                stripped.append(json.dumps(record))
        path.write_text("\n".join(stripped) + "\n")
        reopened = self.open(start=True)  # an earlier-version ledger: loads, nothing resent, no crash
        loaded = reopened.tasks[result["task_id"]]
        self.assertEqual(loaded.message, "")
        self.assertEqual(loaded.summary, text[:1024])
        self.assertEqual(len(self.mailbox.created), 1, "a restart resent the TASK")


class MessageLimitContractTests(unittest.TestCase):
    def test_the_to_manager_message_limit_is_8192_characters(self):
        base = {"kind": "done", "task_id": "0b9c7c4e-6c1e-4a51-9b1a-3f7c2d1e0a11"}
        ok = flow.validate_arguments("to_manager", {**base, "message": "x" * LIMIT})
        too_long = flow.validate_arguments("to_manager", {**base, "message": "x" * (LIMIT + 1)})
        self.assertFalse([e for e in ok if e.startswith("message")], ok)
        self.assertTrue([e for e in too_long if e.startswith("message")], too_long)

    def test_the_to_worker_message_limit_is_8192_characters(self):
        args = {"kind": "work", "spec": dict(SPEC)}
        self.assertEqual(flow.validate_arguments("to_worker", {**args, "message": "x" * LIMIT}), [])
        self.assertTrue(flow.validate_arguments("to_worker", {**args, "message": "x" * (LIMIT + 1)}))


def skill(name: str) -> str:
    return " ".join((SKILLS / name / "SKILL.md").read_text().split())


class SkillTextTests(unittest.TestCase):
    def test_to_manager_skill_states_the_limit_and_how_to_split(self):
        text = skill("to-manager")
        self.assertRegex(text, r"\b8192\b")
        self.assertRegex(text, re.compile(r"(longer|more)[^.]*\b(rejected|refused|fails?)\b", re.I),
                         "a longer message is rejected")
        self.assertRegex(text, re.compile(r"\bsplit\b[^.]*\bprogress\b", re.I), "split into progress reports")
        self.assertRegex(text, re.compile(r"\bbefore\b[^.]*\bdone\b|\bfinal\b[^.]*\bdone\b", re.I),
                         "the final report is the done one")
        self.assertRegex(text, re.compile(r"\b(log|file) path\b", re.I), "or point at the log/file path")
        self.assertNotRegex(text, re.compile(r"no length limit\.?\s*$", re.I))
        # C-D69 (4) still holds: no cap on the report itself beyond the per-message transport limit
        self.assertRegex(text, re.compile(r"report itself has no length limit", re.I))

    def test_to_worker_skill_states_the_limit_and_that_a_long_procedure_is_fine(self):
        text = skill("to-worker")
        self.assertRegex(text, r"\b8192\b")
        self.assertRegex(text, re.compile(r"\bmay be long\b|\blong\b[^.]*\b(fine|ok|allowed)\b", re.I))
        self.assertRegex(text, re.compile(r"worker gets all of it|whole|in full", re.I))
        self.assertNotRegex(text, re.compile(r"short and concrete", re.I),
                            "the old advice that cut the procedure to a short message is gone")

    def test_the_skills_state_no_other_limit_than_the_enforced_one(self):
        for name in ("to-manager", "to-worker"):
            for found in re.findall(r"\b(\d{3,6})\s*(?:characters|chars)\b", skill(name)):
                with self.subTest(skill=name, limit=found):
                    self.assertEqual(int(found), LIMIT)


if __name__ == "__main__":
    unittest.main()
