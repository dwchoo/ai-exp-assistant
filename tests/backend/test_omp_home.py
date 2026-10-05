"""C-D64 Workbench-owned OMP home: launcher env/argv, home setup, auth link, no-auth start, isolation criteria.

Temp HOME fixtures only (never the real ~/.omp); the fake user ``agent.db`` is
made unreadable (0000) so any attempt to open it would fail the test.
"""
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

from workbench.backend import cli, launcher, omp_home
from workbench.backend.launcher import LaunchPlan, StartRequirementError
from workbench.backend.omp_home import OmpHomeError
from workbench.backend.paths import DataLayout, ensure_private_dir
from workbench.backend.service import Backend, _with_home_problems
from workbench.contracts.v1 import PaneId
from workbench.terminal.shell_g2.prototype import ShellChoice


def inventory(root: Path) -> dict:
    """lstat-only metadata of a tree (never opens files)."""
    found = {}
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(directory) / name
            info = os.lstat(path)
            found[str(path.relative_to(root))] = (stat.S_IFMT(info.st_mode), info.st_size, info.st_mtime_ns,
                                                  info.st_ino, os.readlink(path) if path.is_symlink() else None)
    return found


class HomeFixture(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="cw17-home-")
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.data = ensure_private_dir(self.root / "data")
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home)}
        self.addCleanup(self.unlock)

    def user_store(self, *, create=True) -> Path:
        agent = self.home / ".omp" / "agent"
        agent.mkdir(parents=True, exist_ok=True)
        store = agent / "agent.db"
        if create:
            store.write_bytes(b"\0" * 64)
            store.chmod(0)  # any open() of the user's store would now fail
        return store

    def unlock(self):
        store = self.home / ".omp" / "agent" / "agent.db"
        if store.exists() and not store.is_symlink():
            store.chmod(0o600)

    def prepare(self, env=None):
        return omp_home.prepare_omp_home(self.data, env or self.env, skills_dir=launcher.default_skills_dir(),
                                         provider_ids=launcher.ISOLATION_PROVIDER_IDS)


class LauncherEnvironmentTests(HomeFixture):
    def plan(self, *args):
        return LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/x/omp", "omp/18.4.5", "/x/bridge.ts", tuple(args))

    def test_omp_children_get_the_workbench_home_and_never_a_profile_or_user_override(self):
        self.user_store()
        home = self.prepare()
        base = {**self.env, "OMP_PROFILE": "work", "PI_PROFILE": "work", "PI_CONFIG_DIR": ".mine",
                "PI_CODING_AGENT_DIR": "/u/agent", "OMP_WORKTREE_DIR": "/u/wt", "OMP_GITHUB_CACHE_DB": "/u/g.db",
                "OMP_COMMIT_CACHE_DB": "/u/c.db", "OMP_JUDGMENT_CACHE_DB": "/u/j.db",
                "OMP_AUTH_BROKER_SNAPSHOT_CACHE": "/u/a", "BUN_RUNTIME_TRANSPILER_CACHE_PATH": "/u/bun",
                "PI_AUTO_QA": "1", "OPENAI_API_KEY": "user-own"}
        for env in (launcher.omp_environment(base, self.plan(), role="worker", token="t",
                                             bridge_socket=Path("/d/b.sock"), home=home.environment()),
                    launcher.isolation_check_environment(base, self.plan(), role="manager",
                                                         absent_socket=Path("/d/x.sock"), home=home.environment())):
            self.assertEqual(env["PI_CODING_AGENT_DIR"], str(self.data / "omp-root" / "agent"))
            self.assertEqual(env["PI_CONFIG_DIR"], os.path.relpath(self.data / "omp-root", self.home))
            # OMP joins PI_CONFIG_DIR to HOME (path.join normalises ..)
            self.assertEqual(os.path.normpath(os.path.join(env["HOME"], env["PI_CONFIG_DIR"])),
                             str(self.data / "omp-root"))
            self.assertEqual(env["BUN_RUNTIME_TRANSPILER_CACHE_PATH"],
                             str(self.data / "omp-root" / "bun-transpiler-cache"))
            for key in ("OMP_PROFILE", "PI_PROFILE", "OMP_WORKTREE_DIR", "OMP_GITHUB_CACHE_DB",
                        "OMP_COMMIT_CACHE_DB", "OMP_JUDGMENT_CACHE_DB", "OMP_AUTH_BROKER_SNAPSHOT_CACHE",
                        "PI_AUTO_QA"):
                self.assertNotIn(key, env)
            self.assertEqual((env["HOME"], env["OPENAI_API_KEY"]), (str(self.home), "user-own"))
        # without a home only the overrides are dropped (never a user agent dir or profile)
        bare = launcher.omp_environment(base, self.plan(), role="worker", token="t", bridge_socket=Path("/d/b"))
        for key in ("PI_CONFIG_DIR", "PI_CODING_AGENT_DIR", "OMP_PROFILE", "PI_PROFILE"):
            self.assertNotIn(key, bare)
        # the user's own shell keeps all of it
        shell = launcher.shell_environment(base)
        self.assertEqual((shell["OMP_PROFILE"], shell["PI_CONFIG_DIR"]), ("work", ".mine"))
        self.assertNotIn("PI_CODING_AGENT_DIR", launcher.shell_environment(self.env))

    def test_xdg_dirs_that_would_redirect_omp_state_are_not_passed(self):
        self.user_store()
        home = self.prepare()
        used, unused = self.root / "xdg-data", self.root / "xdg-state"
        (used / "omp").mkdir(parents=True)
        unused.mkdir()
        base = {**self.env, "XDG_DATA_HOME": str(used), "XDG_STATE_HOME": str(unused)}
        env = launcher.omp_environment(base, self.plan(), role="worker", token="t", bridge_socket=Path("/d/b"),
                                       home=home.environment())
        self.assertNotIn("XDG_DATA_HOME", env)
        self.assertEqual(env["XDG_STATE_HOME"], str(unused))

    def test_argv_keeps_the_c_d59_isolation_and_profile_is_refused(self):
        argv = launcher.omp_command(self.plan("--model", "m"), "/d/omp-isolation-worker.yml")
        self.assertEqual(argv, ["/x/omp", "--config", str(launcher.default_isolation_overlay()),
                                "--config", "/d/omp-isolation-worker.yml", "--no-extensions",
                                "--append-system-prompt", "", "--no-title",
                                "--tools", ",".join(launcher.WORKER_TOOLS),  # C-D68: worker without bash/eval
                                "--model", "m", "--extension", "/x/bridge.ts"])
        shells = self.root / "bin"
        shells.mkdir()
        (shells / "bash").symlink_to("/bin/sh")
        fake = shells / "omp"
        fake.write_text("#!/bin/sh\necho omp/18.4.5\n")
        fake.chmod(0o700)
        for args in (("--profile", "work"), ("--profile=work",)):
            with self.assertRaisesRegex(StartRequirementError, "--profile"):
                launcher.build_plan({"PATH": str(shells)}, omp=str(fake), omp_args=args)
            with self.assertRaisesRegex(StartRequirementError, "--profile"):
                launcher.build_plan({"PATH": str(shells), "WORKBENCH_OMP_ARGS": " ".join(args)}, omp=str(fake))
        self.assertEqual(launcher.build_plan({"PATH": str(shells)}, omp=str(fake),
                                             omp_args=("--profiles-are-not-this",)).omp_args,
                         ("--profiles-are-not-this",))


