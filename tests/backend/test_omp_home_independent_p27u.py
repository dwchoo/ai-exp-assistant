"""Independent C-D64 tests (p27-home-test-01, unit V-CW-17-p2.7-home): the Workbench-owned OMP home.

Expectations were drafted from DECISIONS C-D64 (incl. the appended probe facts and user choices), CW-17
'Workbench 전용 OMP 홈', result-p27-home-probe-01.md and the senior required_behavior before omp_home.py was read:

- E1 env: PI_CONFIG_DIR names ``<data>/omp-root`` relative to HOME (OMP does ``path.join(os.homedir(), PI_CONFIG_DIR)``),
  PI_CODING_AGENT_DIR is ``<data>/omp-root/agent``, BUN_RUNTIME_TRANSPILER_CACHE_PATH lies under the root; OMP_PROFILE,
  PI_PROFILE, the user's own PI_CONFIG_DIR/PI_CODING_AGENT_DIR and the OMP_*_DIR/_DB path overrides never reach OMP;
  XDG_{DATA,STATE,CACHE}_HOME are not passed when ``$XDG_*/omp`` exists (OMP would use it for our layout);
  ``--profile`` is refused; the host shell keeps the user's environment.
- E2 data dir inside/outside HOME and a symlinked HOME resolve to the same physical root or are refused with a clear
  message; a root overlapping the user's OMP directory is refused.
- E3 ``config.yml`` is rewritten at every start (0600, setupVersion 2, startup.setupWizard false, isolation keys,
  Workbench skills dir), also after a user edit or a symlink swap, and never writes through a symlink.
- E4 ``agent.db`` is a symlink to the user's store; created, kept, repaired (wrong target/dangling); a regular
  file/dir/-wal/-shm/-journal is moved aside inside the Workbench agent dir; the user's home is never written.
- E5 the user's store is ``$XDG_DATA_HOME/omp/agent.db`` when ``$XDG_DATA_HOME/omp`` exists, else
  ``~/.omp/agent/agent.db``; lstat only (a regular file of this user).
- E6 no store: ``start`` exits 2 with the login guidance and nothing is created in the user's home.
- E7 a store that vanishes under a running backend: the link is removed at the next home preparation (restart).
- E8 the role overlay never reads the user's OMP config/agents dir.  E9 restart reuses the env/home.
- E10 isolation check: leak when a file of the user's OMP dirs other than agent.db* is held open; warning when the home
  is not observed or no provider/model is usable; link/inode drift around a check is reported.

No real OMP, no model call, no real credential: every HOME is a temp dir under /tmp with a fake ``agent.db``
(non-credential bytes). The live counterpart is ``live_omp_home_independent_p27u.py``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import test_omp_isolation_independent_p27m as p27m  # noqa: E402
import test_restart_pane_independent_p27q as p27q  # noqa: E402
from workbench.backend import launcher, omp_home  # noqa: E402
from workbench.backend.launcher import LaunchPlan, StartRequirementError  # noqa: E402
from workbench.backend.paths import DataDirError, DataLayout, ensure_private_dir  # noqa: E402
from workbench.backend.service import Backend  # noqa: E402
from workbench.contracts.v1 import PaneId  # noqa: E402
from workbench.terminal.shell_g2.prototype import ShellChoice  # noqa: E402

REPO = p27m.REPO
SKILLS_DIR = REPO / "omp_bridge" / "skills"
FAKE = p27m.FAKE_AUTH_BYTES
PATH_OVERRIDES = ("OMP_WORKTREE_DIR", "OMP_GITHUB_CACHE_DB", "OMP_COMMIT_CACHE_DB", "OMP_JUDGMENT_CACHE_DB",
                  "OMP_AUTH_BROKER_SNAPSHOT_CACHE")
HOME_KEYS = p27m.HOME_ENV_KEYS


def inventory(path: Path) -> dict[str, tuple]:
    """lstat-only inventory (type, mode, size, mtime_ns, inode, link target): never opens a file."""
    out: dict[str, tuple] = {}
    if not os.path.lexists(path):
        return out
    for current, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            full = os.path.join(current, name)
            info = os.lstat(full)
            target = os.readlink(full) if stat.S_ISLNK(info.st_mode) else None
            out[os.path.relpath(full, path)] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_size,
                                                info.st_mtime_ns, info.st_ino, target)
    return out


def plan_for(omp: str = "/usr/bin/omp-fake", args=()) -> LaunchPlan:
    return LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), omp, "omp/18.4.5", "/repo/omp_bridge/g3/bridge.ts",
                      tuple(args))


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="p27u-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}

    def store(self, home: Path | None = None) -> Path:
        return p27m.plant_fake_auth_store(home or self.home)

    def prepare(self, data: Path, env: dict | None = None):
        return omp_home.prepare_omp_home(data, env or self.env, skills_dir=SKILLS_DIR,
                                         provider_ids=launcher.ISOLATION_PROVIDER_IDS)


# ------------------------------------------------------------------------------------------- E1 environment
class EnvironmentTests(_Tmp):
    def base(self, **extra) -> dict:
        return {"PATH": "/usr/bin", "HOME": str(self.home), "LANG": "C.UTF-8", "OPENAI_API_KEY": "sk-p27u-sentinel",
                "OMP_PROFILE": "work", "PI_PROFILE": "work", "PI_CONFIG_DIR": ".omp-user", "PI_AUTO_QA": "1",
                "PI_CODING_AGENT_DIR": str(self.home / "user-agent"), "BUN_RUNTIME_TRANSPILER_CACHE_PATH": "/tmp/u",
                **{key: f"/home/user/{key.lower()}" for key in PATH_OVERRIDES}, **extra}

    def test_pane_and_check_env_carry_the_home_and_drop_every_override(self):
        self.store()
        home = self.prepare(self.root / "data")
        values = home.environment()
        self.assertEqual(set(values), set(HOME_KEYS))
        base = self.base()
        frozen = dict(base)
        for role in ("manager", "worker"):
            pane = launcher.omp_environment(base, plan_for(), role=role, token="t", bridge_socket=Path("/x/b.sock"),
                                            home=values)
            check = launcher.isolation_check_environment(base, plan_for(), role=role, absent_socket=Path("/x/a"),
                                                         home=values)
            for env in (pane, check):
                p27m.assert_workbench_home_env(self, env, self.home, self.root / "data")
                for key in ("OMP_PROFILE", "PI_PROFILE", "PI_AUTO_QA", *PATH_OVERRIDES):
                    self.assertNotIn(key, env, f"{role}: {key} reached OMP")
                self.assertEqual(env["OPENAI_API_KEY"], "sk-p27u-sentinel")
                self.assertEqual(env["HOME"], str(self.home), "HOME stays the user's")
        self.assertEqual(base, frozen, "the caller's mapping was edited")

    def test_xdg_dirs_are_withheld_only_when_omp_would_use_them(self):
        self.store()
        values = self.prepare(self.root / "data").environment()
        xdg = {key: str(self.root / key.lower()) for key in omp_home.XDG_DIR_KEYS}
        for key in xdg:
            Path(xdg[key]).mkdir()
        env = launcher.omp_environment(self.base(**xdg), plan_for(), role="worker", token="t",
                                       bridge_socket=Path("/x/b.sock"), home=values)
        self.assertEqual({k: env.get(k) for k in xdg}, xdg, "XDG kept while $XDG_*/omp does not exist")
        (Path(xdg["XDG_DATA_HOME"]) / "omp").mkdir()
        (Path(xdg["XDG_CACHE_HOME"]) / "omp").mkdir()
        env = launcher.omp_environment(self.base(**xdg), plan_for(), role="worker", token="t",
                                       bridge_socket=Path("/x/b.sock"), home=values)
        self.assertNotIn("XDG_DATA_HOME", env)
        self.assertNotIn("XDG_CACHE_HOME", env)
        self.assertEqual(env.get("XDG_STATE_HOME"), xdg["XDG_STATE_HOME"])

    def test_host_shell_keeps_the_users_environment(self):
        base = self.base()
        shell = launcher.shell_environment(base)
        for key in ("OMP_PROFILE", "PI_CONFIG_DIR", "PI_CODING_AGENT_DIR", *PATH_OVERRIDES):
            self.assertEqual(shell.get(key), base[key], f"the host shell lost the user's {key}")

    def test_profile_in_user_args_is_refused_before_anything_starts(self):
        omp = self.root / "omp"
        omp.write_text("#!/bin/sh\necho omp/18.4.5\n")
        omp.chmod(0o755)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home)}
        for args, extra in ((("--profile", "x"), {}), (("--profile=x",), {}), ((), {"WORKBENCH_OMP_ARGS": "--profile x"}),
                            ((), {"WORKBENCH_OMP_ARGS": "--thinking high --profile=work"})):
            with self.subTest(args=args, extra=extra):
                with self.assertRaises(StartRequirementError) as caught:
                    launcher.build_plan({**env, **extra}, omp=str(omp), omp_args=args)
                self.assertIn("--profile", str(caught.exception))
        launcher.build_plan(env, omp=str(omp), omp_args=("--thinking", "high"))  # other args stay allowed

    def test_a_profile_in_the_checked_command_is_a_leak(self):
        h = p27m.Harness(self)
        h.env["HOME"] = str(self.home)
        command = [str(h.script), "--config", "/x/s.yml", "--no-extensions", "--profile", "w"]
        result = launcher.check_isolation(command, cwd=h.root, environment=h.env, role="worker", allowed_skills=(),
                                          omp_version="omp/18.4.5", timeout=15.0)
        self.assertEqual(result["state"], "leak", result)
        self.assertIn("--profile", json.dumps(result["leaks"]))


# ------------------------------------------------------------------------------------- E2 data dir vs HOME
class ConfigDirValueTests(_Tmp):
    def joined(self, value: str, home: Path | None = None) -> str:
        return os.path.normpath(os.path.join(str(home or self.home), value))  # OMP's path.join

    def test_inside_home_is_a_plain_relative_path(self):
        root = self.home / "wb" / "omp-root"
        value = omp_home.config_dir_value(root, self.env)
        self.assertFalse(os.path.isabs(value))
        self.assertFalse(value.startswith(".."), value)
        self.assertEqual(self.joined(value), str(root))

    def test_outside_home_resolves_to_the_same_directory(self):
        root = self.root / "elsewhere" / "data" / "omp-root"
        value = omp_home.config_dir_value(root, self.env)
        self.assertFalse(os.path.isabs(value))
        self.assertEqual(self.joined(value), str(root))

    def test_symlinked_home_resolves_physically_or_is_refused(self):
        real = self.root / "real-home"
        real.mkdir()
        link = self.root / "home-link"
        link.symlink_to(real)
        env = {"HOME": str(link)}
        for root in (real / "data" / "omp-root", link / "data" / "omp-root", self.root / "out" / "omp-root"):
            root.parent.mkdir(parents=True, exist_ok=True)
            with self.subTest(root=str(root)):
                try:
                    value = omp_home.config_dir_value(root, env)
                except omp_home.OmpHomeError as exc:
                    self.assertIn("HOME", str(exc))
                    continue
                self.assertFalse(os.path.isabs(value))
                self.assertEqual(os.path.realpath(self.joined(value, link)), os.path.realpath(root))

    def test_overlap_with_the_users_omp_dir_is_refused(self):
        (self.home / ".omp" / "agent").mkdir(parents=True)
        outside_link = self.root / "sneaky"
        outside_link.symlink_to(self.home / ".omp" / "agent")
        nested_home = self.root / "omp-root" / "nested-home"
        nested_home.mkdir(parents=True)
        cases = [(self.home / ".omp", self.env), (self.home / ".omp" / "agent" / "x", self.env),
                 (outside_link / "d", self.env), (self.root, {"HOME": str(nested_home)})]
        for data, env in cases:
            with self.subTest(data=str(data)):
                with self.assertRaises(omp_home.OmpHomeError) as caught:
                    omp_home.config_dir_value(omp_home.omp_root(data), env)
                self.assertIn("overlaps", str(caught.exception))

    def test_overlap_with_an_xdg_omp_dir_is_refused(self):
        xdg = self.root / "xdg-data"
        (xdg / "omp").mkdir(parents=True)
        with self.assertRaises(omp_home.OmpHomeError):
            omp_home.config_dir_value(omp_home.omp_root(xdg / "omp" / "wb"), {**self.env, "XDG_DATA_HOME": str(xdg)})

    def test_prepare_refuses_overlap_without_touching_the_users_dir(self):
        self.store()
        before = inventory(self.home)
        with self.assertRaises(DataDirError):
            self.prepare(self.home / ".omp" / "wbdata")
        self.assertEqual(inventory(self.home), before)


# -------------------------------------------------------------------------------------------- E3 config.yml
class ConfigFileTests(_Tmp):
    def read(self, home) -> dict:
        text = home.config.read_text()
        body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        return json.loads(body)

    def test_written_private_with_the_required_keys(self):
        self.store()
        home = self.prepare(self.root / "data")
        self.assertEqual(stat.S_IMODE(os.lstat(home.config).st_mode), 0o600)
        self.assertTrue(stat.S_ISREG(os.lstat(home.config).st_mode))
        for directory in (home.root, home.agent_dir):
            self.assertEqual(stat.S_IMODE(os.lstat(directory).st_mode), 0o700, directory)
        data = self.read(home)
        self.assertEqual(data["setupVersion"], 2)
        self.assertIs(data["startup"]["setupWizard"], False)
        self.assertIs(data["dev"]["autoqa"], False)
        self.assertLessEqual(set(p27m.CAPABILITY_IDS), set(data["disabledProviders"]))
        self.assertFalse(set(p27m.MODEL_PROVIDER_IDS) & set(data["disabledProviders"]))
        self.assertEqual(data["skills"]["customDirectories"], [str(SKILLS_DIR)])
        self.assertEqual((data["skills"]["includeSkills"], data["skills"]["ignoredSkills"]), ([], []))
        self.assertEqual(launcher.default_skills_dir(), SKILLS_DIR)

    def test_rewritten_after_a_user_edit_and_a_mode_change(self):
        self.store()
        home = self.prepare(self.root / "data")
        original = home.config.read_bytes()
        home.config.write_text("setupVersion: 1\nstartup:\n  setupWizard: true\ndev:\n  autoqa: true\n")
        home.config.chmod(0o644)
        again = self.prepare(self.root / "data")
        self.assertEqual(again.config.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(os.lstat(again.config).st_mode), 0o600)

    def test_a_symlinked_config_is_replaced_and_its_target_never_written(self):
        self.store()
        home = self.prepare(self.root / "data")
        victim = self.home / "victim.yml"
        victim.write_text("user content p27u\n")
        victim.chmod(0o644)
        before = inventory(self.home)
        home.config.unlink()
        home.config.symlink_to(victim)
        again = self.prepare(self.root / "data")
        self.assertTrue(stat.S_ISREG(os.lstat(again.config).st_mode), "config.yml is still a symlink")
        self.assertEqual(victim.read_text(), "user content p27u\n")
        self.assertEqual(inventory(self.home), before)

    def test_a_symlinked_agent_dir_is_refused_and_the_users_files_are_untouched(self):
        self.store()
        (self.home / ".omp" / "agent" / "config.yml").write_text("user: config\n")
        data = self.root / "data"
        (data / "omp-root").mkdir(parents=True)
        (data / "omp-root" / "agent").symlink_to(self.home / ".omp" / "agent")
        before = inventory(self.home)
        with self.assertRaises(DataDirError):
            self.prepare(data)
        self.assertEqual(inventory(self.home), before)


# ------------------------------------------------------------------------------------ E4 agent.db symlink
class AuthLinkTests(_Tmp):
    def setUp(self):
        super().setUp()
        self.user_store = self.store()
        self.data = self.root / "data"

    def assert_linked(self, home):
        info = os.lstat(home.link)
        self.assertTrue(stat.S_ISLNK(info.st_mode))
        self.assertEqual(os.readlink(home.link), str(self.user_store))
        self.assertTrue(home.linked)
        self.assertTrue(os.path.isabs(os.readlink(home.link)))

    def test_create_then_keep_the_same_link(self):
        before = inventory(self.home)
        home = self.prepare(self.data)
        self.assert_linked(home)
        first = os.lstat(home.link).st_ino
        again = self.prepare(self.data)
        self.assert_linked(again)
        self.assertEqual(os.lstat(again.link).st_ino, first, "a correct link was replaced")
        self.assertEqual(again.moved_aside, ())
        self.assertEqual(inventory(self.home), before)
        self.assertEqual(omp_home.verify_omp_home(again), [])

    def test_wrong_or_dangling_targets_are_repaired_and_the_old_target_is_untouched(self):
        home = self.prepare(self.data)
        other = self.root / "other.db"
        other.write_bytes(b"other p27u\n")
        for target in (other, self.root / "nowhere.db", Path("relative/agent.db")):
            with self.subTest(target=str(target)):
                home.link.unlink()
                home.link.symlink_to(target)
                before = inventory(self.home)
                again = self.prepare(self.data)
                self.assert_linked(again)
                self.assertEqual(other.read_bytes(), b"other p27u\n")
                self.assertEqual(inventory(self.home), before)

    def test_regular_file_directory_and_sqlite_companions_are_moved_aside_inside_the_wb_agent_dir(self):
        home = self.prepare(self.data)
        agent = home.agent_dir
        user_wal = self.home / ".omp" / "agent" / "agent.db-wal"
        user_wal.write_bytes(b"user wal p27u\n")
        before = inventory(self.home)
        for kind in ("file", "dir"):
            with self.subTest(kind=kind):
                (agent / "agent.db").unlink() if os.path.lexists(agent / "agent.db") and not (agent / "agent.db").is_dir() \
                    else None
                if kind == "file":
                    (agent / "agent.db").write_bytes(b"old local store p27u\n")
                else:
                    (agent / "agent.db").mkdir()
                    (agent / "agent.db" / "inner").write_text("x")
                (agent / "agent.db-wal").symlink_to(user_wal)  # a companion pointing at the user's file
                (agent / "agent.db-shm").write_bytes(b"shm")
                (agent / "agent.db-journal").write_bytes(b"journal")
                again = self.prepare(self.data)
                self.assert_linked(again)
                names = sorted(os.listdir(agent))
                for name in ("agent.db-wal", "agent.db-shm", "agent.db-journal"):
                    self.assertNotIn(name, names)
                moved = [n for n in names if ".moved-" in n]
                self.assertTrue(any(n.startswith("agent.db.moved-") for n in moved), names)
                self.assertTrue(set(again.moved_aside) <= set(names), (again.moved_aside, names))
                self.assertTrue(again.notes, "moving aside must be reported")
                self.assertEqual(omp_home.verify_omp_home(again), [])
                self.assertEqual(user_wal.read_bytes(), b"user wal p27u\n")
                self.assertEqual(inventory(self.home), before, "the user's home changed")
                for name in moved:
                    p = agent / name
                    shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()


# ---------------------------------------------------------------------------------- E5 store resolution
class StoreResolutionTests(_Tmp):
    def test_default_location(self):
        self.assertEqual(omp_home.user_auth_store(self.env), self.home / ".omp" / "agent" / "agent.db")

    def test_xdg_data_home_omp_wins_when_it_exists(self):
        xdg = self.root / "xdg"
        env = {**self.env, "XDG_DATA_HOME": str(xdg)}
        self.store()
        self.assertEqual(omp_home.user_auth_store(env), self.home / ".omp" / "agent" / "agent.db")
        (xdg / "omp").mkdir(parents=True)
        self.assertEqual(omp_home.user_auth_store(env), xdg / "omp" / "agent.db")
        (xdg / "omp" / "agent.db").write_bytes(FAKE)
        home = self.prepare(self.root / "data", env)
        self.assertEqual(os.readlink(home.link), str(xdg / "omp" / "agent.db"))

    def test_store_must_be_a_regular_file_of_this_user(self):
        agent = self.home / ".omp" / "agent"
        agent.mkdir(parents=True)
        real = self.root / "real.db"
        real.write_bytes(FAKE)
        for setup, detail in ((lambda: None, "missing"), (lambda: (agent / "agent.db").mkdir(), "regular"),
                              (lambda: (agent / "agent.db").symlink_to(real), "symlink")):
            with self.subTest(detail=detail):
                if os.path.lexists(agent / "agent.db"):
                    p = agent / "agent.db"
                    p.rmdir() if p.is_dir() and not p.is_symlink() else p.unlink()
                setup()
                state = omp_home.auth_store_state(agent / "agent.db")
                self.assertFalse(state.ok)
                self.assertIn(detail, state.detail)
                self.assertIsNotNone(omp_home.auth_guidance(self.env))


# ------------------------------------------------------------------------------- E6/E7 no or vanished store
class NoStoreTests(_Tmp):
    def test_prepare_without_store_creates_nothing_in_the_home_and_carries_the_guidance(self):
        for layout in ("empty-home", "agent-dir-only"):
            with self.subTest(layout=layout):
                if layout == "agent-dir-only":
                    (self.home / ".omp" / "agent").mkdir(parents=True, exist_ok=True)
                before = inventory(self.home)
                home = self.prepare(self.root / f"data-{layout}")
                self.assertFalse(home.linked)
                self.assertFalse(os.path.lexists(home.link))
                self.assertEqual(inventory(self.home), before)
                text = " ".join(home.notes)
                self.assertIn("omp", text)
                self.assertIn("/login", text)
                self.assertIn("API key", text)

    def test_a_vanished_store_removes_the_link_at_the_next_preparation(self):
        store = self.store()
        home = self.prepare(self.root / "data")
        self.assertTrue(home.linked)
        store.unlink()
        self.assertIn("auth_store:missing", omp_home.verify_omp_home(home))
        before = inventory(self.home)
        again = self.prepare(self.root / "data")
        self.assertFalse(again.linked)
        self.assertFalse(os.path.lexists(again.link), "a dangling link would let OMP create the user's store")
        self.assertEqual(inventory(self.home), before)
        self.assertFalse(store.exists(), "the Workbench recreated the user's store")


# ------------------------------------------------------------------------------------- E8 overlay reads
class OverlayTests(_Tmp):
    def test_overlay_in_the_workbench_home_never_reads_the_users_agents_dir(self):
        self.store()
        (self.home / ".omp" / "agent" / "agents").mkdir()
        (self.home / ".omp" / "agent" / "agents" / "u.md").write_text("---\nname: user-agent\ndescription: d\n---\n")
        project = self.root / "proj"
        (project / ".omp" / "agents").mkdir(parents=True)
        (project / ".omp" / "agents" / "p.md").write_text("---\nname: proj-agent\ndescription: d\n---\n")
        values = self.prepare(self.root / "data").environment()
        env = launcher.omp_environment(self.env, plan_for(), role="worker", token="t", bridge_socket=Path("/x"),
                                       home=values)
        overlay = launcher.role_overlay("worker", project_dir=project, home=self.home, environment=env)
        # C-D68: plus the worker's fixed set (OMP's bundled agents); the Workbench explorer installed in the Workbench
        # home (C-D69 (3): no analyst any more) is not ambient and stays usable for the worker, the manager disables it.
        self.assertEqual(p27m.ambient_disabled(overlay, "worker"), ["proj-agent"])
        self.assertFalse(p27m.WORKBENCH_AGENTS & set(overlay["task"]["disabledAgents"]))
        manager = launcher.role_overlay("manager", project_dir=project, home=self.home, environment=env)
        self.assertEqual(p27m.ambient_disabled(manager, "manager"), ["proj-agent"])
        self.assertFalse(p27m.BUNDLED_AGENTS & set(manager["task"]["disabledAgents"]))
        self.assertEqual(launcher.omp_user_dir(self.home, env), self.root / "data" / "omp-root" / "agent")
        files = launcher.ambient_prompt_files(project, env)
        self.assertFalse([f for f in files if str(self.home / ".omp") in f["path"]], files)


# ------------------------------------------------------------------------- E10 isolation-check classification
class ClassificationTests(_Tmp):
    def setUp(self):
        super().setUp()
        self.user_store = self.store()
        self.home_obj = self.prepare(self.root / "data")
        self.check_env = launcher.isolation_check_environment(self.env, plan_for(), role="worker",
                                                              absent_socket=Path("/x/a"),
                                                              home=self.home_obj.environment())
        self.login = {"providers": [{"id": "openai-codex", "authenticated": True}, {"id": "x", "authenticated": False}]}
        self.wb = str(self.home_obj.agent_dir / "models.db")

    def observe(self, paths, login="default", model="openai-codex/gpt"):
        return launcher.home_observation(paths, self.check_env, self.login if login == "default" else login, model)

    def test_clean_home_use_is_ok_and_keeps_provider_ids_only(self):
        observed, leaks, warnings = self.observe([self.wb, str(self.user_store), str(self.user_store) + "-wal",
                                                  str(self.user_store) + "-shm", "/usr/lib/x.so"])
        self.assertEqual((leaks, warnings), ([], []))
        self.assertEqual(observed["authenticated_providers"], ["openai-codex"])
        self.assertTrue(observed["auth_store_open"])
        self.assertTrue(observed["agent_dir_in_use"])

    def test_any_other_file_under_the_users_omp_dir_is_a_leak(self):
        for rel in ("agent/sessions/s.jsonl", "logs/omp.log", "agent/config.yml", "agent/agent.db.bak",
                    "run/daemons/x/clients/1.json", "agent/PERSONALITY.md"):
            with self.subTest(rel=rel):
                path = str(self.home / ".omp" / rel)
                _, leaks, _ = self.observe([self.wb, path])
                self.assertEqual(leaks, [f"user_omp_path:{path}"])

    def test_prefix_neighbours_of_the_users_dir_are_not_leaks(self):
        _, leaks, _ = self.observe([self.wb, str(self.home / ".omp-other" / "x"), str(self.home / ".ompx")])
        self.assertEqual(leaks, [])

    def test_xdg_users_omp_dir_is_watched_too(self):
        xdg = self.root / "xdg"
        (xdg / "omp").mkdir(parents=True)
        (xdg / "omp" / "agent.db").write_bytes(FAKE)
        env = {**self.env, "XDG_DATA_HOME": str(xdg)}
        home = self.prepare(self.root / "data-xdg", env)
        check_env = launcher.isolation_check_environment(env, plan_for(), role="worker", absent_socket=Path("/x/a"),
                                                         home=home.environment())
        leak = str(xdg / "omp" / "sessions" / "s.jsonl")
        observed, leaks, _ = launcher.home_observation([str(home.agent_dir / "models.db"), str(xdg / "omp" / "agent.db"),
                                                        leak], check_env, self.login, "m")
        self.assertIn(f"user_omp_path:{leak}", leaks, "a file of the user's XDG OMP dir is not reported")
        self.assertTrue(observed["auth_store_open"], "the shared XDG store is not recognised")

    def test_home_not_observed_and_no_provider_are_warnings(self):
        _, leaks, warnings = self.observe(["/usr/lib/x.so"])
        self.assertEqual(leaks, [])
        self.assertIn("omp_home:not_observed", warnings)
        _, _, warnings = self.observe([self.wb], login={"providers": [{"id": "x", "authenticated": False}]}, model=None)
        self.assertIn("auth:no_provider", warnings)
        _, _, warnings = self.observe([self.wb], login=None, model="m")
        self.assertEqual(warnings, [], "a model without login data is a usable provider")

    def test_link_and_inode_drift_is_reported(self):
        home = self.home_obj
        self.assertEqual(omp_home.verify_omp_home(home), [])
        home.link.unlink()
        self.assertIn("auth_link:missing", omp_home.verify_omp_home(home))
        home.link.write_bytes(b"local")
        self.assertIn("auth_link:replaced_by_a_file", omp_home.verify_omp_home(home))
        home.link.unlink()
        home.link.symlink_to(self.root / "else.db")
        self.assertIn("auth_link:retargeted", omp_home.verify_omp_home(home))
        home.link.unlink()
        home.link.symlink_to(self.user_store)
        self.user_store.rename(self.user_store.with_name("old.db"))
        self.user_store.write_bytes(FAKE)  # a new inode at the same path (user logged in again)
        self.assertIn("auth_store:replaced", omp_home.verify_omp_home(home))
        (home.agent_dir / "agent.db-wal").write_bytes(b"x")
        self.assertIn("auth_link:split:agent.db-wal", omp_home.verify_omp_home(home))

    def test_check_isolation_reports_open_user_files_as_a_leak(self):
        h = _HomeRpc(self, self.home_obj, self.check_env)
        ok = h.check()
        self.assertEqual((ok["state"], ok["leaks"], ok["warnings"]), ("ok", [], []), ok)
        self.assertEqual(ok["observed"]["omp_home"]["authenticated_providers"], ["openai-codex"])
        sessions = self.home / ".omp" / "agent" / "sessions"
        sessions.mkdir()
        (sessions / "s.jsonl").write_text("")
        h.set_mode(open=[str(sessions / "s.jsonl")])
        leak = h.check()
        self.assertEqual(leak["state"], "leak", leak)
        self.assertIn(str(sessions / "s.jsonl"), json.dumps(leak["leaks"]))
        h.set_mode(open_wb=False)
        quiet = h.check()
        self.assertEqual(quiet["state"], "warning", quiet)
        self.assertIn("omp_home:not_observed", quiet["warnings"])
        h.set_mode(login_error=True)
        no_login = h.check()
        self.assertEqual(no_login["state"], "ok", "an OMP without get_login_providers must not fail the check")
        rpc = [r["type"] for r in h.records() if r["event"] == "rpc"]
        self.assertEqual(set(rpc), {"get_state", "get_available_commands", "get_login_providers"}, "zero model calls")

    def test_version_evidence_matches_the_home_probe(self):
        # C-D68 (6): the isolation evidence is re-made with the installed OMP 18.6.1 (no drift there, drift for 18.4.5)
        roles = {"manager": {"state": "ok", "ok": True, "leaks": [], "warnings": []},
                 "worker": {"state": "ok", "ok": True, "leaks": [], "warnings": []}}
        summary = launcher.summarize_isolation(roles, "omp/18.6.1")
        self.assertFalse(summary.get("version_drift", {}).get("isolation"),
                         f"the C-D68 isolation evidence is OMP 18.6.1: {summary.get('version_drift')}")
        self.assertEqual(launcher.summarize_isolation(roles, "omp/18.4.5")["version_drift"].get("isolation"), "18.6.1")


HOME_RPC = r'''#!/usr/bin/env python3
import json, os, sys
mode = json.load(open(os.environ["P27U_MODE"]))
log = open(os.environ["P27U_LOG"], "a")
held = []
if mode.get("open_wb", True):
    held.append(open(os.path.join(os.environ["PI_CODING_AGENT_DIR"], "models.db"), "ab"))
for path in mode.get("open", []):
    held.append(open(path, "rb"))
log.write(json.dumps({"event": "start", "pid": os.getpid()}) + "\n"); log.flush()
for line in sys.stdin:
    req = json.loads(line)
    log.write(json.dumps({"event": "rpc", "type": req["type"]}) + "\n"); log.flush()
    if req["type"] == "get_login_providers" and mode.get("login_error"):
        out = {"id": req["id"], "type": "response", "command": req["type"], "success": False, "error": "unknown"}
    else:
        data = {"get_state": {"model": {"provider": "openai-codex", "id": "gpt"}, "systemPrompt": "You are omp's x\n",
                              "dumpTools": [{"name": "read", "description": "r"}]},
                "get_available_commands": {"commands": [{"name": "init", "source": "builtin"}]},
                "get_login_providers": {"providers": [{"id": "openai-codex", "authenticated": True}]}}.get(req["type"], {})
        out = {"id": req["id"], "type": "response", "command": req["type"], "success": True, "data": data}
    sys.stdout.write(json.dumps(out) + "\n"); sys.stdout.flush()
'''


class _HomeRpc:
    def __init__(self, test: unittest.TestCase, home, env: dict):
        self.dir = Path(tempfile.mkdtemp(prefix="p27u-rpc-", dir="/tmp"))
        test.addCleanup(shutil.rmtree, self.dir, True)
        self.script = self.dir / "omp"
        self.script.write_text(HOME_RPC)
        self.script.chmod(0o755)
        self.mode, self.log = self.dir / "mode.json", self.dir / "log.jsonl"
        self.set_mode()
        self.env = {**env, "P27U_MODE": str(self.mode), "P27U_LOG": str(self.log)}
        self.home = home

    def set_mode(self, **mode):
        self.mode.write_text(json.dumps(mode))

    def records(self):
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def check(self):
        command = [str(self.script), "--config", "/x/s.yml", "--no-extensions", "--append-system-prompt", "",
                   "--no-title"]
        return launcher.check_isolation(command, cwd=self.dir, environment=self.env, role="worker", allowed_skills=(),
                                        omp_version="omp/18.4.5", timeout=15.0)


# ------------------------------------------------------------------- entrypoint (stub OMP of p27m) cases
class EntrypointHomeTests(p27m.RealEntrypointBase):
    maxDiff = None
    def test_start_refuses_with_an_empty_home_and_creates_no_omp_dir(self):
        shutil.rmtree(self.home / ".omp")
        before = inventory(self.home)
        out = self.start()
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("/login", out.stderr)
        self.assertIn("No backend was started", out.stderr)
        self.assertEqual(inventory(self.home), before)
        self.assertFalse(os.path.lexists(self.home / ".omp"), "~/.omp was created")
        self.assertEqual([c for c in self.calls() if c.get("event") == "start"], [])

    def test_data_dir_inside_the_users_omp_dir_is_refused(self):
        before = inventory(self.home)
        out = self.cli("start", "--data-dir", str(self.home / ".omp" / "wb"), "--omp", str(self.root / "omp"),
                       "--no-attach", "--timeout", "8")
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("overlaps", out.stderr)
        self.assertEqual(inventory(self.home), before)
        self.assertEqual([c for c in self.calls() if c.get("event") == "start"], [])

    def test_profile_from_env_is_refused(self):
        out = self.start(env_extra={"WORKBENCH_OMP_ARGS": "--profile work"})
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("--profile", out.stderr)
        self.assertIsNone(self.status_json())

    USER_REDIRECT_ENV = {"OMP_PROFILE": "work", "PI_PROFILE": "work", "PI_CODING_AGENT_DIR": "/tmp/nope",
                         "PI_CONFIG_DIR": ".nope"}

    def test_profile_env_never_reaches_omp_and_the_home_is_used(self):
        # Root adjudication (root-adjudication-p27-home-profile-fixture): with this user env the user's own omp keeps
        # its store at ~/.nope/profiles/work/agent/agent.db (F3), so the fake store lives there, not at ~/.omp/agent.
        user_store = self.home / ".nope" / "profiles" / "work" / "agent" / "agent.db"
        user_store.parent.mkdir(parents=True)
        user_store.write_bytes(p27m.FAKE_AUTH_BYTES)
        user_store.chmod(0o600)
        out = self.start(env_extra=self.USER_REDIRECT_ENV)
        self.assertNotEqual(out.returncode, 2, out.stdout + out.stderr)
        self.wait_isolation()
        for call in self.by_kind("pane") + self.by_kind("rpc"):
            self.assertIsNone(call["env"]["OMP_PROFILE"])
            self.assertIsNone(call["env"]["PI_PROFILE"])
            p27m.assert_workbench_home_env(self, call["env"], self.home, self.data)
        self.assertEqual(os.readlink(self.data / "omp-root" / "agent" / "agent.db"), str(user_store))
        self.assertFalse(os.path.lexists("/tmp/nope"))

    def test_profile_env_with_the_store_only_at_the_default_location_is_refused(self):
        # complementary case: ~/.omp/agent/agent.db exists (planted by the base fixture) but the user's own omp
        # would not use it with this env -> refuse with guidance, create nothing, start no OMP
        before = inventory(self.home)
        out = self.start(env_extra=self.USER_REDIRECT_ENV)
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("/login", out.stderr)
        self.assertIn(str(self.home / ".nope" / "profiles" / "work" / "agent" / "agent.db"), out.stderr)
        self.assertNotIn(str(self.auth_store), out.stderr, "guidance names the store the user's omp would not use")
        self.assertEqual(inventory(self.home), before)
        self.assertFalse(os.path.lexists(self.data), "a refused start created the data dir")
        self.assertEqual([c for c in self.calls() if c.get("event") == "start"], [])
        self.assertFalse(os.path.lexists("/tmp/nope"))

    def test_an_older_data_dir_gets_the_home_on_the_next_start(self):
        # an older data dir: state files from a run before C-D64, no omp-root, a stray local agent store
        self.data.mkdir(mode=0o700)
        (self.data / "omp-root" / "agent").mkdir(parents=True, mode=0o700)
        (self.data / "omp-root" / "agent" / "agent.db").write_bytes(b"older local store p27u\n")
        self.start()
        snapshot = self.wait_isolation()
        self.assertTrue(snapshot["omp_isolation"]["checked"])
        agent = self.data / "omp-root" / "agent"
        self.assertTrue((agent / "agent.db").is_symlink())
        self.assertEqual(os.readlink(agent / "agent.db"), str(self.auth_store))
        self.assertTrue([n for n in os.listdir(agent) if n.startswith("agent.db.moved-")])
        self.assertIn("moved aside", json.dumps(snapshot["omp_isolation"].get("notes")))

    def test_a_user_edit_of_config_yml_is_replaced_at_the_next_start(self):
        self.start()
        self.wait_isolation()
        self.cli("shutdown", "--data-dir", str(self.data), "--yes", timeout=60)
        config = self.data / "omp-root" / "agent" / "config.yml"
        original = config.read_bytes()
        config.write_text("setupVersion: 1\n")
        config.chmod(0o644)
        self.start()
        self.wait_isolation()
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)


# ------------------------------------------------- F2 (p27-home-test-02): WORKBENCH_USER_* never reaches a child
class WithheldUserValuesNeverLeakTests(p27m.RealEntrypointBase):
    """The classifier sees the user's withheld XDG dirs as ``WORKBENCH_USER_*``; no child process may ever see them.

    A wrapper ``omp`` records the COMPLETE environment of every process the real backend starts through it (the two
    panes and the two isolation checks; fake values only, temp dirs) and then execs the p27m entrypoint stub.
    """

    XDG = ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")

    def setUp(self):
        super().setUp()
        self.xdg = {key: self.root / f"user-{key.lower()}" for key in self.XDG}
        for base in self.xdg.values():
            (base / "omp").mkdir(parents=True)
        (self.xdg["XDG_DATA_HOME"] / "omp" / "agent.db").write_bytes(p27m.FAKE_AUTH_BYTES)  # the user's store (XDG)
        (self.root / "omp-real").write_text((self.root / "omp").read_text())
        (self.root / "omp-real").chmod(0o755)
        self.dumps = self.root / "env-dumps"
        self.dumps.mkdir()
        (self.root / "omp").write_text(
            f"#!/bin/sh\n/usr/bin/env -0 > {self.dumps}/$$.$(/usr/bin/date +%s%N).env\nexec {self.root}/omp-real \"$@\"\n")
        self.extra = {key: str(path) for key, path in self.xdg.items()}

    def dumped(self) -> list[dict[str, str]]:
        out = []
        for path in sorted(self.dumps.iterdir()):
            pairs = [item.decode().partition("=") for item in path.read_bytes().split(b"\0") if item]
            out.append({key: value for key, _, value in pairs})
        return out

    def test_no_child_environment_carries_workbench_user_values_or_the_users_xdg_dirs(self):
        self.start(env_extra=self.extra)  # the stub never joins the bridge: `start` itself stays "starting"
        snapshot = self.wait_isolation()
        iso = snapshot["omp_isolation"]
        self.assertTrue(iso["checked"], iso)
        self.assertEqual((iso["state"], iso["leaks"]), ("ok", []), iso)
        envs = self.dumped()
        roled = [env for env in envs if env.get("WORKBENCH_G3_ROLE")]
        self.assertGreaterEqual(len(roled), 4, "expected the two panes and the two isolation checks")
        self.assertEqual({env["WORKBENCH_G3_ROLE"] for env in roled}, {"manager", "worker"})
        for env in envs:  # every OMP-named process, also the version probe
            self.assertEqual([k for k in env if k.startswith("WORKBENCH_USER_")], [], "WORKBENCH_USER_* reached an OMP")
            if not env.get("WORKBENCH_G3_ROLE"):
                continue  # the `omp --version` probe runs in the user's own environment by design (no Workbench home)
            blob = "\n".join(f"{k}={v}" for k, v in env.items())
            for key, path in self.xdg.items():
                self.assertNotIn(str(path), blob, f"the user's {key} value reached an OMP environment")
                self.assertNotEqual(env.get(key), str(path))
            p27m.assert_workbench_home_env(self, env, self.home, self.data)
        # the classifier did get the user's dirs (the backend itself must not have lost them): a user-dir file is a leak
        self.assertTrue(os.path.isfile(self.xdg["XDG_DATA_HOME"] / "omp" / "agent.db"))


# ---------------------------------------------------------------------- E7/E9 restart through the backend
class RestartHomeTests(p27q._BackendCase):
    def setUp(self):  # p27q's fixture plus a fake user auth store in its HOME (planted before the backend opens)
        real_mkdir = Path.mkdir

        def mkdir(path, *args, **kwargs):
            real_mkdir(path, *args, **kwargs)
            if path.name == "h" and path.parent.name.startswith("p27q-"):
                p27m.plant_fake_auth_store(path)

        with mock.patch.object(Path, "mkdir", mkdir):
            super().setUp()
        self.home = Path(self.backend.environment["HOME"])
        self.home_tree = inventory(self.home)

    def home_env(self, entry) -> dict:
        return {key: entry["env"].get(key) for key in HOME_KEYS}

    def test_restart_reuses_the_home_env_rewrites_config_and_repairs_the_link(self):
        first = self.of_role("worker")[0]
        p27m.assert_workbench_home_env(self, first["env"], self.home, self.backend.layout.root)
        agent = self.backend.layout.root / "omp-root" / "agent"
        config = agent / "config.yml"
        original = config.read_bytes()
        config.write_text("edited: true\n")
        (agent / "agent.db").unlink()
        self.exit_pane(PaneId.WORKER_OMP)
        self.restart_ready(PaneId.WORKER_OMP)
        again = self.of_role("worker")[-1]
        self.assertEqual(self.home_env(again), self.home_env(first))
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual(os.readlink(agent / "agent.db"), str(self.home / ".omp" / "agent" / "agent.db"))
        role, _command, env = self.checks[-1]
        self.assertEqual(role, "worker")
        self.assertEqual({k: env.get(k) for k in HOME_KEYS}, self.home_env(first), "the re-check used another home")
        self.assertEqual(inventory(self.home), self.home_tree)

    def test_a_store_that_vanished_is_unlinked_at_restart_and_never_recreated(self):
        store = self.home / ".omp" / "agent" / "agent.db"
        store.unlink()
        self.exit_pane(PaneId.MANAGER_OMP)
        self.backend.restart_pane(PaneId.MANAGER_OMP)
        link = self.backend.layout.root / "omp-root" / "agent" / "agent.db"
        self.assertFalse(os.path.lexists(link), "the dangling link stayed: OMP would create the user's store")
        self.assertFalse(os.path.lexists(store))
        self.assertIn("/login", json.dumps(self.backend.omp_isolation.get("notes")))

    def test_link_drift_during_a_check_turns_ok_into_a_warning(self):
        self.gate.clear()
        self.exit_pane(PaneId.WORKER_OMP)
        self.backend.restart_pane(PaneId.WORKER_OMP)
        self.wait(lambda: len(self.checks) >= 3, "the re-check to start")
        link = self.backend.layout.root / "omp-root" / "agent" / "agent.db"
        link.unlink()
        link.symlink_to(self.home / "elsewhere.db")
        self.gate.set()
        self.wait(lambda: "rechecking" not in self.backend.omp_isolation
                  and self.backend.omp_isolation["state"] != "ok", "the re-check result")
        iso = self.backend.omp_isolation
        self.assertEqual(iso["state"], "warning", iso)
        self.assertIn("auth_link:retargeted", json.dumps(iso))


if __name__ == "__main__":
    unittest.main()
