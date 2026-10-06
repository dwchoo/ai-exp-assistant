"""C-D69 (5)(b)(c) independent (p27-cd69-stuck-test-01): the shipped guidance.

(b) the worker is told to use several short commands or a script file (``write`` + ``bash <file>``), what
``command_too_long`` and ``start_failed`` mean, and the stated limit matches the real host shell room;
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
        text = skill("to-manager")
        self.assertRegex(text, r"(?i)short")
        self.assertRegex(text, r"(?i)several short `?terminal`? commands")
        self.assertRegex(text, r"(?i)write it to a file with `write`")
        self.assertIn("bash <file>", text)
        self.assertRegex(text, r"(?i)never put a long (multi-line )?script inline")

    def test_worker_skill_explains_too_long_and_start_failed(self):
        text = skill("to-manager")
        self.assertRegex(text, r"`command_too_long`: nothing ran and nothing was typed")
        self.assertRegex(text, r"`start_failed`: nothing ran; its detail says whether the host terminal is the "
                               r"user's again; if not, report `blocked`")

    def test_terminal_tool_description(self):
        text = terminal_description()
        self.assertIn("command_too_long", text)
        self.assertRegex(text, r"(?i)write it to a file with the write tool and run bash <file>")
        self.assertRegex(text, r"(?i)non-ASCII")

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