class HomeSetupTests(HomeFixture):
    def test_first_start_creates_the_private_layout_config_and_auth_link(self):
        store = self.user_store()
        before = inventory(self.home)
        home = self.prepare()
        root, agent = self.data / "omp-root", self.data / "omp-root" / "agent"
        self.assertEqual((home.root, home.agent_dir, home.auth_store, home.linked), (root, agent, store, True))
        for directory in (root, agent):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        link = agent / "agent.db"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(store))
        config = agent / "config.yml"
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        content = json.loads(config.read_text().split("\n", 2)[2])  # two comment lines, then JSON (valid YAML)
        self.assertEqual(content["setupVersion"], 2)
        self.assertEqual(content["startup"], {"setupWizard": False})
        self.assertEqual(content["disabledProviders"], list(launcher.ISOLATION_PROVIDER_IDS))
        self.assertEqual(content["skills"], {"customDirectories": [str(launcher.default_skills_dir())],
                                            "includeSkills": [], "ignoredSkills": []})
        self.assertEqual(content["dev"], {"autoqa": False})
        self.assertEqual(sorted(os.listdir(agent)), ["agent.db", "agents", "config.yml"])  # C-D68: Workbench agents
        self.assertEqual(inventory(self.home), before, "the user's home must not change")
        self.assertEqual(omp_home.verify_omp_home(home), [])

    def test_setup_is_idempotent_and_rewrites_config_each_start(self):
        self.user_store()
        first = self.prepare()
        config = first.config
        config.write_text("setupVersion: 1\n")  # OMP or a user edited it
        link_inode = os.lstat(first.link).st_ino
        second = self.prepare()
        self.assertEqual((second.root, second.config_dir, second.linked), (first.root, first.config_dir, True))
        self.assertIn('"setupVersion": 2', config.read_text())
        self.assertEqual(os.lstat(second.link).st_ino, link_inode, "a correct link is left in place")
        self.assertEqual((second.notes, second.moved_aside), ((), ()))

    def test_wrong_or_dangling_link_is_repaired(self):
        store = self.user_store()
        home = self.prepare()
        home.link.unlink()
        home.link.symlink_to(self.root / "elsewhere.db")
        self.assertEqual(omp_home.verify_omp_home(home), ["auth_link:retargeted"])
        repaired = self.prepare()
        self.assertEqual(os.readlink(repaired.link), str(store))
        self.assertFalse((self.root / "elsewhere.db").exists(), "the link is replaced, never followed")
        self.assertEqual(omp_home.verify_omp_home(repaired), [])

    def test_a_regular_store_from_an_older_run_is_moved_aside_inside_the_workbench_home(self):
        store = self.user_store()
        agent = ensure_private_dir(ensure_private_dir(self.data / "omp-root") / "agent")
        for name, payload in (("agent.db", b"OLD-LOCAL-DB"), ("agent.db-wal", b"W"), ("agent.db-shm", b"S")):
            (agent / name).write_bytes(payload)
        before = inventory(self.home)
        home = self.prepare()
        self.assertTrue(home.link.is_symlink())
        self.assertEqual(os.readlink(home.link), str(store))
        self.assertEqual(len(home.moved_aside), 3)
        moved = {name: (agent / name).read_bytes() for name in home.moved_aside}
        self.assertEqual(sorted(moved.values()), [b"OLD-LOCAL-DB", b"S", b"W"])
        self.assertTrue(all(name.startswith("agent.db") and ".moved-" in name for name in moved))
        self.assertTrue(any("moved aside" in note for note in home.notes))
        self.assertEqual(inventory(self.home), before, "nothing is moved into the user's home")
        self.assertEqual(omp_home.verify_omp_home(home), [])

    def test_an_existing_data_dir_without_a_home_gets_the_new_layout(self):
        self.user_store()
        for name in ("backend.json", "tasks.sqlite3", "omp-isolation-manager.yml"):
            (self.data / name).write_text("{}")
        home = self.prepare()
        self.assertTrue(home.link.is_symlink() and home.config.is_file())
        self.assertEqual((self.data / "backend.json").read_text(), "{}")

    def test_verify_reports_a_split_or_replaced_store(self):
        store = self.user_store()
        home = self.prepare()
        (home.agent_dir / "agent.db-wal").write_bytes(b"")
        self.assertEqual(omp_home.verify_omp_home(home), ["auth_link:split:agent.db-wal"])
        (home.agent_dir / "agent.db-wal").unlink()
        home.link.unlink()
        home.link.write_bytes(b"")  # OMP replaced the link by a local file
        self.assertIn("auth_link:replaced_by_a_file", omp_home.verify_omp_home(home))
        home.link.unlink()
        home.link.symlink_to(store)
        fresh = store.with_name("fresh.db")
        fresh.write_bytes(b"")
        os.replace(fresh, store)  # a new inode: the user's store was recreated
        self.assertEqual(omp_home.verify_omp_home(home), ["auth_store:replaced"])


class NoAuthStoreTests(HomeFixture):
    def test_missing_user_agent_dir_creates_nothing_in_home_and_gives_login_guidance(self):
        before = inventory(self.home)
        home = self.prepare()
        self.assertFalse(home.linked)
        self.assertFalse(os.path.lexists(home.link))
        self.assertEqual(inventory(self.home), before)
        self.assertFalse((self.home / ".omp").exists())
        guidance = omp_home.auth_guidance(self.env)
        for needle in ("'omp'", "/login", "API key", str(self.home / ".omp" / "agent" / "agent.db"), "missing"):
            self.assertIn(needle, guidance)
        self.assertTrue(any("/login" in note for note in home.notes))

    def test_a_link_left_from_an_earlier_start_is_removed_when_the_store_is_gone(self):
        store = self.user_store()
        home = self.prepare()
        store.chmod(0o600)
        store.unlink()
        before = inventory(self.home)
        again = self.prepare()
        self.assertFalse(again.linked)
        self.assertFalse(os.path.lexists(home.link), "OMP would create the user's store through the link")
        self.assertEqual(inventory(self.home), before)
        self.assertTrue(any("removed the agent.db link" in note for note in again.notes))

    def test_a_symlinked_or_foreign_store_is_not_used(self):
        agent = self.home / ".omp" / "agent"
        agent.mkdir(parents=True)
        (agent / "agent.db").symlink_to(self.root / "real.db")
        self.assertIn("symlink", omp_home.auth_guidance(self.env))
        self.assertFalse(self.prepare().linked)

    def test_start_refuses_before_any_backend_with_the_guidance(self):
        args = cli.parser().parse_args(["start", "--data-dir", str(self.data), "--no-attach"])
        stderr = io.StringIO()
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/x/omp", "omp/18.4.5", "/x/bridge.ts")
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(cli, "_running_snapshot", return_value=None), \
                mock.patch.object(cli, "build_plan", return_value=plan), \
                mock.patch.object(cli, "spawn_backend") as spawn, mock.patch("sys.stderr", stderr):
            self.assertEqual(cli.cmd_start(args), cli.EXIT_REQUIREMENT)
        spawn.assert_not_called()
        self.assertIn("/login", stderr.getvalue())
        self.assertIn("No backend was started.", stderr.getvalue())
        self.assertFalse((self.home / ".omp").exists())
        self.assertFalse((self.data / "omp-root").exists())

    def test_start_requirements_pass_with_a_store(self):
        self.user_store()
        cli.omp_home_requirements(DataLayout(self.data), self.env)  # no exception


