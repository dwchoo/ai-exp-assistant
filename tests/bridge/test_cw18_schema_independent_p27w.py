"""CW-18 independent verification (p27-cw18-test-01): skills <-> bridge tool schemas <-> backend validator.

The bridge extension's registered tools (dumped by ``cw18_schema_dump_independent_p27w.ts`` under Node, no
socket) must match what the backend accepts (``workbench.backend.flow.validate_arguments``), and the
Workbench skills must describe exactly those tools (C-D64/C-D65 (3)/C-D66 (2)):

- manager registers only ``to_worker``; worker registers ``to_manager`` and, since C-D68 (1), ``terminal`` (its only
  command execution path; the manager keeps OMP's own tools and gets no ``terminal``, C-D68 (3));
- property names, kinds, required fields, limits and the variable-name rule agree on both sides;
- the to-worker skill names every to_worker field and the one-task rule (worker_busy, no queue), the idle-only
  host terminal, the retry limit, no environment values; it also explains the results the backend really
  returns (incl. ``held`` while paused); the to-manager skill names every to_manager field and the
  staged-reply marker the bridge enforces; no skill mentions the removed approval flow.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from workbench.backend import flow

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "omp_bridge" / "skills"


def dump() -> dict:
    result = subprocess.run(["node", "--experimental-strip-types", "--no-warnings",
                             str(ROOT / "tests/bridge/cw18_schema_dump_independent_p27w.ts")],
                            cwd=ROOT, capture_output=True, text=True, timeout=60, check=True)
    return json.loads(result.stdout)


def skill(name: str) -> tuple[dict, str]:
    text = (SKILLS / name / "SKILL.md").read_text()
    match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    assert match, f"{name}: no front matter"
    front = dict(line.split(": ", 1) for line in match.group(1).splitlines() if ": " in line)
    return front, match.group(2)


@unittest.skipUnless(shutil.which("node"), "node is required to load the bridge extension")
class SchemaAgreementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = dump()

    def tool(self, role, name=None):
        # C-D68 (1): the worker has two bridge tools (to_manager, terminal); the handoff tool is the first one.
        name = name or {"manager": "to_worker", "worker": "to_manager"}[role]
        found = [t for t in self.tools[role] if t["name"] == name]
        self.assertEqual(len(found), 1, self.tools[role])
        return found[0]

    def test_bridge_tools_per_role(self):
        # C-D64 (3) handoff tools; C-D68 (1) adds the worker-only ``terminal`` (no manager terminal, C-D68 (3)).
        self.assertEqual([t["name"] for t in self.tools["manager"]], ["to_worker"])
        self.assertEqual(sorted(t["name"] for t in self.tools["worker"]), ["terminal", "to_manager"])
        self.assertEqual({name: role.value for name, role in flow.TOOL_ROLES.items()},
                         {"to_worker": "manager", "to_manager": "worker"})

    def test_property_names_kinds_and_required_agree_with_the_backend(self):
        to_worker = self.tool("manager")["parameters"]
        to_manager = self.tool("worker")["parameters"]
        self.assertEqual(set(to_worker["properties"]), set(flow._TO_WORKER_KEYS))
        self.assertEqual(set(to_manager["properties"]), set(flow._TO_MANAGER_KEYS))
        self.assertEqual(tuple(to_worker["properties"]["kind"]["enum"]), flow.TO_WORKER_KINDS)
        self.assertEqual(tuple(to_manager["properties"]["kind"]["enum"]), flow.TO_MANAGER_KINDS)
        for schema in (to_worker, to_manager):
            self.assertEqual(sorted(schema["required"]), ["kind", "message"])
            self.assertIs(schema["additionalProperties"], False)
            self.assertEqual(schema["properties"]["message"]["maxLength"], flow.MESSAGE_MAX)
        spec = to_worker["properties"]["spec"]
        self.assertEqual(set(spec["properties"]), set(flow._SPEC_KEYS))
        self.assertEqual(spec["properties"]["paths"]["maxItems"], flow.LIST_MAX)
        self.assertEqual(spec["properties"]["paths"]["items"]["maxLength"], flow.SHORT_MAX)
        execution = spec["properties"]["execution"]
        self.assertEqual(set(execution["properties"]), set(flow._EXECUTION_KEYS))
        self.assertEqual(set(execution["required"]), set(flow._EXECUTION_KEYS))
        self.assertEqual(set(execution["properties"]["criteria"]["properties"]), set(flow._CRITERIA_KEYS))
        self.assertEqual(execution["properties"]["shell"]["enum"], ["bash", "sh"])
        env_pattern = execution["properties"]["environment"]["items"]["pattern"]
        for name in ("PATH", "_x1", "A", "API_TOKEN"):
            self.assertTrue(re.fullmatch(env_pattern, name) and flow.ENV_NAME.fullmatch(name), name)
        for value in ("PATH=/bin", "1A", "A B", "$HOME", ""):
            self.assertFalse(re.fullmatch(env_pattern, value) or flow.ENV_NAME.fullmatch(value), value)
        request = to_manager["properties"]["request"]
        self.assertEqual(sorted(request["required"]), ["goal", "paths"])
        self.assertIs(request["additionalProperties"], False)

    def test_examples_valid_for_the_extension_schema_are_accepted_by_the_backend(self):
        task_id = "6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b"
        self.assertRegex(task_id, self.tool("manager")["parameters"]["properties"]["task_id"]["pattern"])
        execution = {"source": "/src", "commit": "c" * 40, "command": ["./run.sh", "--fast"],
                     "criteria": {"log_contains": "OK", "result_file": "r.txt", "result_contains": "OK"},
                     "environment": ["PATH", "DATA_DIR"], "shell": "sh"}
        good_worker = [
            {"kind": "work", "message": "m", "spec": {"goal": "g", "paths": ["a/"], "instructions": "i"}},
            {"kind": "experiment", "message": "m", "spec": {"goal": "g", "paths": [], "execution": execution}},
            {"kind": "work", "message": "follow", "task_id": task_id},
            {"kind": "experiment", "message": "again", "task_id": task_id, "run": True},
            {"kind": "work", "message": "stop", "task_id": task_id, "cancel": True},
        ]
        for args in good_worker:
            self.assertEqual(flow.validate_arguments("to_worker", args), [], args)
        good_manager = [
            {"kind": kind, "message": "m"} for kind in flow.TO_MANAGER_KINDS if kind != "answer"
        ] + [{"kind": "answer", "message": "m", "in_reply_to": task_id},
             {"kind": "blocked", "message": "m", "reason": "r", "requires_code_change": False, "task_id": task_id,
              "request": {"goal": "g", "paths": ["b/"]}}]
        for args in good_manager:
            self.assertEqual(flow.validate_arguments("to_manager", args), [], args)


class SkillContentTests(unittest.TestCase):
    def test_front_matter_and_directories(self):
        self.assertEqual(sorted(p.name for p in SKILLS.iterdir() if (p / "SKILL.md").is_file()),
                         ["to-manager", "to-worker"])
        for name in ("to-worker", "to-manager"):
            front, body = skill(name)
            self.assertEqual(front.get("name"), name)
            self.assertTrue(front.get("description"))
            self.assertLess(len(body.splitlines()), 80, "about one page")
            lowered = body.lower()
            for removed in ("approval_pending", "approve", "order-worker", "order-manager"):
                self.assertNotIn(removed, lowered, f"{name} still mentions {removed}")

    def test_to_worker_names_every_field_and_the_rules(self):
        _, body = skill("to-worker")
        for field in sorted(flow._TO_WORKER_KEYS | flow._SPEC_KEYS | flow._EXECUTION_KEYS | flow._CRITERIA_KEYS):
            self.assertRegex(body, rf"[`.]{field}\b", f"to-worker does not explain {field}")
        for kind in flow.TO_WORKER_KINDS:
            self.assertIn(f"`{kind}`", body)
        for needle in ("worker_busy", "ONE task at a time", "Nothing is queued", "held:host_terminal_busy",
                       "3 re-runs", "environment variable values", "to_manager"):
            self.assertIn(needle, body)

    def test_to_worker_explains_every_result_status_the_backend_returns(self):
        _, body = skill("to-worker")
        # The backend answers: dispatched, queued, cancelled, cancel_requested, worker_busy, rejected and held
        # (held:paused while the user paused automation, held:retry_limit, held:no_active_run).
        for status in ("dispatched", "queued", "cancelled", "cancel_requested", "worker_busy"):
            self.assertIn(f"`{status}`", body)
        self.assertIn("paused", body, "the manager is not told what held:paused means (wait for the user's resume)")

    def test_to_manager_names_every_field_and_the_marker(self):
        _, body = skill("to-manager")
        for field in sorted(flow._TO_MANAGER_KEYS):
            self.assertIn(f"`{field}`", body, f"to-manager does not explain {field}")
        for kind in flow.TO_MANAGER_KINDS:
            self.assertIn(f"`{kind}`", body)
        marker = re.search(r'WORKER_RESPONSE_MARKER = "([^"]+)"', (ROOT / "omp_bridge/g3/bridge.ts").read_text())
        self.assertIsNotNone(marker)
        self.assertIn(f"`{marker.group(1)}`", body)
        self.assertIn("Never call `to_manager` while answering such a message", body)
        self.assertIn("done", body)


if __name__ == "__main__":
    unittest.main()
