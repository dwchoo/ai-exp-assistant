"""Independent C-D64 re-verification tests (p27-home-test-03, unit V-CW-17-p2.7-home): the review findings.

Expectations were drafted from result-p27-home-review-01-agent.json (F1..F5), the Root adjudication
root-adjudication-p27-home-profile-fixture.json, the senior's resolution list (result-p27-home-senior-03.json),
C-D64 and the user choice "no browser automation" (2026-10-03) before omp_home.py was read again:

- F1 every OMP env that points a Workbench OMP back at user config/session/db/cache locations (the review's four plus
  the senior's audited extras and the earlier path overrides, profiles included) is absent from the pane AND the
  isolation-check environment of both roles, from every OMP process the real backend starts, still reaches the host
  shell, and is reported as a leak (``env:<KEY>``) when it is injected into the checked OMP environment anyway.
  Provider keys and HOME/PATH stay.
- Browser: no ``browser`` tool in the static overlay, the generated home config, the role overlay or the OMP argv;
  an OMP that reports a browser tool / the eval ``browser`` prelude, or one that a ``--config`` re-enables
  (``browser.enabled: true``), is a ``tool:browser`` leak.
- F3 the user's auth store is the one the user's own omp 18.4.5 uses: PI_CONFIG_DIR (relative to HOME, ``||``),
  profile (OMP_PROFILE else PI_PROFILE, "" and "default" mean none, an invalid name is refused), absolute
  PI_CODING_AGENT_DIR (relative refused), XDG_DATA_HOME/omp[/profiles/<p>] when it exists. Values that depend on the
  user's cwd or that OMP rejects are refused with a message naming the variable, creating nothing anywhere.
- F4 no auth store + a provider key variable (non-empty) starts without any link and creates nothing in the user's
  home; the key VALUE is never logged/printed/stored by Workbench (names only) but still reaches the OMPs.

No real OMP, no model call, no real credential: every HOME is a temp dir under /tmp holding a fake ``agent.db``
(non-credential bytes), every API key value is a dummy sentinel. Processes the tests start are found by
cmdline/cwd only (never by reading another process' environ) and killed by exact pid+start identity.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import test_omp_home_independent_p27u as p27u  # noqa: E402
import test_omp_isolation_independent_p27m as p27m  # noqa: E402
from workbench.backend import cli, launcher, omp_home  # noqa: E402
from workbench.backend.launcher import StartRequirementError  # noqa: E402
from workbench.backend.omp_home import OmpHomeError  # noqa: E402

REPO = p27m.REPO
SKILLS_DIR = REPO / "omp_bridge" / "skills"
FAKE = p27m.FAKE_AUTH_BYTES
inventory, plan_for = p27u.inventory, p27u.plan_for

# F1: the review's four ...
REVIEW_ENV = ("PI_CONFIG_FILES", "PI_CODING_AGENT_SESSION_DIR", "OMP_TEXT_PREDICT_AGENT_DIR", "OMP_AUTORESEARCH_DB_DIR")
# ... the senior's audited extra list (result-p27-home-senior-03 ``extra_dropped_env``) ...
SENIOR_ENV = ("PI_SESSION_FILE", "PI_ARTIFACTS_DIR", "PI_EVAL_LOCAL_ROOTS", "PI_TOOL_BRIDGE_URL", "PI_TOOL_BRIDGE_TOKEN",
              "PI_TOOL_BRIDGE_SESSION", "OMP_DAEMON_PROJECT_DIR", "OMP_DAEMON_RUNTIME_DIR", "OMP_LSP_MUX_SOCKET",
              "OMP_LSP_MUX_PROJECT_DIR", "OMP_TEXT_PREDICT_SOCKET", "OMP_TINY_WORKER_SOCKET", "OMP_BLOB_BROKER_SOCKET",
              "OMP_BLOB_BROKER_CONFIG", "OMP_IDA_HOST_CONFIG")
# ... the earlier path overrides and the profile selectors.
EARLIER_ENV = ("OMP_WORKTREE_DIR", "OMP_GITHUB_CACHE_DB", "OMP_COMMIT_CACHE_DB", "OMP_JUDGMENT_CACHE_DB",
               "OMP_AUTH_BROKER_SNAPSHOT_CACHE", "OMP_PROFILE", "PI_PROFILE", "PI_AUTO_QA")
REDIRECT_ENV = REVIEW_ENV + SENIOR_ENV + EARLIER_ENV
LOCATION_ENV = ("PI_CONFIG_DIR", "PI_CODING_AGENT_DIR")  # replaced by the Workbench home values
PROVIDER_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "XAI_API_KEY")
SENTINEL = "/tmp/p27v-user-sentinel"


def dummy(key: str) -> str:
    return f"dummy-p27v-{key.lower()}-{os.getpid()}"


def user_env(home: Path, **extra) -> dict[str, str]:
    base = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8", "OPENAI_API_KEY": dummy("OPENAI_API_KEY"),
            "UNRELATED_USER_VAR": "keep-me"}
    base.update({key: f"{SENTINEL}/{key}" for key in REDIRECT_ENV})
    base.update(extra)
    return base


# ================================================================================================= F1 (unit)
class RedirectEnvUnitTests(p27u._Tmp):
    def setUp(self):
        super().setUp()
        self.store()
        self.data = self.root / "data"
        self.values = self.prepare(self.data).environment()
        self.base = user_env(self.home, PI_CONFIG_DIR=".user-cfg", PI_CODING_AGENT_DIR=str(self.home / "user-agent"))
        self.frozen = dict(self.base)

    def envs(self, role):
        pane = launcher.omp_environment(self.base, plan_for(), role=role, token="t", bridge_socket=Path("/x/b.sock"),
                                        home=self.values)
        check = launcher.isolation_check_environment(self.base, plan_for(), role=role, absent_socket=Path("/x/a"),
                                                     home=self.values)
        return pane, check

    def test_no_redirect_env_reaches_the_pane_or_the_check_of_either_role(self):
        for role in ("manager", "worker"):
            pane, check = self.envs(role)
            for label, env in (("pane", pane), ("check", check)):
                for key in REDIRECT_ENV:
                    with self.subTest(role=role, env=label, key=key):
                        self.assertNotIn(key, env)
                # WORKBENCH_USER_* (check env only, stripped before the OMP is spawned) may carry the user's values
                visible = "\n".join(v for k, v in env.items() if not k.startswith(omp_home.USER_ENV_PREFIX))
                self.assertNotIn(SENTINEL, visible, f"{role} {label}: a user redirect value leaked")
                p27m.assert_workbench_home_env(self, env, self.home, self.data)
                for key in LOCATION_ENV:  # replaced, never the user's value
                    self.assertNotEqual(env[key], self.base[key])
                self.assertEqual(env["HOME"], str(self.home))
                self.assertEqual(env["PATH"], "/usr/bin:/bin")
                self.assertEqual(env["OPENAI_API_KEY"], self.base["OPENAI_API_KEY"], "provider keys pass through")
                self.assertEqual(env["UNRELATED_USER_VAR"], "keep-me", "unrelated variables are not dropped")
        self.assertEqual(self.base, self.frozen, "the caller's mapping was edited")

    def test_the_pane_carries_no_user_values_at_all_and_the_check_only_the_location_keys(self):
        pane, check = self.envs("worker")
        self.assertEqual([k for k in pane if k.startswith(omp_home.USER_ENV_PREFIX)], [])
        allowed = {omp_home.USER_ENV_PREFIX + key for key in (*omp_home.XDG_DIR_KEYS, *LOCATION_ENV, "OMP_PROFILE",
                                                             "PI_PROFILE")}
        extra = [k for k in check if k.startswith(omp_home.USER_ENV_PREFIX) and k not in allowed]
        self.assertEqual(extra, [], "the check environment carries user values beyond the location variables")
        for key in (*REVIEW_ENV, *SENIOR_ENV, *EARLIER_ENV[:5]):
            self.assertNotIn(omp_home.USER_ENV_PREFIX + key, check)

    def test_the_host_shell_keeps_every_user_variable(self):
        shell = launcher.shell_environment(self.base)
        for key in (*REDIRECT_ENV, *LOCATION_ENV):
            with self.subTest(key=key):
                self.assertEqual(shell.get(key), self.base[key], f"the host shell lost the user's {key}")

    def test_an_injected_redirect_variable_is_a_leak_in_the_check_and_a_clean_check_is_not(self):
        pane_env = self.envs("worker")[1]
        rpc = p27u._HomeRpc(self, self.prepare(self.data), {**os.environ, **pane_env})
        clean = rpc.check()
        self.assertEqual((clean["state"], clean["leaks"]), ("ok", []), clean)
        # PI_AUTO_QA is dropped like the others but is detected by its prompt marker (C-D59), not by name
        for key in (k for k in REDIRECT_ENV if k != "PI_AUTO_QA"):
            with self.subTest(key=key):
                rpc.env = {**os.environ, **pane_env, "P27U_MODE": str(rpc.mode), "P27U_LOG": str(rpc.log),
                           key: f"{SENTINEL}/{key}"}
                result = rpc.check()
                self.assertEqual(result["state"], "leak", result)
                self.assertFalse(result["ok"])
                self.assertIn(f"env:{key}", result["leaks"])
        zero_model = {r["type"] for r in rpc.records() if r["event"] == "rpc"}
        self.assertEqual(zero_model, {"get_state", "get_available_commands", "get_login_providers"})

    def test_the_check_watches_the_users_redirected_dirs(self):
        # F3 x isolation check: the user's own PI_CONFIG_DIR location is a user dir, its agent.db the shared store
        base = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "PI_CONFIG_DIR": ".cfg"}
        store = self.home / ".cfg" / "agent" / "agent.db"
        store.parent.mkdir(parents=True)
        store.write_bytes(FAKE)
        home = self.prepare(self.root / "data2", base)
        check = launcher.isolation_check_environment(base, plan_for(), role="worker", absent_socket=Path("/x/a"),
                                                     home=home.environment())
        wb = str(home.agent_dir / "models.db")
        login = {"providers": [{"id": "x", "authenticated": True}]}
        _, leaks, _ = launcher.home_observation([wb, str(store)], check, login, "x/m")
        self.assertEqual(leaks, [], "the shared store itself is not a leak")
        _, leaks, _ = launcher.home_observation([wb, str(store.parent / "sessions" / "s.jsonl")], check, login, "x/m")
        self.assertEqual(len(leaks), 1, leaks)
        self.assertIn(str(store.parent / "sessions" / "s.jsonl"), leaks[0])


# ================================================================================================= browser (unit)
def _json_after_header(path: Path) -> dict:
    return json.loads("".join(line for line in path.read_text().splitlines(True) if not line.startswith("#")))


class BrowserUnitTests(p27u._Tmp):
    def test_no_browser_in_the_static_overlay_the_home_config_the_role_overlay_or_the_command(self):
        static = p27m.parse_yaml_subset(p27m.STATIC_OVERLAY.read_text())
        self.assertIs(static["browser"]["enabled"], False)
        self.assertIs(omp_home.home_config(SKILLS_DIR, ["a"])["browser"]["enabled"], False)
        self.store()
        home = self.prepare(self.root / "data")
        self.assertIs(_json_after_header(home.config)["browser"]["enabled"], False)
        # the role overlay content as written for a real start is checked end-to-end in BrowserEntrypointTests
        command = launcher.omp_command(plan_for(), "/x/role.yml")
        self.assertEqual([item for item in command if "browser" in item.lower()], [], command)

    def _state(self, tools=None, prompt="You are omp's x\n"):
        return {"model": {"provider": "p", "id": "m"}, "systemPrompt": prompt,
                "dumpTools": tools if tools is not None else [{"name": "read", "description": "r"}]}

    def leaks(self, state):
        observed = launcher.observe_isolation(state, [{"name": "init", "source": "builtin"}])
        return launcher.isolation_leaks(observed, allowed_skills=())

    def test_a_browser_tool_or_the_eval_browser_prelude_is_a_leak(self):
        self.assertNotIn("tool:browser", self.leaks(self._state()))
        self.assertNotIn("tool:browser", self.leaks(self._state([{"name": "read", "description": "r"},
                                                                 {"name": "eval", "description": "python and js"}])))
        self.assertIn("tool:browser", self.leaks(self._state([{"name": "browser", "description": "drive a page"}])))
        self.assertIn("tool:browser", self.leaks(self._state([{"name": "eval",
                                                               "description": "preludes: xd://eval/browser"}])))
        self.assertIn("tool:browser", self.leaks(self._state(prompt="You are omp's x\nuse `xd://eval/browser`\n")))

    def test_check_isolation_reports_a_browser_tool_from_a_running_omp(self):
        self.store()
        home = self.prepare(self.root / "data")
        env = {**os.environ, **launcher.isolation_check_environment(self.env, plan_for(), role="worker",
                                                                    absent_socket=Path("/x/a"),
                                                                    home=home.environment())}
        rpc = p27u._HomeRpc(self, home, env)
        script = rpc.script.read_text().replace(
            '"dumpTools": [{"name": "read", "description": "r"}]',
            '"dumpTools": [{"name": "read", "description": "r"}, {"name": "eval", "description": "xd://eval/browser"}]')
        rpc.script.write_text(script)
        result = rpc.check()
        self.assertEqual(result["state"], "leak", result)
        self.assertIn("tool:browser", result["leaks"])


# ================================================================================================= F3 matrix
REFUSALS = [
    ("PI_CODING_AGENT_DIR", {"PI_CODING_AGENT_DIR": "rel/agent"}),
    ("PI_CODING_AGENT_DIR", {"PI_CODING_AGENT_DIR": "./agent"}),
    ("PI_CODING_AGENT_DIR", {"PI_CODING_AGENT_DIR": "agent", "PI_CONFIG_DIR": ".cfg"}),
    ("OMP_PROFILE", {"OMP_PROFILE": "Work"}),
    ("OMP_PROFILE", {"OMP_PROFILE": "a/b"}),
    ("OMP_PROFILE", {"OMP_PROFILE": ".."}),
    ("OMP_PROFILE", {"OMP_PROFILE": "."}),
    ("OMP_PROFILE", {"OMP_PROFILE": "x."}),
    ("OMP_PROFILE", {"OMP_PROFILE": "con"}),
    ("OMP_PROFILE", {"OMP_PROFILE": "bad name"}),
    ("PI_PROFILE", {"PI_PROFILE": "Bad Name"}),
    ("PI_PROFILE", {"PI_PROFILE": "../x"}),
    ("XDG_DATA_HOME", {"XDG_DATA_HOME": "relative/xdg"}),
]


class UserStoreMatrixTests(p27u._Tmp):
    """Each row: user env builder -> the store the user's own omp 18.4.5 uses (relative to HOME or absolute)."""

    def cases(self):
        h, t = self.home, self.root
        xdg = t / "xdg"
        xdg_p = t / "xdg-prof"
        (xdg / "omp").mkdir(parents=True)
        (xdg_p / "omp" / "profiles" / "p").mkdir(parents=True)
        (t / "xdg-noprof" / "omp").mkdir(parents=True)
        (t / "xdg-absent").mkdir()
        agent = t / "own-agent"
        d = h / ".omp" / "agent" / "agent.db"
        return [
            ("default", {}, d),
            ("PI_CONFIG_DIR relative", {"PI_CONFIG_DIR": ".cfg"}, h / ".cfg" / "agent" / "agent.db"),
            ("PI_CONFIG_DIR nested", {"PI_CONFIG_DIR": "a/b"}, h / "a" / "b" / "agent" / "agent.db"),
            ("PI_CONFIG_DIR absolute is joined to HOME", {"PI_CONFIG_DIR": "/abs/cfg"},
             h / "abs" / "cfg" / "agent" / "agent.db"),
            ("PI_CONFIG_DIR empty", {"PI_CONFIG_DIR": ""}, d),
            ("OMP_PROFILE", {"OMP_PROFILE": "work"}, h / ".omp" / "profiles" / "work" / "agent" / "agent.db"),
            ("PI_PROFILE", {"PI_PROFILE": "work"}, h / ".omp" / "profiles" / "work" / "agent" / "agent.db"),
            ("OMP_PROFILE wins over PI_PROFILE", {"OMP_PROFILE": "a1", "PI_PROFILE": "b2"},
             h / ".omp" / "profiles" / "a1" / "agent" / "agent.db"),
            ("OMP_PROFILE empty overrides PI_PROFILE", {"OMP_PROFILE": "", "PI_PROFILE": "work"}, d),
            ("OMP_PROFILE default", {"OMP_PROFILE": "default"}, d),
            ("PI_PROFILE default", {"PI_PROFILE": "default"}, d),
            ("PI_PROFILE empty", {"PI_PROFILE": ""}, d),
            ("OMP_PROFILE default overrides PI_PROFILE", {"OMP_PROFILE": "default", "PI_PROFILE": "work"}, d),
            ("OMP_PROFILE valid shadows an invalid PI_PROFILE", {"OMP_PROFILE": "work", "PI_PROFILE": "Bad Name"},
             h / ".omp" / "profiles" / "work" / "agent" / "agent.db"),
            ("profile under PI_CONFIG_DIR", {"PI_CONFIG_DIR": ".cfg", "OMP_PROFILE": "work"},
             h / ".cfg" / "profiles" / "work" / "agent" / "agent.db"),
            ("absolute PI_CODING_AGENT_DIR", {"PI_CODING_AGENT_DIR": str(agent)}, agent / "agent.db"),
            ("empty PI_CODING_AGENT_DIR", {"PI_CODING_AGENT_DIR": ""}, d),
            ("PI_CODING_AGENT_DIR with PI_CONFIG_DIR", {"PI_CONFIG_DIR": ".cfg", "PI_CODING_AGENT_DIR": str(agent)},
             agent / "agent.db"),
            ("profile wins over PI_CODING_AGENT_DIR", {"OMP_PROFILE": "work", "PI_CODING_AGENT_DIR": str(agent)},
             h / ".omp" / "profiles" / "work" / "agent" / "agent.db"),
            ("XDG_DATA_HOME/omp exists", {"XDG_DATA_HOME": str(xdg)}, xdg / "omp" / "agent.db"),
            ("XDG_DATA_HOME without omp dir", {"XDG_DATA_HOME": str(t / "xdg-absent")}, d),
            ("XDG_DATA_HOME/omp with PI_CONFIG_DIR", {"XDG_DATA_HOME": str(xdg), "PI_CONFIG_DIR": ".cfg"},
             xdg / "omp" / "agent.db"),
            ("XDG_DATA_HOME/omp/profiles/p", {"XDG_DATA_HOME": str(xdg_p), "OMP_PROFILE": "p"},
             xdg_p / "omp" / "profiles" / "p" / "agent.db"),
            ("XDG_DATA_HOME/omp without the profile dir", {"XDG_DATA_HOME": str(t / "xdg-noprof"), "OMP_PROFILE": "p"},
             h / ".omp" / "profiles" / "p" / "agent" / "agent.db"),
            ("XDG_DATA_HOME ignored with PI_CODING_AGENT_DIR", {"XDG_DATA_HOME": str(xdg),
                                                                "PI_CODING_AGENT_DIR": str(agent)}, agent / "agent.db"),
        ]

    def env_for(self, extra):
        return {"HOME": str(self.home), "PATH": "/usr/bin:/bin", **extra}

    def test_the_store_is_the_one_the_users_own_omp_uses(self):
        for name, extra, expected in self.cases():
            with self.subTest(name):
                self.assertEqual(omp_home.user_auth_store(self.env_for(extra)), expected)

    def test_prepare_links_exactly_that_store_and_never_the_default_one(self):
        decoy = self.store()  # ~/.omp/agent/agent.db: a stale store the user's omp does not use for these envs
        decoy.write_bytes(b"decoy-p27v\n")
        for index, (name, extra, expected) in enumerate(self.cases()):
            if expected == decoy:
                continue
            with self.subTest(name):
                expected.parent.mkdir(parents=True, exist_ok=True)
                expected.write_bytes(FAKE)
                env = self.env_for(extra)
                data = self.root / f"data-{index}"
                home = self.prepare(data, env)
                self.assertTrue(home.linked, home.notes)
                self.assertEqual(os.readlink(home.link), str(expected))
                self.assertNotEqual(os.readlink(home.link), str(decoy))
                self.assertEqual(decoy.read_bytes(), b"decoy-p27v\n")
                self.assertIsNone(omp_home.auth_guidance(env))
                self.assertEqual(omp_home.verify_omp_home(home), [])
                expected.unlink()

    def test_the_default_store_alone_does_not_satisfy_a_redirecting_env(self):
        decoy = self.store()
        for index, (name, extra, expected) in enumerate(self.cases()):
            if expected == decoy:
                continue
            with self.subTest(name):
                self.assertFalse(os.path.lexists(expected))
                env = self.env_for(extra)
                guidance = omp_home.auth_guidance(env)
                self.assertIsNotNone(guidance, "the stale default store was accepted")
                self.assertIn(str(expected), guidance)
                self.assertNotIn(str(decoy), guidance)
                data = self.root / f"data-nostore-{index}"
                home = self.prepare(data, env)
                self.assertFalse(home.linked)
                self.assertFalse(os.path.lexists(home.link), "agent.db was created without a user store")

    def test_ambiguous_or_rejected_values_are_refused_naming_the_variable_and_nothing_is_created(self):
        self.store()
        for index, (key, extra) in enumerate(REFUSALS):
            with self.subTest(key=key, extra=extra):
                env = self.env_for(extra)
                before = inventory(self.home)
                data = self.root / f"data-refused-{index}"
                with self.assertRaises(OmpHomeError) as caught:
                    omp_home.user_auth_store(env)
                self.assertIn(key, str(caught.exception))
                with self.assertRaises(OmpHomeError) as prepared:
                    self.prepare(data, env)
                self.assertIn(key, str(prepared.exception))
                with self.assertRaises(StartRequirementError) as required:
                    cli.omp_home_requirements(_layout(data), env)
                self.assertIn(key, str(required.exception))
                self.assertIn("No backend was started", str(required.exception))
                self.assertEqual(inventory(self.home), before, "a refusal changed the user's home")
                self.assertFalse(os.path.lexists(data), "a refusal created the data dir")