class ConfigDirValueTests(HomeFixture):
    def test_data_dir_outside_home_is_expressed_with_dotdot(self):
        value = omp_home.config_dir_value(self.root / "elsewhere" / "omp-root", self.env)
        self.assertTrue(value.startswith(".."))
        self.assertEqual(os.path.normpath(os.path.join(str(self.home), value)), str(self.root / "elsewhere" / "omp-root"))

    def test_symlinked_home_is_refused(self):
        real = self.root / "real-home"
        real.mkdir()
        (self.root / "deep").mkdir()
        link = self.root / "deep" / "home"
        link.symlink_to(real)
        with self.assertRaisesRegex(OmpHomeError, "relative to HOME"):
            omp_home.config_dir_value(self.root / "data" / "omp-root", {"HOME": str(link)})

    def test_the_users_own_omp_dir_is_refused(self):
        self.user_store()
        for target in (self.home / ".omp", self.home / ".omp" / "x" / "omp-root", self.home):
            with self.assertRaises(OmpHomeError):
                omp_home.config_dir_value(target, self.env)
        with self.assertRaises(StartRequirementError):
            cli.omp_home_requirements(DataLayout(self.home / ".omp" / "wb"), self.env)


class OverlayTests(HomeFixture):
    def test_task_agents_come_from_the_project_and_the_workbench_home_only(self):
        self.user_store()
        home = self.prepare()
        project = self.root / "proj"
        user_agents = self.home / ".omp" / "agent" / "agents"
        for directory, name in ((project / ".omp" / "agents", "proj-agent"), (user_agents, "user-agent"),
                                (home.agent_dir / "agents", "wb-agent")):
            directory.mkdir(parents=True, exist_ok=True)  # the home's agents dir holds the Workbench definitions
            (directory / f"{name}.md").write_text(f"---\nname: {name}\ndescription: d\n---\n")
        env = launcher.omp_environment(self.env, LaunchPlan(ShellChoice("bash", "/bin/bash"), "/x/omp", "v", "/x/b"),
                                       role="worker", token="t", bridge_socket=Path("/d/b"), home=home.environment())
        overlay = launcher.role_overlay("worker", project_dir=project, home=self.home, environment=env)
        # ambient definitions, then (C-D68) the worker's bundled agents; the installed Workbench ones are not ambient
        self.assertEqual(overlay["task"]["disabledAgents"], ["proj-agent", "wb-agent", *launcher.BUNDLED_AGENT_ROLES])
        self.assertEqual(overlay["disabledProviders"], list(launcher.ISOLATION_PROVIDER_IDS))
        self.assertEqual(overlay["skills"]["customDirectories"], [str(launcher.default_skills_dir())])


FAKE_RPC_OMP = r'''#!{python}
import json, os, sys
held = [open(path, "rb") for path in filter(None, os.environ.get("FAKE_OPEN", "").split(":"))]
if os.environ.get("FAKE_ENV_OUT"):
    with open(os.environ["FAKE_ENV_OUT"], "w") as stream:
        json.dump(dict(os.environ), stream)
mode = os.environ.get("FAKE_LOGIN", "ok")
for line in sys.stdin:
    request = json.loads(line)
    kind = request["type"]
    if kind == "get_state":
        data = {{"systemPrompt": ["You are omp's assistant"],
                 "dumpTools": json.loads(os.environ.get("FAKE_TOOLS", "[]")),
                 "model": None if mode == "none" else {{"provider": "p", "id": "m"}}}}
    elif kind == "get_login_providers":
        if mode == "fail":
            print(json.dumps({{"type": "response", "id": request["id"], "command": kind, "success": False,
                              "error": "unknown"}}), flush=True)
            continue
        data = {{"providers": [{{"id": "openai-codex", "name": "x", "authenticated": mode == "ok"}},
                               {{"id": "other", "name": "y", "authenticated": False}}]}}
    else:
        data = {{"commands": []}}
    print(json.dumps({{"type": "response", "id": request["id"], "command": kind, "success": True,
                      "data": data}}), flush=True)
'''


class IsolationCriteriaTests(HomeFixture):
    def setUp(self):
        super().setUp()
        self.store = self.user_store()
        self.omp_home = self.prepare()
        self.fake = self.root / "omp"
        self.fake.write_text(FAKE_RPC_OMP.format(python=sys.executable))
        self.fake.chmod(0o700)
        self.models = self.omp_home.agent_dir / "models.db"
        self.models.write_bytes(b"")

    def check(self, *, opened=(), login="ok", command=()):
        env = {**self.env, **self.omp_home.environment(), "FAKE_OPEN": ":".join(map(str, opened)),
               "FAKE_LOGIN": login}
        return launcher.check_isolation([str(self.fake), *command, "--no-title"], cwd=self.root, environment=env,
                                        role="worker", allowed_skills=(), omp_version="omp/18.4.5", timeout=10)

    def test_workbench_home_in_use_with_a_provider_is_ok(self):
        result = self.check(opened=(self.models,))
        self.assertEqual((result["state"], result["leaks"], result["warnings"], result["error"]),
                         ("ok", [], [], None))
        observed = result["observed"]["omp_home"]
        self.assertEqual(observed["agent_dir"], os.path.realpath(self.omp_home.agent_dir))
        self.assertTrue(observed["agent_dir_in_use"])
        self.assertEqual((observed["authenticated_providers"], observed["user_paths"]), (["openai-codex"], []))

    def test_a_file_held_open_in_the_users_omp_dir_is_a_leak_but_the_shared_store_is_not(self):
        user_log = self.home / ".omp" / "logs" / "omp.log"
        user_log.parent.mkdir(parents=True)
        user_log.write_text("")
        self.store.chmod(0o600)  # the fake (not Workbench) opens it, like OMP's SQLite would
        result = self.check(opened=(self.models, self.store, user_log))
        self.assertEqual((result["state"], result["leaks"]), ("leak", [f"user_omp_path:{user_log}"]))
        self.assertTrue(result["observed"]["omp_home"]["auth_store_open"])

    def test_unobserved_home_and_no_provider_are_warnings(self):
        result = self.check(login="none")
        self.assertEqual((result["state"], result["ok"], result["leaks"]), ("warning", True, []))
        self.assertEqual(result["warnings"], ["omp_home:not_observed", "auth:no_provider"])
        # a model without a logged-in login provider (env API key) is no warning
        self.assertEqual(self.check(opened=(self.models,), login="out")["state"], "ok")

    def test_login_query_failure_does_not_fail_the_check(self):
        result = self.check(opened=(self.models,), login="fail")
        self.assertEqual((result["state"], result["error"]), ("ok", None))
        self.assertIsNone(result["observed"]["omp_home"]["authenticated_providers"])

    def test_users_personality_file_is_no_longer_reported_and_profile_is_a_leak(self):
        personality = self.home / ".omp" / "agent" / "PERSONALITY.md"
        personality.write_text("pirate\n")
        result = self.check(opened=(self.models,))
        self.assertEqual((result["state"], result["warnings"]), ("ok", []))
        self.assertEqual(launcher.ambient_prompt_files(self.root, {**self.env, **self.omp_home.environment()}), [])
        self.assertIn("profile:--profile=work", self.check(opened=(self.models,),
                                                           command=("--profile=work",))["leaks"])

    def test_user_append_system_md_is_looked_up_in_the_workbench_home(self):
        (self.omp_home.agent_dir / "APPEND_SYSTEM.md").write_text("WB\n")
        (self.home / ".omp" / "agent" / "APPEND_SYSTEM.md").write_text("USER\n")
        files = launcher.ambient_prompt_files(self.root / "none", {**self.env, **self.omp_home.environment()})
        self.assertEqual([item["path"] for item in files], [str(self.omp_home.agent_dir / "APPEND_SYSTEM.md")])

    def test_home_problems_turn_ok_into_a_warning_only(self):
        ok = {"state": "ok", "ok": True, "leaks": [], "warnings": []}
        self.assertEqual(_with_home_problems(ok, ["auth_link:retargeted"])["state"], "warning")
        self.assertEqual(_with_home_problems(dict(ok, state="leak"), ["x"])["state"], "leak")
        self.assertIs(_with_home_problems(ok, []), ok)
        summary = launcher.summarize_isolation({"worker": _with_home_problems(ok, ["auth_link:retargeted"])},
                                               "omp/18.4.5")
        self.assertEqual((summary["state"], summary["ok"]), ("warning", True))
        self.assertIn("auth_link:retargeted", summary["warning"])
        self.assertNotIn("PERSONALITY", summary["warning"])


