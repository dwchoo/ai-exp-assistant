"""C-D69 (5)(b)(c) independent (p27-cd69-stuck-test-01), updated for C-D69 (6)(c) (p27-cd69-cmds-test-01).

(b) as replaced by C-D69 (6)(c): the worker is told to prefer several short commands, that a command longer than
one host shell request runs from a script file Workbench writes (``<shell> <file>``; no ``command_too_long``
refusal any more), what ``start_failed`` means, and the stated limit matches the real host shell room;
(c) the manager is told to keep procedures concise (key commands, fallbacks, when to stop, values to return).
Reads the files the launcher ships (skills dir, bridge extension).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from workbench.backend import flow_terminal, launcher

SKILLS = Path(launcher.default_skills_dir())
BRIDGE = Path(launcher.default_bridge_extension()).read_text(encoding="utf-8")


def flat(text: str) -> str:
    return " ".join(text.split())


def skill(name: str) -> str:
    return flat((SKILLS / name / "SKILL.md").read_text(encoding="utf-8"))


def terminal_description() -> str:
    start = BRIDGE.index("const TERMINAL_PARAMETERS")
    block = BRIDGE[start:BRIDGE.index("};", start)]
    return flat(re.sub(r'"\s*\+\s*"', "", block))


def stated_room(text: str) -> int:
    match = re.search(r"about ([\d,]+) plain ASCII characters", text)
    assert match, text[:200]
    return int(match.group(1).replace(",", ""))


class WorkerGuidanceTests(unittest.TestCase):
    def test_worker_skill_short_commands_or_script_file(self):
        # C-D69 (6)(c): the harness writes the script file; the worker is no longer told to write one itself
        text = skill("to-manager")
        self.assertRegex(text, r"(?i)several short `?terminal`? commands")
        self.assertRegex(text, r"(?i)longer than one host shell request")
        self.assertRegex(text, r"(?i)written by Workbench to a script file and run as `<shell> <file>`")
        self.assertRegex(text, r"(?i)shows its first line and the script path")

    def test_worker_skill_explains_refusals_and_start_failed(self):
        text = skill("to-manager")
        self.assertNotIn("command_too_long", text, "C-D69 (6)(c) replaced the oversize refusal")
        self.assertRegex(text, r"`not_in_task_commands`: nothing ran")
        self.assertRegex(text, r"`start_failed`: nothing ran; its detail says whether the host terminal is the "
                               r"user's again; if not, report `blocked`")

    def test_terminal_tool_description(self):
        text = terminal_description()
        self.assertNotIn("command_too_long", text)
        self.assertRegex(text, r"(?i)runs from a script file Workbench writes \(<shell> <file>\)")
        self.assertIn("not_in_task_commands", text)
        self.assertRegex(text, r"(?i)non-ASCII")
        validation = " ".join(flow_terminal.validate_terminal_arguments({"command": "x" * 9000}))
        self.assertIn("script file", validation)
        self.assertNotIn("bash <file>", validation)

    def test_stated_limit_matches_the_real_room(self):
        # the number the worker reads must not promise more than the host shell takes (within 5 %)
        for source in (skill("to-manager"), terminal_description()):
            stated = stated_room(source)
            for executable in ("/usr/bin/bash", "/bin/bash", "/bin/sh", "/usr/bin/dash"):
                room = flow_terminal.command_room(executable)
                with self.subTest(executable=executable, stated=stated, room=room):
                    self.assertLessEqual(stated, room * 1.05)
                    self.assertGreaterEqual(stated, room * 0.9)

    def test_worker_skill_has_no_contrary_guidance(self):
        text = skill("to-manager")
        self.assertNotRegex(text, r"(?i)(one|a single) (long|big) (script|command)")
        self.assertNotRegex(text, r"(?i)bash -c '")


class ManagerGuidanceTests(unittest.TestCase):
    def test_manager_skill_concise_procedure(self):
        text = skill("to-worker")
        self.assertRegex(text, r"(?i)as concise as the task needs")
        self.assertRegex(text, r"(?i)no multi-section essays")
        for needle in (r"commands or steps", r"fallbacks", r"when to stop", r"result you need back"):
            self.assertRegex(text, needle)

    def test_to_worker_message_description(self):
        joined = flat(re.sub(r'"\s*\+\s*"', "", BRIDGE))
        start = joined.index("TO_WORKER_PARAMETERS")
        block = joined[start:start + 4000]
        self.assertIn("as concise as the task needs", block)
        for needle in ("key commands", "allowed fallbacks", "when to stop", "the values to return",
                       "no multi-section essays"):
            self.assertIn(needle, block)


if __name__ == "__main__":
    unittest.main()