def _layout(data: Path):
    from workbench.backend.paths import DataLayout
    return DataLayout(Path(data))


class _CmdlineBase(p27m.RealEntrypointBase):
    """RealEntrypointBase that finds its processes by cmdline/cwd only (never another process' environ)."""

    def setUp(self):
        patcher = mock.patch.object(p27m, "procs_naming", _procs_naming)
        patcher.start()
        self.addCleanup(patcher.stop)  # registered first: runs last (stop_backend still needs the patch)
        self.addCleanup(self._no_leaks)  # runs after stop_backend (LIFO)
        super().setUp()

    def _no_leaks(self):
        self.assertEqual(self.leaked, [], "processes survived the normal shutdown path")

    def refusal_outputs_nothing(self, out, key=None):
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("No backend was started", out.stderr)
        if key:
            self.assertIn(key, out.stderr)
        self.assertEqual([c for c in self.calls() if c.get("event") == "start"], [])
        self.assertFalse(os.path.lexists(self.data), "a refused start created the data dir")
        # (no `status` call here: querying a missing data dir would create it)


# Replace procs_naming (reads cmdline AND environ of every process) with cmdline + cwd only.
def _procs_naming(root: Path) -> dict[int, str]:
    needle, found = str(root).encode(), {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            blob = (entry / "cmdline").read_bytes()
            cwd = os.readlink(entry / "cwd")
        except OSError:
            continue
        if (needle in blob or cwd.startswith(str(root))) and (ticks := p27m.identity(int(entry.name))):
            found[int(entry.name)] = ticks
    return found




class F3EntrypointTests(_CmdlineBase):
    def test_refusals_through_the_real_entrypoint_name_the_variable_and_create_nothing(self):
        for key, extra in REFUSALS:
            with self.subTest(key=key, extra=extra):
                before = p27u.inventory(self.home)
                out = self.start(env_extra=extra)
                self.refusal_outputs_nothing(out, key)
                self.assertEqual(p27u.inventory(self.home), before, "the user's home changed")

    def test_an_absolute_pi_coding_agent_dir_store_is_linked_end_to_end(self):
        own = self.root / "own-agent"
        own.mkdir()
        (own / "agent.db").write_bytes(FAKE)
        (own / "agent.db").chmod(0o600)
        decoy_before = p27u.inventory(self.home / ".omp")
        out = self.start(env_extra={"PI_CODING_AGENT_DIR": str(own)})
        self.assertNotEqual(out.returncode, 2, out.stdout + out.stderr)
        self.wait_isolation()
        link = self.data / "omp-root" / "agent" / "agent.db"
        self.assertEqual(os.readlink(link), str(own / "agent.db"))
        for call in self.by_kind("pane") + self.by_kind("rpc"):
            p27m.assert_workbench_home_env(self, call["env"], self.home, self.data)
        self.assertEqual(p27u.inventory(self.home / ".omp"), decoy_before, "the default ~/.omp was touched")


# ================================================================================================= e2e wrapper OMP
WRAPPER = r'''#!/usr/bin/env python3
import json, os, re, sys, time
ROOT = {root!r}
argv = sys.argv[1:]
real = ROOT + "/omp-real"
if "--version" in argv or argv[:2] == ["config", "get"]:
    os.execv(real, [real] + argv)
rpc = "--mode" in argv and "rpc" in argv
stamp = "%d.%d" % (os.getpid(), time.time_ns())
with open(ROOT + "/dumps/" + stamp + ".json", "w") as handle:
    json.dump({{"kind": "rpc" if rpc else "pane", "argv": argv, "env": dict(os.environ)}}, handle)
if not rpc:
    os.execv(real, [real] + argv)

def enabled(text):
    try:
        return json.loads(text).get("browser", {{}}).get("enabled")
    except ValueError:
        found = re.search(r"^browser:\s*\n\s+enabled:\s*(true|false)", text, re.M)
        return None if not found else found.group(1) == "true"

order = [os.path.join(os.environ.get("PI_CODING_AGENT_DIR", "/nonexistent"), "config.yml")]
order += [argv[i + 1] for i, item in enumerate(argv) if item == "--config" and i + 1 < len(argv)]
browser = False
for path in order:
    try:
        value = enabled(open(path).read())
    except OSError:
        continue
    if value is not None:
        browser = value
held = open(os.path.join(os.environ["PI_CODING_AGENT_DIR"], "models.db"), "ab")
tools = [{{"name": "read", "description": "r"}},
         {{"name": "eval", "description": "run code" + (" preludes: xd://eval/browser" if browser else "")}}]
state = {{"model": {{"provider": "stubprov", "id": "m1"}}, "systemPrompt": "You are omp's trusted coding assistant.\n",
         "dumpTools": tools}}
for line in sys.stdin:
    try:
        req = json.loads(line)
    except ValueError:
        continue
    data = (state if req["type"] == "get_state" else {{"commands": [{{"name": "init", "source": "builtin"}}]}}
            if req["type"] == "get_available_commands" else
            {{"providers": [{{"id": "stubprov", "authenticated": True}}]}} if req["type"] == "get_login_providers" else {{}})
    sys.stdout.write(json.dumps({{"id": req["id"], "type": "response", "command": req["type"], "success": True,
                                 "data": data}}) + "\n")
    sys.stdout.flush()
'''


class _WrapperBase(_CmdlineBase):
    def setUp(self):
        super().setUp()
        (self.root / "omp-real").write_text((self.root / "omp").read_text())
        (self.root / "omp-real").chmod(0o755)
        (self.root / "dumps").mkdir()
        (self.root / "omp").write_text(WRAPPER.format(root=str(self.root)))
        (self.root / "omp").chmod(0o755)

    def dumps(self) -> list[dict]:
        out = [json.loads(path.read_text()) for path in sorted((self.root / "dumps").iterdir())]
        for item in out:
            item["role"] = item["env"].get("WORKBENCH_G3_ROLE")
        return [item for item in out if item["role"]]

    def started(self):
        envs = self.dumps()
        self.assertEqual(sorted((d["role"], d["kind"]) for d in envs),
                         [("manager", "pane"), ("manager", "rpc"), ("worker", "pane"), ("worker", "rpc")])
        return envs


class RedirectEnvEntrypointTests(_WrapperBase):
    def test_every_omp_process_the_backend_starts_is_free_of_the_redirect_env(self):
        extra = {key: f"{self.root}/user-sentinel/{key}" for key in REDIRECT_ENV}
        # valid profile selectors (the user's own omp keeps its store under the profile): store planted there
        profile_store = self.home / ".omp" / "profiles" / "p27v" / "agent" / "agent.db"
        profile_store.parent.mkdir(parents=True)
        profile_store.write_bytes(FAKE)
        extra.update({"OMP_PROFILE": "p27v", "PI_PROFILE": "p27v"})
        extra["ANTHROPIC_API_KEY"] = dummy("ANTHROPIC_API_KEY")
        out = self.start(env_extra=extra)
        self.assertNotEqual(out.returncode, 2, out.stdout + out.stderr)
        snapshot = self.wait_isolation()
        iso = snapshot["omp_isolation"]
        self.assertEqual((iso["state"], iso["leaks"]), ("ok", []), iso)
        for item in self.started():
            env = item["env"]
            label = f"{item['role']} {item['kind']}"
            for key in REDIRECT_ENV:
                self.assertNotIn(key, env, f"{label}: {key} reached OMP")
            self.assertEqual([k for k in env if k.startswith("WORKBENCH_USER_")], [], label)
            self.assertNotIn(f"{self.root}/user-sentinel", "\n".join(env.values()), label)
            p27m.assert_workbench_home_env(self, env, self.home, self.data)
            self.assertEqual(env["ANTHROPIC_API_KEY"], extra["ANTHROPIC_API_KEY"], "provider keys pass through")
            self.assertEqual(env["HOME"], str(self.home))
        self.assertFalse(os.path.lexists(self.root / "user-sentinel"), "something was created at a user redirect path")
        self.assertEqual(os.readlink(self.data / "omp-root" / "agent" / "agent.db"), str(profile_store))

    def test_a_user_pi_config_dir_with_the_other_redirects_is_handled_together(self):
        user_store = self.home / ".cfg" / "agent" / "agent.db"
        user_store.parent.mkdir(parents=True)
        user_store.write_bytes(FAKE)
        extra = {"PI_CONFIG_DIR": ".cfg", "PI_CONFIG_FILES": str(self.home / ".cfg" / "extra.yml"),
                 "PI_CODING_AGENT_SESSION_DIR": str(self.home / ".cfg" / "agent" / "sessions")}
        out = self.start(env_extra=extra)
        self.assertNotEqual(out.returncode, 2, out.stdout + out.stderr)
        iso = self.wait_isolation()["omp_isolation"]
        self.assertEqual((iso["state"], iso["leaks"]), ("ok", []), iso)
        for item in self.started():
            self.assertNotIn("PI_CONFIG_FILES", item["env"])
            self.assertNotIn("PI_CODING_AGENT_SESSION_DIR", item["env"])
            p27m.assert_workbench_home_env(self, item["env"], self.home, self.data)
        self.assertEqual(sorted(p.name for p in (self.home / ".cfg" / "agent").iterdir()), ["agent.db"],
                         "something was created in the user's redirected OMP dir")


class BrowserEntrypointTests(_WrapperBase):
    def test_a_default_start_has_no_browser_in_config_argv_or_tools(self):
        out = self.start()
        self.assertNotEqual(out.returncode, 2, out.stdout + out.stderr)
        iso = self.wait_isolation()["omp_isolation"]
        self.assertEqual((iso["state"], iso["leaks"]), ("ok", []), iso)
        config = self.data / "omp-root" / "agent" / "config.yml"
        self.assertIs(_json_after_header(config)["browser"]["enabled"], False)
        for item in self.started():
            self.assertEqual([a for a in item["argv"] if "browser" in a.lower()], [], item["argv"])
            argv = item["argv"]
            for path in [argv[i + 1] for i, a in enumerate(argv) if a == "--config"]:
                if path.endswith(".json") or "role" in path or "omp-isolation-" in path:
                    try:
                        text = Path(path).read_text()
                    except OSError:
                        continue
                    self.assertIsNot((json.loads(text).get("browser") or {}).get("enabled"), True, path)
        self.assertFalse(os.path.lexists(self.home / ".omp" / "browser-state"))

    def test_an_injected_browser_enabled_true_is_reported_as_a_leak(self):
        user_config = self.project / "user-browser.json"
        user_config.write_text(json.dumps({"browser": {"enabled": True}}))
        out = self.start("--omp-arg=--config", f"--omp-arg={user_config}")
        self.assertNotEqual(out.returncode, 2, out.stdout + out.stderr)
        iso = self.wait_isolation()["omp_isolation"]
        self.assertEqual(iso["state"], "leak", iso)
        self.assertFalse(iso["ok"])
        self.assertTrue(any("tool:browser" in leak for leak in iso["leaks"]), iso["leaks"])
        self.assertFalse(os.path.lexists(self.home / ".omp" / "browser-state"))


# ================================================================================================= F4
class KeyOnlyUnitTests(p27u._Tmp):
    def test_names_never_values_and_only_non_empty_provider_keys_count(self):
        env = {"ANTHROPIC_API_KEY": dummy("a"), "OPENAI_API_KEY": "", "UNRELATED_API_KEY": "x"}
        self.assertEqual(omp_home.provider_key_names(env), ["ANTHROPIC_API_KEY"])
        self.assertEqual(omp_home.provider_key_names({}), [])

    def test_no_store_with_a_provider_key_starts_without_a_link_and_creates_nothing(self):
        for key in PROVIDER_KEYS:
            with self.subTest(key=key):
                env = {**self.env, key: dummy(key)}
                self.assertFalse(os.path.lexists(self.home / ".omp"))
                before = inventory(self.home)
                self.assertIsNone(omp_home.auth_guidance(env))
                home = self.prepare(self.root / f"data-{key}", env)
                self.assertFalse(home.linked)
                self.assertFalse(os.path.lexists(home.link), "agent.db was created")
                self.assertEqual(inventory(self.home), before, "the user's home changed")
                self.assertFalse(os.path.lexists(self.home / ".omp"), "~/.omp was created")
                blob = repr(home) + "\n".join(home.notes)
                self.assertNotIn(dummy(key), blob, "the key value reached notes/repr")
                self.assertEqual(omp_home.verify_omp_home(home), [])
                cli.omp_home_requirements(_layout(self.root / f"data-{key}"), env)  # no refusal

    def test_without_a_key_or_with_an_empty_one_it_is_still_refused_with_guidance(self):
        for env in (self.env, {**self.env, "OPENAI_API_KEY": ""}, {**self.env, "UNRELATED_API_KEY": "x"}):
            with self.subTest(env=sorted(env)):
                with self.assertRaises(StartRequirementError) as caught:
                    cli.omp_home_requirements(_layout(self.root / "data-x"), env)
                self.assertIn("/login", str(caught.exception))

    def test_a_stale_link_is_removed_and_never_recreated_when_only_a_key_remains(self):
        store = self.store()
        home = self.prepare(self.root / "data", self.env)
        self.assertTrue(os.path.islink(home.link))
        store.unlink()
        env = {**self.env, "OPENAI_API_KEY": dummy("OPENAI_API_KEY")}
        again = self.prepare(self.root / "data", env)
        self.assertFalse(again.linked)
        self.assertFalse(os.path.lexists(again.link), "the dangling link would let OMP create the user's store")
        self.assertFalse(os.path.lexists(store), "the user's store was created through the link")

    def test_a_present_store_still_links_when_a_key_is_set_too(self):
        store = self.store()
        home = self.prepare(self.root / "data", {**self.env, "OPENAI_API_KEY": dummy("OPENAI_API_KEY")})
        self.assertTrue(home.linked)
        self.assertEqual(os.readlink(home.link), str(store))


class KeyOnlyEntrypointTests(_WrapperBase):
    def scan(self, value: str, out) -> list[str]:
        """Where the dummy key VALUE appears in what Workbench printed or stored (our own temp data only)."""
        hits = []
        for label, text in (("stdout", out.stdout), ("stderr", out.stderr)):
            if value in text:
                hits.append(label)
        status = self.cli("status", "--data-dir", str(self.data), "--json")
        for label, text in (("status", status.stdout + status.stderr),):
            if value in text:
                hits.append(label)
        for path in self.data.rglob("*"):
            if path.is_file() and not path.is_symlink() and value.encode() in path.read_bytes():
                hits.append(str(path.relative_to(self.data)))
        return hits

    def test_start_with_a_provider_key_and_no_store_works_without_a_link_and_never_logs_the_value(self):
        shutil.rmtree(self.home / ".omp")
        home_before = p27u.inventory(self.home)
        value = dummy("ANTHROPIC_API_KEY")
        out = self.start(env_extra={"ANTHROPIC_API_KEY": value})
        self.assertNotEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertNotIn("No backend was started", out.stderr)
        snapshot = self.wait_isolation()
        iso = snapshot["omp_isolation"]
        self.assertEqual(iso["leaks"], [], iso)
        self.assertIn(iso["state"], ("ok", "warning"), iso)
        agent = self.data / "omp-root" / "agent"
        self.assertFalse(os.path.lexists(agent / "agent.db"), "an agent.db link/file was created")
        self.assertEqual(p27u.inventory(self.home), home_before, "the user's home changed")
        self.assertFalse(os.path.lexists(self.home / ".omp"), "~/.omp was created")
        for item in self.started():
            self.assertEqual(item["env"]["ANTHROPIC_API_KEY"], value, "the key must still reach the OMPs")
            p27m.assert_workbench_home_env(self, item["env"], self.home, self.data)
        self.assertEqual(self.scan(value, out), [], "the key value was printed or stored by Workbench")

    def test_an_empty_key_variable_is_not_a_key(self):
        shutil.rmtree(self.home / ".omp")
        before = p27u.inventory(self.home)
        out = self.start(env_extra={"OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": ""})
        self.refusal_outputs_nothing(out)
        self.assertIn("/login", out.stderr)
        self.assertEqual(p27u.inventory(self.home), before)

    def test_a_store_and_a_key_together_link_as_usual(self):
        value = dummy("OPENAI_API_KEY")
        out = self.start(env_extra={"OPENAI_API_KEY": value})
        self.assertNotEqual(out.returncode, 2, out.stdout + out.stderr)
        self.wait_isolation()
        self.assertEqual(os.readlink(self.data / "omp-root" / "agent" / "agent.db"), str(self.auth_store))
        self.assertEqual(self.scan(value, out), [])


if __name__ == "__main__":
    unittest.main()