FAKE_PANE_OMP = r'''#!{python}
import json, os, socket, sys, uuid
argv = sys.argv[1:]
with open(os.environ["FAKE_RECORD"], "a") as stream:
    stream.write(json.dumps({{"argv": argv, "pid": os.getpid(),
                              "env": {{k: os.environ.get(k) for k in ("PI_CONFIG_DIR", "PI_CODING_AGENT_DIR",
                                       "BUN_RUNTIME_TRANSPILER_CACHE_PATH", "OMP_PROFILE", "HOME")}}}}) + "\n")
if argv[:1] == ["config"]:
    sys.exit(0)
sock = socket.socket(socket.AF_UNIX)
sock.connect(os.environ["WORKBENCH_G3_BRIDGE_SOCKET"])
hello = {{"kind": "hello", "protocolVersion": 1, "token": os.environ["WORKBENCH_G3_TOKEN"],
          "role": os.environ["WORKBENCH_G3_ROLE"], "ompSessionId": str(uuid.uuid4()),
          "generation": int(os.environ["WORKBENCH_G3_GENERATION"]), "pid": os.getpid()}}
sock.sendall((json.dumps(hello) + "\n").encode())
sock.makefile("rb").readline()
print("fake omp ready", flush=True)
for line in sys.stdin:
    if line.strip() == "exit":
        break
'''


class BackendHomeTests(HomeFixture):
    def setUp(self):
        super().setUp()
        self.fake = self.root / "omp"
        self.fake.write_text(FAKE_PANE_OMP.format(python=sys.executable))
        self.fake.chmod(0o700)
        self.record = self.root / "record.jsonl"
        self.project = self.root / "proj"
        self.project.mkdir()
        self.checks = []
        patcher = mock.patch("workbench.backend.service.check_isolation", self.fake_check)
        patcher.start()
        self.addCleanup(patcher.stop)

    def user_store(self, *, create=True) -> Path:
        # start() runs with the user's OMP_PROFILE=work: the user's own omp keeps its store in the profile (F3)
        agent = self.home / ".omp" / "profiles" / "work" / "agent"
        agent.mkdir(parents=True, exist_ok=True)
        store = agent / "agent.db"
        if create:
            store.write_bytes(b"\0" * 64)
            store.chmod(0)
            self.addCleanup(lambda: store.exists() and not store.is_symlink() and store.chmod(0o600))
        return store

    def fake_check(self, command, *, cwd, environment, role, allowed_skills, omp_version, cancel):
        self.checks.append((role, dict(environment)))
        return {"role": role, "state": "ok", "ok": True, "leaks": [], "warnings": [], "error": None}

    def start(self, extra_env=None):
        env = {**self.env, "FAKE_RECORD": str(self.record), "OMP_PROFILE": "work", **(extra_env or {})}
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), str(self.fake), "omp/18.4.5", "/x/bridge.ts")
        backend = Backend(DataLayout(self.data), plan, project_dir=str(self.project), environment=env)
        self.addCleanup(backend._close)
        backend._open()
        self.wait(backend, lambda: backend.phase == "ready" and backend.omp_isolation["state"] != "pending")
        return backend

    def wait(self, backend, predicate, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            backend._tick(0.02)
            if predicate():
                return
        self.fail(f"timed out; phase={backend.phase} isolation={backend.omp_isolation['state']}")

    def records(self):
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def test_both_panes_and_checks_use_the_home_and_no_user_config_is_read(self):
        store = self.user_store()
        before = inventory(self.home)
        backend = self.start()
        agent = str(self.data / "omp-root" / "agent")
        records = self.records()
        self.assertEqual(len(records), 2, "only the two panes ran: no 'omp config get' read of the user config")
        for item in records:
            self.assertEqual(item["env"]["PI_CODING_AGENT_DIR"], agent)
            self.assertIsNone(item["env"]["OMP_PROFILE"])
            self.assertEqual(item["env"]["HOME"], str(self.home))
        self.assertEqual(sorted(role for role, _ in self.checks), ["manager", "worker"])
        for _, env in self.checks:
            self.assertEqual(env["PI_CODING_AGENT_DIR"], agent)
            self.assertNotIn("OMP_PROFILE", env)
        self.assertEqual(os.readlink(self.data / "omp-root" / "agent" / "agent.db"), str(store))
        self.assertEqual(backend.omp_isolation["state"], "ok")
        self.assertEqual(inventory(self.home), before)

    def test_restart_reuses_the_env_and_repairs_the_home(self):
        store = self.user_store()
        backend = self.start()
        config = self.data / "omp-root" / "agent" / "config.yml"
        config.write_text("setupVersion: 1\n")
        link = self.data / "omp-root" / "agent" / "agent.db"
        link.unlink()
        pane = backend.panes[PaneId.WORKER_OMP]
        self.assertIsNone(backend.admit(PaneId.WORKER_OMP, b"exit\n", "input"))
        self.wait(backend, lambda: pane.info()["alive"] is False)
        result = backend.restart_pane(PaneId.WORKER_OMP)
        self.assertTrue(result["restarted"])
        self.wait(backend, lambda: len(self.records()) == 3 and backend.omp_isolation["state"] == "ok")
        again = self.records()[2]
        # the worker's first start (the two panes may start in either order)
        first = next(item for item in self.records()[:2]
                     if any(arg.endswith("omp-isolation-worker.yml") for arg in item["argv"]))
        self.assertEqual(again["env"], first["env"])
        self.assertEqual(again["argv"], first["argv"])
        self.assertEqual(os.readlink(link), str(store))
        self.assertIn('"setupVersion": 2', config.read_text())

    def test_a_changed_auth_link_during_the_check_is_reported(self):
        self.user_store()

        def tamper(command, **kwargs):
            link = self.data / "omp-root" / "agent" / "agent.db"
            link.unlink()
            link.write_bytes(b"")
            return self.fake_check(command, **kwargs)
        with mock.patch("workbench.backend.service.check_isolation", tamper):
            backend = self.start()
        self.assertEqual(backend.omp_isolation["state"], "warning")
        self.assertIn("worker:auth_link:replaced_by_a_file", backend.omp_isolation["warnings"])

    def test_backend_notes_a_natives_extraction_into_the_users_home(self):
        self.user_store()
        backend = self.start()
        notes = json.dumps(backend.omp_isolation["notes"])
        self.assertIn(str(self.home / ".omp" / "natives" / "18.4.5"), notes)
        self.assertEqual(backend.omp_isolation["state"], "ok", "a note, not a warning")
        self.assertFalse((self.home / ".omp" / "natives").exists(), "the backend itself writes nothing there")

    def test_backend_without_a_store_starts_unlinked_with_the_guidance_note(self):
        before = inventory(self.home)
        backend = self.start()
        self.assertFalse(os.path.lexists(self.data / "omp-root" / "agent" / "agent.db"))
        self.assertTrue(any("/login" in note for note in backend.omp_isolation["notes"]))
        self.assertEqual(inventory(self.home), before)


class RefusedStartCreatesNothingTests(HomeFixture):
    """F1 (C-D64 correction): the data dir is validated before anything is created."""

    def run_start(self, data_dir, env=None):
        args = cli.parser().parse_args(["start", "--data-dir", str(data_dir), "--no-attach"])
        stderr = io.StringIO()
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/x/omp", "omp/18.4.5", "/x/bridge.ts")
        with mock.patch.dict(os.environ, env or self.env, clear=True), \
                mock.patch.object(cli, "_running_snapshot", return_value=None) as running, \
                mock.patch.object(cli, "build_plan", return_value=plan), \
                mock.patch.object(cli, "spawn_backend") as spawn, mock.patch("sys.stderr", stderr):
            code = cli.cmd_start(args)
        spawn.assert_not_called()
        return code, stderr.getvalue(), running

    def test_a_data_dir_inside_the_users_omp_dir_is_refused_before_it_is_created(self):
        self.user_store()
        before = inventory(self.home)
        for target in (self.home / ".omp" / "wb", self.home / ".omp" / "agent" / "wb" / "deep"):
            with self.subTest(target=target):
                code, err, running = self.run_start(target)
                self.assertEqual(code, cli.EXIT_REQUIREMENT, err)
                self.assertIn("overlaps", err)
                self.assertFalse(os.path.lexists(target))
                self.assertEqual(inventory(self.home), before, "a refused start created something")
                running.assert_not_called()

    def test_a_data_dir_inside_the_users_xdg_omp_dir_is_refused_before_it_is_created(self):
        xdg = self.root / "xdg"
        (xdg / "omp").mkdir(parents=True)
        (xdg / "omp" / "agent.db").write_bytes(b"\0")
        env = {**self.env, "XDG_DATA_HOME": str(xdg)}
        before = inventory(xdg)
        code, err, _ = self.run_start(xdg / "omp" / "wb", env)
        self.assertEqual(code, cli.EXIT_REQUIREMENT, err)
        self.assertIn("overlaps", err)
        self.assertEqual(inventory(xdg), before)

    def test_any_refused_start_leaves_no_new_data_dir(self):
        target = self.root / "fresh" / "data"
        code, err, _ = self.run_start(target)  # no auth store
        self.assertEqual(code, cli.EXIT_REQUIREMENT, err)
        self.assertIn("/login", err)
        self.assertFalse(os.path.lexists(self.root / "fresh"), "the data dir (or its parent) was created")
        self.assertFalse((self.home / ".omp").exists())


class XdgClassificationTests(HomeFixture):
    """F2: the user's OMP locations come from the user's own environment, not the stripped check env."""

    def setUp(self):
        super().setUp()
        self.xdg_data, self.xdg_state = self.root / "xdg-data", self.root / "xdg-state"
        for base in (self.xdg_data, self.xdg_state):
            (base / "omp").mkdir(parents=True)
        self.store = self.xdg_data / "omp" / "agent.db"
        self.store.write_bytes(b"\0" * 8)
        self.user_env = {**self.env, "XDG_DATA_HOME": str(self.xdg_data), "XDG_STATE_HOME": str(self.xdg_state)}
        self.omp_home = self.prepare(self.user_env)
        self.plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/x/omp", "omp/18.4.5", "/x/b.ts")
        self.check_env = launcher.isolation_check_environment(self.user_env, self.plan, role="worker",
                                                              absent_socket=Path("/d/x.sock"),
                                                              home=self.omp_home.environment())

    def test_the_check_env_withholds_xdg_but_classification_uses_the_users_dirs(self):
        self.assertNotIn("XDG_DATA_HOME", self.check_env)
        self.assertNotIn("XDG_STATE_HOME", self.check_env)
        models = str(self.omp_home.agent_dir / "models.db")
        leaks_in = [str(self.xdg_data / "omp" / "sessions" / "s.jsonl"), str(self.xdg_state / "omp" / "logs" / "x.log")]
        observed, leaks, _ = launcher.home_observation([models, str(self.store), str(self.store) + "-wal", *leaks_in],
                                                       self.check_env, {"providers": []}, "m")
        self.assertEqual(leaks, [f"user_omp_path:{path}" for path in leaks_in])
        self.assertTrue(observed["auth_store_open"])
        self.assertEqual(observed["agent_dir"], os.path.realpath(self.omp_home.agent_dir))

    def test_the_checked_omp_process_never_sees_the_users_withheld_values(self):
        fake = self.root / "omp"
        fake.write_text(FAKE_RPC_OMP.format(python=sys.executable))
        fake.chmod(0o700)
        dump = self.root / "env.json"
        env = {**self.check_env, "FAKE_ENV_OUT": str(dump)}
        launcher.check_isolation([str(fake), "--no-title"], cwd=self.root, environment=env, role="worker",
                                 allowed_skills=(), omp_version="omp/18.4.5", timeout=10)
        seen = json.loads(dump.read_text())
        self.assertEqual([key for key in seen if key.startswith("WORKBENCH_USER_")], [])
        self.assertNotIn(str(self.xdg_data), json.dumps(seen))
        self.assertEqual(seen["PI_CODING_AGENT_DIR"], str(self.omp_home.agent_dir))

    def test_the_pane_env_carries_no_user_xdg_values(self):
        pane = launcher.omp_environment(self.user_env, self.plan, role="worker", token="t",
                                        bridge_socket=Path("/d/b.sock"), home=self.omp_home.environment())
        self.assertNotIn(str(self.xdg_data), json.dumps(pane))


class NativesDirTests(HomeFixture):
    """F3: where OMP 18.4.5 extracts its native addon (fake HOME, incl. an XDG user)."""

    def addon(self, directory: Path) -> None:
        directory.mkdir(parents=True)
        (directory / "pi_natives.linux-x64-modern.node").write_bytes(b"\0")

    def test_omp_natives_dir_rule(self):
        self.assertEqual(omp_home.natives_dir(self.env), self.home / ".omp" / "natives")
        xdg = self.root / "xdg"
        xdg.mkdir()
        env = {**self.env, "XDG_DATA_HOME": str(xdg), "PI_CONFIG_DIR": "../elsewhere"}
        self.assertEqual(omp_home.natives_dir(env), self.home / ".omp" / "natives", "no $XDG_DATA_HOME/omp yet")
        (xdg / "omp").mkdir()
        self.assertEqual(omp_home.natives_dir(env), xdg / "omp" / "natives")

    def test_non_xdg_user_with_the_version_extracted_gets_no_note(self):
        self.user_store()
        self.addon(self.home / ".omp" / "natives" / "18.4.5")
        home = self.prepare()
        before = inventory(self.home)
        omp_env = omp_home.home_environment(self.env, home.environment())
        status = omp_home.natives_status(self.env, omp_env, "omp/18.4.5")
        self.assertEqual((status["dir"], status["present"], status["split"]),
                         (str(self.home / ".omp" / "natives" / "18.4.5"), True, False))
        self.assertIsNone(status["note"])
        self.assertEqual(inventory(self.home), before)

    def test_non_xdg_user_before_running_a_new_omp_version_gets_a_note(self):
        self.user_store()
        self.addon(self.home / ".omp" / "natives" / "18.4.4")
        home = self.prepare()
        omp_env = omp_home.home_environment(self.env, home.environment())
        status = omp_home.natives_status(self.env, omp_env, "omp/18.4.5")
        self.assertFalse(status["present"])
        self.assertIn(str(self.home / ".omp" / "natives" / "18.4.5"), status["note"])
        self.assertIn("extract", status["note"])

    def test_xdg_user_note_names_the_split(self):
        xdg = self.root / "xdg"
        (xdg / "omp").mkdir(parents=True)
        (xdg / "omp" / "agent.db").write_bytes(b"\0")
        self.addon(xdg / "omp" / "natives" / "18.4.5")  # the user's own OMP extracted it here
        env = {**self.env, "XDG_DATA_HOME": str(xdg)}
        home = self.prepare(env)
        before = inventory(self.home)
        omp_env = omp_home.home_environment(env, home.environment())
        status = omp_home.natives_status(env, omp_env, "omp/18.4.5")
        self.assertEqual(status["dir"], str(self.home / ".omp" / "natives" / "18.4.5"))
        self.assertEqual(status["user_dir"], str(xdg / "omp" / "natives" / "18.4.5"))
        self.assertEqual((status["present"], status["split"]), (False, True))
        self.assertIn("XDG_DATA_HOME", status["note"])
        self.assertIn(str(self.home / ".omp" / "natives" / "18.4.5"), status["note"])
        self.assertEqual(inventory(self.home), before, "the check itself writes nothing")
        # the Workbench OMP already has the addon in ~/.omp/natives: nothing new would be written
        self.addon(self.home / ".omp" / "natives" / "18.4.5")
        self.assertIsNone(omp_home.natives_status(env, omp_env, "omp/18.4.5")["note"])

    def test_unknown_version_gives_no_status_note(self):
        self.assertIsNone(omp_home.natives_status(self.env, self.env, None)["note"])


# ----------------------------------------------------------------------- p27-home-senior-03 (C-D64 review)
EVAL_WITH_BROWSER = {"name": "eval", "description": "Preludes:\n- `browser`: Drive real Chromium tabs from "
                     "JavaScript or Python Eval with the global `browser` object. → `xd://eval/browser`"}
EVAL_WITHOUT_BROWSER = {"name": "eval", "description": "Preludes:\n- `tool`: call tools"}


class EnvOverrideDropTests(HomeFixture):
    """F1: OMP 18.4.5 env that points a Workbench OMP back at user config/session/data/db/cache paths."""

    REQUIRED = ("PI_CONFIG_FILES", "PI_CODING_AGENT_SESSION_DIR", "OMP_TEXT_PREDICT_AGENT_DIR",
                "OMP_AUTORESEARCH_DB_DIR")
    AUDITED = ("PI_SESSION_FILE", "PI_ARTIFACTS_DIR", "PI_EVAL_LOCAL_ROOTS", "PI_TOOL_BRIDGE_URL",
               "PI_TOOL_BRIDGE_TOKEN", "PI_TOOL_BRIDGE_SESSION", "OMP_DAEMON_PROJECT_DIR", "OMP_DAEMON_RUNTIME_DIR",
               "OMP_LSP_MUX_SOCKET", "OMP_LSP_MUX_PROJECT_DIR", "OMP_TEXT_PREDICT_SOCKET", "OMP_TINY_WORKER_SOCKET",
               "OMP_BLOB_BROKER_SOCKET", "OMP_BLOB_BROKER_CONFIG", "OMP_IDA_HOST_CONFIG")

    def setUp(self):
        super().setUp()
        self.user_store()
        self.omp_home = self.prepare()
        self.plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/x/omp", "omp/18.4.5", "/x/b.ts")

    def test_each_path_override_is_dropped_from_pane_and_check_env_but_kept_in_the_shell(self):
        for key in (*self.REQUIRED, *self.AUDITED):
            with self.subTest(key=key):
                base = {**self.env, key: str(self.home / ".omp" / "agent" / "x")}
                pane = launcher.omp_environment(base, self.plan, role="worker", token="t",
                                                bridge_socket=Path("/d/b.sock"), home=self.omp_home.environment())
                check = launcher.isolation_check_environment(base, self.plan, role="worker",
                                                             absent_socket=Path("/d/x.sock"),
                                                             home=self.omp_home.environment())
                self.assertNotIn(key, pane)
                self.assertNotIn(key, check)
                self.assertNotIn(key, launcher.omp_environment(base, self.plan, role="worker", token="t",
                                                               bridge_socket=Path("/d/b.sock")))
                self.assertEqual(launcher.shell_environment(base)[key], base[key])

    def test_a_path_override_still_present_in_the_checked_env_is_a_leak(self):
        fake = self.root / "omp"
        fake.write_text(FAKE_RPC_OMP.format(python=sys.executable))
        fake.chmod(0o700)
        models = self.omp_home.agent_dir / "models.db"
        models.write_bytes(b"")
        base = {**self.env, **self.omp_home.environment(), "FAKE_OPEN": str(models)}
        clean = launcher.check_isolation([str(fake), "--no-title"], cwd=self.root, environment=base, role="worker",
                                         allowed_skills=(), omp_version="omp/18.4.5", timeout=10)
        self.assertEqual((clean["state"], clean["leaks"]), ("ok", []))
        for key in (*self.REQUIRED, "OMP_PROFILE", "OMP_WORKTREE_DIR", "PI_SESSION_FILE"):
            with self.subTest(key=key):
                result = launcher.check_isolation([str(fake), "--no-title"], cwd=self.root,
                                                  environment={**base, key: "/u/x"}, role="worker",
                                                  allowed_skills=(), omp_version="omp/18.4.5", timeout=10)
                self.assertEqual(result["state"], "leak")
                self.assertIn(f"env:{key}", result["leaks"])


class BrowserToolTests(HomeFixture):
    """User decision 2026-10-03: OMP's browser capability (18.4.5: the eval `browser` prelude) is off."""

    def test_generated_home_config_and_static_overlay_disable_the_browser(self):
        self.user_store()
        home = self.prepare()
        self.assertEqual(omp_home.home_config("/s", ())["browser"], {"enabled": False})
        self.assertEqual(json.loads(home.config.read_text().split("\n", 2)[2])["browser"], {"enabled": False})
        text = launcher.default_isolation_overlay().read_text()
        self.assertRegex(text, r"(?m)^browser:\n  enabled: false$")
        # other tool settings are untouched (web search/fetch stay as OMP ships them)
        self.assertNotRegex(text, r"(?m)^(web|fetch|search|tools):")
        self.assertEqual(set(omp_home.home_config("/s", ())) & {"web", "fetch", "search", "tools"}, set())

    def test_browser_prelude_or_tool_in_the_checked_omp_is_a_leak(self):
        state = {"systemPrompt": "You are omp's", "dumpTools": [EVAL_WITH_BROWSER]}
        observed = launcher.observe_isolation(state, [])
        self.assertTrue(observed["browser"])
        self.assertIn("tool:browser", launcher.isolation_leaks(observed, allowed_skills=()))
        for tools in ([EVAL_WITHOUT_BROWSER], [{"name": "browser", "description": "x"}]):
            observed = launcher.observe_isolation({"systemPrompt": "You are omp's", "dumpTools": tools}, [])
            self.assertEqual(observed["browser"], tools[0]["name"] == "browser")
        clean = launcher.observe_isolation({"systemPrompt": "You are omp's", "dumpTools": [EVAL_WITHOUT_BROWSER]}, [])
        self.assertNotIn("tool:browser", launcher.isolation_leaks(clean, allowed_skills=()))

    def test_check_reports_the_browser_leak_end_to_end(self):
        self.user_store()
        home = self.prepare()
        fake = self.root / "omp"
        fake.write_text(FAKE_RPC_OMP.format(python=sys.executable))
        fake.chmod(0o700)
        (home.agent_dir / "models.db").write_bytes(b"")
        env = {**self.env, **home.environment(), "FAKE_OPEN": str(home.agent_dir / "models.db")}
        results = {}
        for name, tool in (("on", EVAL_WITH_BROWSER), ("off", EVAL_WITHOUT_BROWSER)):
            results[name] = launcher.check_isolation(
                [str(fake), "--no-title"], cwd=self.root, environment={**env, "FAKE_TOOLS": json.dumps([tool])},
                role="manager", allowed_skills=(), omp_version="omp/18.4.5", timeout=10)  # the worker's eval is a leak itself
        self.assertEqual((results["on"]["state"], results["on"]["leaks"]), ("leak", ["tool:browser"]))
        self.assertEqual((results["off"]["state"], results["off"]["leaks"]), ("ok", []))


class UserStoreResolutionTests(HomeFixture):
    """F3: the user's auth store is resolved the way the user's own omp 18.4.5 resolves it."""

    def store_at(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        store = directory / "agent.db"
        store.write_bytes(b"\0" * 8)
        store.chmod(0)
        self.addCleanup(lambda: store.exists() and store.chmod(0o600))
        return store

    def test_resolution_rules(self):
        h = self.home
        xdg = self.root / "xdg"
        (xdg / "omp" / "profiles" / "work").mkdir(parents=True)
        cases = [
            ({}, h / ".omp" / "agent" / "agent.db"),
            ({"PI_CODING_AGENT_DIR": str(self.root / "own-agent")}, self.root / "own-agent" / "agent.db"),
            ({"PI_CODING_AGENT_DIR": str(self.root / "a") + "/../own"}, self.root / "own" / "agent.db"),
            ({"PI_CONFIG_DIR": ".alt"}, h / ".alt" / "agent" / "agent.db"),
            ({"PI_CONFIG_DIR": "/abs"}, h / "abs" / "agent" / "agent.db"),  # OMP path.join(homedir, value)
            ({"OMP_PROFILE": "work"}, h / ".omp" / "profiles" / "work" / "agent" / "agent.db"),
            ({"PI_PROFILE": "work"}, h / ".omp" / "profiles" / "work" / "agent" / "agent.db"),
            ({"OMP_PROFILE": "", "PI_PROFILE": "work"}, h / ".omp" / "agent" / "agent.db"),
            ({"OMP_PROFILE": "default"}, h / ".omp" / "agent" / "agent.db"),
            ({"OMP_PROFILE": "work", "PI_CONFIG_DIR": ".alt"}, h / ".alt" / "profiles" / "work" / "agent" / "agent.db"),
            ({"OMP_PROFILE": "work", "PI_CODING_AGENT_DIR": "/elsewhere"},
             h / ".omp" / "profiles" / "work" / "agent" / "agent.db"),
            # a stale export of the PI_PROFILE agent dir is ignored by OMP when no profile is active
            ({"OMP_PROFILE": "default", "PI_PROFILE": "work",
              "PI_CODING_AGENT_DIR": str(h / ".omp" / "profiles" / "work" / "agent")}, h / ".omp" / "agent" / "agent.db"),
            ({"XDG_DATA_HOME": str(xdg)}, xdg / "omp" / "agent.db"),
            ({"XDG_DATA_HOME": str(xdg), "PI_CODING_AGENT_DIR": "/own"}, Path("/own/agent.db")),
            ({"XDG_DATA_HOME": str(xdg), "OMP_PROFILE": "work"}, xdg / "omp" / "profiles" / "work" / "agent.db"),
            ({"XDG_DATA_HOME": str(xdg), "OMP_PROFILE": "other"},
             h / ".omp" / "profiles" / "other" / "agent" / "agent.db"),
        ]
        for extra, expected in cases:
            with self.subTest(env=extra):
                self.assertEqual(omp_home.user_auth_store({**self.env, **extra}), expected)

    def test_ambiguous_values_are_refused_naming_the_variable(self):
        for extra, name in (({"PI_CODING_AGENT_DIR": "rel/agent"}, "PI_CODING_AGENT_DIR"),
                            ({"OMP_PROFILE": "Work!"}, "OMP_PROFILE"),
                            ({"PI_PROFILE": "../x"}, "PI_PROFILE"),
                            ({"XDG_DATA_HOME": "rel"}, "XDG_DATA_HOME")):
            with self.subTest(env=extra):
                env = {**self.env, **extra}
                with self.assertRaises(OmpHomeError) as caught:
                    omp_home.user_auth_store(env)
                self.assertIn(name, str(caught.exception))
                with self.assertRaises(StartRequirementError) as start:
                    cli.omp_home_requirements(DataLayout(self.data), env)
                self.assertIn(name, str(start.exception))
                self.assertIn("No backend was started.", str(start.exception))
                with self.assertRaises(OmpHomeError):
                    self.prepare(env)
        self.assertFalse((self.data / "omp-root").exists())

    def test_the_link_targets_the_users_overridden_store_and_the_wb_env_never_gets_the_override(self):
        own = self.store_at(self.root / "own-agent")
        stale = self.user_store()  # ~/.omp/agent/agent.db is not the user's live store here
        env = {**self.env, "PI_CODING_AGENT_DIR": str(self.root / "own-agent")}
        cli.omp_home_requirements(DataLayout(self.data), env)
        home = self.prepare(env)
        self.assertTrue(home.linked)
        self.assertEqual(os.readlink(home.link), str(own))
        self.assertNotEqual(os.readlink(home.link), str(stale))
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/x/omp", "omp/18.4.5", "/x/b.ts")
        pane = launcher.omp_environment(env, plan, role="worker", token="t", bridge_socket=Path("/d/b"),
                                        home=home.environment())
        self.assertEqual(pane["PI_CODING_AGENT_DIR"], str(home.agent_dir))
        self.assertNotIn(str(self.root / "own-agent"), json.dumps(pane))
        # without the user's store at the overridden location, start is refused (no fallback to ~/.omp)
        own.chmod(0o600)
        own.unlink()
        self.assertIn(str(own), omp_home.auth_guidance(env))

    def test_profile_store_is_linked(self):
        store = self.store_at(self.home / ".omp" / "profiles" / "work" / "agent")
        home = self.prepare({**self.env, "OMP_PROFILE": "work"})
        self.assertEqual(os.readlink(home.link), str(store))

    def test_overlap_with_the_users_overridden_agent_dir_is_refused(self):
        own = self.root / "own-agent"
        self.store_at(own)
        env = {**self.env, "PI_CODING_AGENT_DIR": str(own)}
        with self.assertRaisesRegex(OmpHomeError, "overlaps"):
            omp_home.config_dir_value(omp_home.omp_root(own / "wb"), env)
        alt = {**self.env, "PI_CONFIG_DIR": ".alt"}
        with self.assertRaisesRegex(OmpHomeError, "overlaps"):
            omp_home.config_dir_value(omp_home.omp_root(self.home / ".alt" / "wb"), alt)

    def test_classification_uses_the_users_overridden_store_and_dirs(self):
        own = self.root / "own-agent"
        store = self.store_at(own)
        env = {**self.env, "PI_CODING_AGENT_DIR": str(own)}
        home = self.prepare(env)
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/x/omp", "omp/18.4.5", "/x/b.ts")
        check_env = launcher.isolation_check_environment(env, plan, role="worker", absent_socket=Path("/d/x.sock"),
                                                         home=home.environment())
        self.assertEqual(check_env["PI_CODING_AGENT_DIR"], str(home.agent_dir))
        session = str(own / "sessions" / "s.jsonl")
        observed, leaks, _ = launcher.home_observation(
            [str(home.agent_dir / "models.db"), str(store), str(store) + "-wal", session], check_env,
            {"providers": []}, "m")
        self.assertTrue(observed["auth_store_open"])
        self.assertEqual(leaks, [f"user_omp_path:{session}"])
        # the checked OMP itself never receives the user's values
        self.assertEqual(launcher.without_user_values(check_env)["PI_CODING_AGENT_DIR"], str(home.agent_dir))
        self.assertNotIn(str(own), json.dumps(launcher.without_user_values(check_env)))


class EnvKeyStartTests(HomeFixture):
    """F4: no auth store but a provider API key in the environment: start without the link."""

    def test_provider_key_without_store_starts_unlinked_and_keeps_the_key(self):
        env = {**self.env, "ANTHROPIC_API_KEY": "sk-user-own-value"}
        self.assertIsNone(omp_home.auth_guidance(env))
        cli.omp_home_requirements(DataLayout(self.data), env)  # no exception
        before = inventory(self.home)
        home = self.prepare(env)
        self.assertFalse(home.linked)
        self.assertFalse(os.path.lexists(home.link))
        self.assertEqual(inventory(self.home), before)
        notes = " ".join(home.notes)
        self.assertIn("ANTHROPIC_API_KEY", notes)
        self.assertNotIn("sk-user-own-value", notes)
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/x/omp", "omp/18.4.5", "/x/b.ts")
        pane = launcher.omp_environment(env, plan, role="worker", token="t", bridge_socket=Path("/d/b"),
                                        home=home.environment())
        self.assertEqual(pane["ANTHROPIC_API_KEY"], "sk-user-own-value")

    def test_start_passes_with_only_a_provider_key(self):
        args = cli.parser().parse_args(["start", "--data-dir", str(self.data), "--no-attach"])
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/x/omp", "omp/18.4.5", "/x/bridge.ts")
        with mock.patch.dict(os.environ, {**self.env, "OPENAI_API_KEY": "k"}, clear=True), \
                mock.patch.object(cli, "_running_snapshot", return_value=None), \
                mock.patch.object(cli, "build_plan", return_value=plan), \
                mock.patch.object(cli, "spawn_backend", side_effect=RuntimeError("spawned")) as spawn, \
                mock.patch("sys.stderr", io.StringIO()), mock.patch("sys.stdout", io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "spawned"):
                cli.cmd_start(args)
        spawn.assert_called_once()

    def test_empty_or_unrelated_variables_do_not_count_and_guidance_matches(self):
        for extra in ({}, {"ANTHROPIC_API_KEY": ""}, {"GITHUB_TOKEN": "x"}, {"AWS_PROFILE": "x"}):
            with self.subTest(env=extra):
                guidance = omp_home.auth_guidance({**self.env, **extra})
                self.assertIsNotNone(guidance)
                for needle in ("'omp'", "/login", "API key", "ANTHROPIC_API_KEY", "without", "No"):
                    self.assertIn(needle, guidance)
        # an unusable (not missing) store is still refused even with a key
        agent = self.home / ".omp" / "agent"
        agent.mkdir(parents=True)
        (agent / "agent.db").symlink_to(self.root / "real.db")
        self.assertIn("symlink", omp_home.auth_guidance({**self.env, "OPENAI_API_KEY": "k"}))


class XdgBashToolNoteTests(HomeFixture):
    """F5: XDG users are told that commands the OMP bash tool runs do not get their XDG_* values."""

    def test_note_when_xdg_values_are_withheld(self):
        xdg = self.root / "xdg"
        (xdg / "omp").mkdir(parents=True)
        (xdg / "omp" / "agent.db").write_bytes(b"\0")
        home = self.prepare({**self.env, "XDG_DATA_HOME": str(xdg)})
        notes = " ".join(home.notes)
        self.assertIn("XDG_DATA_HOME", notes)
        self.assertIn("bash tool", notes)
        self.user_store()
        self.assertFalse(any("bash tool" in note for note in self.prepare().notes))

    def test_isolation_overlay_documents_the_bash_tool_xdg_behaviour(self):
        text = launcher.default_isolation_overlay().read_text()
        self.assertIn("bash tool", text)
        self.assertIn("XDG_", text)


if __name__ == "__main__":
    unittest.main()
