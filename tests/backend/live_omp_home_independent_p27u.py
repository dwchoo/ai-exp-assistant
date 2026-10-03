"""LIVE C-D64 Workbench-owned OMP home check with the real OMP (p27-home-test-01). Opt-in: ``WB_LIVE_HOME=1``.

Run (repo root)::

    WB_LIVE_HOME=1 PYTHONPATH=src:tests/backend /tmp/cw02-g1-venv/bin/python -m unittest \\
        tests.backend.live_omp_home_independent_p27u -v

``LiveRealStore`` uses the REAL user HOME and therefore the user's REAL OMP auth store through the Workbench symlink.
The store and every file under ``~/.omp`` are only ``lstat``ed (type, mode, size, mtime, inode, link target): nothing is
opened, read, copied or hashed. No prompt is ever sent (RPC introspection only, made by the backend's own isolation check;
the panes are never typed into) and every provider/OAuth request is blocked by pointing HTTP(S)/ALL_PROXY at a closed port.
Expectations (drafted from C-D64, the probe and the senior required_behavior):

- a backend in a temp data dir starts both OMPs, the isolation check is ``ok`` for both roles with zero leaks/warnings,
  authenticated provider ids are reported (ids only), each OMP pane runs with PI_CONFIG_DIR/PI_CODING_AGENT_DIR of the
  Workbench home and holds files open there and none under ``~/.omp`` other than ``agent.db*``;
- the Workbench root holds ``logs``/``run``/``cache`` (OMP writes them there, not into ``~/.omp``);
- an exited worker OMP restarts with the same home and the re-check is ``ok``; shutdown leaves no process;
- the ``~/.omp`` lstat inventory before/after differs only in ``agent/agent.db*`` and ``natives/<ver>`` metadata: no new or
  deleted files (SQLite ``-wal``/``-shm`` appearing/disappearing is reported separately). Anything else is listed (never
  deleted) and fails the test.

``LiveFakeHomeXdg`` runs the real OMP once with a FAKE HOME and a fake empty store under ``$XDG_DATA_HOME/omp`` (no real
credential) to see what the Workbench OMP writes into the user's locations for an XDG layout and a default layout: only the
version-keyed natives (``natives/<ver>/*.node``, C-D64 Root adjudication) and the backend predicts it.
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
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

from workbench.backend import launcher, omp_home  # noqa: E402
from workbench.backend.client import UiClient  # noqa: E402
from workbench.contracts.ui_v1 import ClientType  # noqa: E402

LIVE = os.environ.get("WB_LIVE_HOME") == "1"
BLOCKED = "http://127.0.0.1:9"
FAKE_KEY = "sk-ant-fake-p27u-never-used"  # only makes OMP list a model so that it stays up for get_state (no provider is contacted)
SQLITE_SUFFIXES = ("", "-wal", "-shm", "-journal")
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
REPORT = Path(os.environ.get("P27U_LIVE_REPORT", "/tmp/p27u-live-report.json"))


def inventory(path: Path) -> dict[str, tuple]:
    """lstat-only inventory; never opens a file."""
    out: dict[str, tuple] = {}
    for current, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            full = os.path.join(current, name)
            try:
                info = os.lstat(full)
            except FileNotFoundError:
                continue
            target = os.readlink(full) if stat.S_ISLNK(info.st_mode) else None
            out[os.path.relpath(full, path)] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_size,
                                                info.st_mtime_ns, info.st_ino, target)
    return out


def diff(before: dict, after: dict) -> dict[str, list[str]]:
    return {"new": sorted(set(after) - set(before)), "deleted": sorted(set(before) - set(after)),
            "changed": sorted(k for k in set(before) & set(after) if before[k] != after[k])}


def ticks(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return None if fields[0] in "ZX" else fields[19]


def owned(root: Path) -> dict[int, str]:
    """Processes whose command line names our temp root (cmdline only; never another process's environment)."""
    needle, found = str(root).encode(), {}
    for entry in os.listdir("/proc"):
        if entry.isdigit() and int(entry) != os.getpid():
            try:
                if needle in Path(f"/proc/{entry}/cmdline").read_bytes() and (t := ticks(int(entry))):
                    found[int(entry)] = t
            except OSError:
                continue
    return found


def home_keys(pid: int) -> dict[str, str | None]:
    """PI_CONFIG_DIR/PI_CODING_AGENT_DIR of an OMP WE started (only those two keys are kept)."""
    raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    keep = {}
    for item in raw:
        key, _, value = item.partition(b"=")
        if key in (b"PI_CONFIG_DIR", b"PI_CODING_AGENT_DIR", b"OMP_PROFILE", b"PI_PROFILE"):
            keep[key.decode()] = value.decode()
    return keep


def kill_exact(pid: int, start: str, sig=signal.SIGKILL) -> None:
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return
    try:
        if ticks(pid) == start:
            signal.pidfd_send_signal(fd, sig)
    except OSError:
        pass
    finally:
        os.close(fd)


@unittest.skipUnless(LIVE, "set WB_LIVE_HOME=1 to run the live Workbench OMP home check")
class LiveRealStore(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.omp = shutil.which("omp") or os.path.expanduser("~/.local/bin/omp")
        if not os.access(self.omp, os.X_OK):
            self.skipTest("omp not found")
        self.home = Path(os.environ["HOME"])
        self.user_omp = self.home / ".omp"
        store = omp_home.user_auth_store(os.environ)
        if not omp_home.auth_store_state(store).ok:
            self.skipTest("no usable user OMP auth store")
        self.store = store
        self.root = Path(tempfile.mkdtemp(prefix="p27u-live-", dir="/tmp"))
        self.data, self.project = self.root / "d", self.root / "proj"
        self.project.mkdir()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HERDR_", "WORKBENCH_", "TMUX", "PI_", "OMP_", "PYTHON"))}
        env.update({k: BLOCKED for k in PROXY_KEYS})
        env.update({"NO_PROXY": "", "no_proxy": "", "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1",
                    "LANG": "C.UTF-8"})
        self.env = env
        self.seen: dict[int, str] = {}
        self.report: dict = {"omp": self.omp, "store_kind": "xdg" if ".omp" not in str(store) else "default"}
        self.addCleanup(self.cleanup)

    def cli(self, *args, timeout=150):
        return subprocess.run([sys.executable, "-m", "workbench", *args], env=self.env, cwd=self.project,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)

    def status(self) -> dict | None:
        out = self.cli("status", "--data-dir", str(self.data), "--json", timeout=30)
        try:
            body = json.loads(out.stdout)
        except ValueError:
            return None
        return body.get("snapshot") if body.get("running") else None

    def wait(self, predicate, what, timeout=90.0) -> dict:
        deadline, snap = time.monotonic() + timeout, None
        while time.monotonic() < deadline:
            self.seen.update(owned(self.root))
            snap = self.status()
            if snap and predicate(snap):
                return snap
            time.sleep(0.5)
        self.fail(f"timeout waiting for {what}: {snap and {k: snap.get(k) for k in ('phase', 'omp_isolation')}}")

    def cleanup(self):
        self.seen.update(owned(self.root))
        if owned(self.root):
            try:
                self.cli("shutdown", "--data-dir", str(self.data), "--yes", timeout=60)
            except subprocess.TimeoutExpired:
                pass
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and any(ticks(p) == t for p, t in self.seen.items()):
            time.sleep(0.2)
        survivors = [p for p, t in self.seen.items() if ticks(p) == t]
        for pid in survivors:
            kill_exact(pid, self.seen[pid])
        shutil.rmtree(self.root, ignore_errors=True)
        self.report["cleanup"] = {"survivors_killed": survivors, "root_removed": not self.root.exists()}
        REPORT.write_text(json.dumps(self.report, indent=1, default=str))

    def pane_checks(self, snap: dict, role_pane: str) -> dict:
        pid = snap["panes"][role_pane]["process"]["pid"]
        keys = home_keys(pid)
        agent = self.data / "omp-root" / "agent"
        self.assertEqual(keys.get("PI_CODING_AGENT_DIR"), str(agent), keys)
        self.assertEqual(os.path.normpath(os.path.join(str(self.home), keys.get("PI_CONFIG_DIR", ""))),
                         str(self.data / "omp-root"))
        self.assertNotIn("OMP_PROFILE", keys)
        self.assertNotIn("PI_PROFILE", keys)
        paths = omp_home.observe_open_paths(pid)
        user = [p for p in paths if p.startswith(str(self.user_omp) + "/")
                and not os.path.basename(p).startswith("agent.db")]
        in_root = [p for p in paths if p.startswith(str(self.data / "omp-root") + "/")]
        self.assertEqual(user, [], f"{role_pane} holds files of the user's ~/.omp open")
        self.assertTrue(in_root, f"{role_pane} holds nothing open in the Workbench home")
        return {"pid": pid, "open_in_wb_root": len(in_root), "open_user_agent_db": sorted(
            os.path.basename(p) for p in paths if p.startswith(str(self.user_omp) + "/"))}

    def test_real_store_through_the_link_isolated_and_no_user_omp_writes(self):
        before = inventory(self.user_omp)
        store_before = os.lstat(self.store)
        out = self.cli("start", "--data-dir", str(self.data), "--omp", self.omp, "--no-attach", "--timeout", "90")
        self.seen.update(owned(self.root))
        self.report["start"] = {"exit": out.returncode, "stdout": out.stdout[-1500:], "stderr": out.stderr[-1500:]}
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        snap = self.wait(lambda s: s["phase"] == "ready" and s["omp_isolation"].get("checked"), "ready + checked")
        iso = snap["omp_isolation"]
        roles = iso.get("roles") or {}
        self.report["isolation"] = {"state": iso["state"], "leaks": iso["leaks"], "warnings": iso["warnings"],
                                    "providers": {r: ((v.get("observed") or {}).get("omp_home") or {}).get(
                                        "authenticated_providers") for r, v in roles.items()},
                                    "omp_version": snap["backend"].get("omp_version")}
        self.assertEqual((iso["state"], iso["leaks"], iso["warnings"]), ("ok", [], []), self.report["isolation"])
        for role in ("manager", "worker"):
            home = (roles[role].get("observed") or {}).get("omp_home") or {}
            self.assertTrue(home.get("authenticated_providers"), f"{role}: no authenticated provider via the link")
            self.assertEqual(home.get("user_paths"), [])
            self.assertTrue(home.get("agent_dir_in_use"))
        agent = self.data / "omp-root" / "agent"
        self.assertTrue(stat.S_ISLNK(os.lstat(agent / "agent.db").st_mode))
        self.assertEqual(os.readlink(agent / "agent.db"), str(self.store))
        for companion in ("-wal", "-shm", "-journal"):
            self.assertFalse(os.path.lexists(agent / f"agent.db{companion}"), "the store split into the wb home")
        root_entries = sorted(os.listdir(self.data / "omp-root"))
        self.report["wb_root_entries"] = root_entries
        for name in ("logs", "run", "cache"):
            self.assertIn(name, root_entries, f"OMP did not use the Workbench root for {name}")
        self.report["panes"] = {p: self.pane_checks(snap, p) for p in ("manager_omp", "worker_omp")}

        # restart the worker: stop exactly the OMP our backend started, then restart_pane through ui_v1
        worker = snap["panes"]["worker_omp"]["process"]
        kill_exact(worker["pid"], str(worker["start_ticks"]), signal.SIGTERM)
        self.wait(lambda s: not s["panes"]["worker_omp"]["alive"], "worker exit", 30)
        with UiClient(self.data / "ui.sock", name="p27u-live", timeout=30) as client:
            client.attach((30, 120))
            result = client.request(ClientType.RESTART_PANE, pane="worker_omp", timeout=30)
        self.report["restart_result"] = {k: result.get(k) for k in ("ok", "result", "reason")}
        snap = self.wait(lambda s: s["panes"]["worker_omp"]["alive"] and s["phase"] == "ready"
                         and "rechecking" not in s["omp_isolation"] and s["omp_isolation"].get("checked")
                         and s["panes"]["worker_omp"]["process"]["pid"] != worker["pid"], "restart + re-check", 120)
        iso = snap["omp_isolation"]
        self.report["after_restart"] = {"state": iso["state"], "leaks": iso["leaks"], "warnings": iso["warnings"],
                                        "worker": self.pane_checks(snap, "worker_omp")}
        self.assertEqual((iso["state"], iso["leaks"], iso["warnings"]), ("ok", [], []), iso)

        out = self.cli("shutdown", "--data-dir", str(self.data), "--yes", timeout=90)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and any(ticks(p) == t for p, t in self.seen.items()):
            time.sleep(0.2)
        alive = [p for p, t in self.seen.items() if ticks(p) == t]
        self.report["shutdown"] = {"exit": out.returncode, "alive_after": alive, "owned_seen": len(self.seen)}
        self.assertEqual(alive, [], "processes survived the shutdown")

        after = inventory(self.user_omp)
        store_after = os.lstat(self.store)
        changes = diff(before, after)
        rel_store = os.path.relpath(self.store, self.user_omp) if str(self.store).startswith(str(self.user_omp)) else None
        sqlite = {rel_store, f"{rel_store}-wal", f"{rel_store}-shm", f"{rel_store}-journal"} if rel_store else set()
        natives = {k for k in after if k.startswith("natives/") and k.count("/") == 1}
        unexpected = {
            "new": [k for k in changes["new"] if k not in sqlite],
            "deleted": [k for k in changes["deleted"] if k not in sqlite],
            "changed": [k for k in changes["changed"] if k not in sqlite and k not in natives and k != "natives"],
        }
        self.report["user_omp_inventory"] = {"entries_before": len(before), "entries_after": len(after),
                                             "diff": changes, "unexpected": unexpected,
                                             "store_inode_same": store_before.st_ino == store_after.st_ino,
                                             "store_mode_same": store_before.st_mode == store_after.st_mode}
        self.assertEqual(store_before.st_ino, store_after.st_ino, "the user's store was replaced")
        self.assertEqual(stat.S_IMODE(store_before.st_mode), stat.S_IMODE(store_after.st_mode))
        self.assertEqual(unexpected, {"new": [], "deleted": [], "changed": []},
                         "files of ~/.omp changed beyond agent.db*/natives (listed, not deleted)")


NODE_SUFFIX = ".node"


def run_introspection(omp: str, env: dict, cwd: Path, timeout: float = 90.0) -> bool:
    """Start the real OMP once (RPC ``get_state`` only, no prompt), wait for the answer, stop its own group exactly.

    Returns True when the OMP answered. The native addon extraction (two ~190MB files) happens before the answer.
    """
    import select
    process = subprocess.Popen([omp, "--no-extensions", "--mode", "rpc", "--no-session", "--no-title"], cwd=cwd, env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    start = ticks(process.pid)
    answered = False
    try:
        process.stdin.write(b'{"id":"s","type":"get_state"}\n')
        process.stdin.flush()
        deadline, buffer = time.monotonic() + timeout, b""
        while time.monotonic() < deadline and not answered:
            ready, _, _ = select.select([process.stdout], [], [], 0.5)
            if ready:
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                buffer += chunk
                answered = b'"id":"s"' in buffer.replace(b'"id": "s"', b'"id":"s"')
            if process.poll() is not None:
                break
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        if process.poll() is None and ticks(process.pid) == start:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except OSError:
                    break
                try:
                    process.wait(5)
                    break
                except subprocess.TimeoutExpired:
                    continue
        process.wait()
        process.stdout.close()
    return answered


@unittest.skipUnless(LIVE, "set WB_LIVE_HOME=1 to run the live Workbench OMP home check")
class LiveFakeHomeXdg(unittest.TestCase):
    """Fake HOME (+ fake store, no credential): where does a Workbench-started OMP write in the user's locations?

    Root adjudication (root-adjudication-p27-home-natives, C-D64): OMP's native addon dir ignores PI_CONFIG_DIR, so the
    only creation allowed in the fake user locations is ``~/.omp/natives/<installed omp version>/*.node`` (or
    ``$XDG_DATA_HOME/omp/natives/<version>/*.node``); anything else is forbidden and the backend's isolation note must
    have predicted the creation (``natives_status``).
    """

    def setUp(self):
        self.omp = shutil.which("omp") or os.path.expanduser("~/.local/bin/omp")
        if not os.access(self.omp, os.X_OK):
            self.skipTest("omp not found")
        self.version_text = launcher.omp_version(self.omp)
        self.version = (__import__("re").search(r"(\d+\.\d+\.\d+)", self.version_text or "") or [None, None])[1]
        self.assertTrue(self.version, f"cannot determine the installed omp version: {self.version_text!r}")
        self.root = Path(tempfile.mkdtemp(prefix="p27u-xdg-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.home.mkdir()

    def started(self, base: dict) -> tuple[dict, omp_home.OmpHome]:
        prepared = omp_home.prepare_omp_home(self.root / "data", base, skills_dir=launcher.default_skills_dir(),
                                             provider_ids=launcher.ISOLATION_PROVIDER_IDS)
        return omp_home.home_environment(base, prepared.environment()), prepared

    def assert_only_natives(self, before: dict, after: dict, natives_root: Path, base_dir: Path, what: str,
                            store: Path | None = None) -> list[str]:
        """Nothing but ``<natives_root>/<version>/*.node`` (plus the directories on the way) may appear.

        ``store``: the user's (fake) auth store the Workbench OMP shares by design; only it and its SQLite companions
        may differ (new ``-wal``/``-shm``).
        """
        changes = diff(before, after)
        store_dirs: set[str] = set()
        if store is not None and str(store).startswith(str(base_dir) + os.sep):
            names = {os.path.relpath(str(store) + suffix, base_dir) for suffix in SQLITE_SUFFIXES}
            changes = {kind: [k for k in keys if k not in names] for kind, keys in changes.items()}
            store_dirs = {os.path.dirname(os.path.relpath(store, base_dir))}  # mtime of the dir holding the store
        rel = os.path.relpath(natives_root, base_dir)
        version_dir = f"{rel}/{self.version}"
        allowed_dirs = {rel, version_dir, os.path.dirname(rel)} - {"", "."}
        problems = [k for k in changes["new"]
                    if not (k in allowed_dirs or (os.path.dirname(k) == version_dir and k.endswith(NODE_SUFFIX)
                                                  and after[k][0] == stat.S_IFREG))]
        self.assertEqual(problems, [], f"{what}: the Workbench OMP created something besides natives/{self.version}/*.node")
        self.assertEqual(changes["deleted"], [], f"{what}: something was deleted")
        self.assertEqual([k for k in changes["changed"] if k not in allowed_dirs | store_dirs], [],
                         f"{what}: existing entries changed (only the natives dirs may)")
        return sorted(k for k in changes["new"] if k.endswith(NODE_SUFFIX))

    def test_xdg_layout_only_creates_the_version_keyed_natives_in_the_users_locations(self):
        home, xdg = self.home, self.root / "xdg"
        (xdg / "omp").mkdir(parents=True)
        (xdg / "omp" / "agent.db").write_bytes(b"")  # fake empty store in the temp dir (no credential)
        base = {"PATH": "/usr/bin:/bin", "HOME": str(home), "XDG_DATA_HOME": str(xdg), "LANG": "C.UTF-8",
                "ANTHROPIC_API_KEY": FAKE_KEY, **{k: BLOCKED for k in PROXY_KEYS}}
        env, _prepared = self.started(base)
        status = omp_home.natives_status(base, env, self.version_text)
        wb_natives = home / ".omp" / "natives"
        # the backend predicted (before starting anything) what the OMP then does: it extracts into HOME, not into XDG
        self.assertTrue(status["split"], status)
        self.assertEqual(status["dir"], str(wb_natives / self.version), status)
        self.assertIsNotNone(status["note"], "the isolation note did not predict the natives extraction")
        self.assertIn(str(wb_natives / self.version), status["note"])
        self.assertNotIn("XDG_DATA_HOME", env, "the user's XDG_DATA_HOME reached the Workbench OMP")
        before_home, before_xdg = inventory(home), inventory(xdg)
        answered = run_introspection(self.omp, env, self.root)
        self.assertTrue(answered, "the real OMP did not answer get_state")
        nodes = self.assert_only_natives(before_home, inventory(home), wb_natives, home, "fake HOME")
        self.assertEqual(self.assert_only_natives(before_xdg, inventory(xdg), xdg / "omp" / "natives", xdg, "fake XDG dir",
                                                  store=xdg / "omp" / "agent.db"), [],
                         "the Workbench OMP extracted natives into the user's XDG data dir")
        self.assertTrue(nodes, "OMP extracted no natives although the note predicted it (prediction is wrong)")
        self.assertEqual(sorted(os.listdir(wb_natives)), [self.version], "other natives versions appeared")

    def test_default_layout_predicts_and_limits_the_natives_extraction_too(self):
        home = self.home
        (home / ".omp" / "agent").mkdir(parents=True)
        (home / ".omp" / "agent" / "agent.db").write_bytes(b"")  # fake empty store (no credential)
        base = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8", "ANTHROPIC_API_KEY": FAKE_KEY,
                **{k: BLOCKED for k in PROXY_KEYS}}
        env, _prepared = self.started(base)
        status = omp_home.natives_status(base, env, self.version_text)
        natives = home / ".omp" / "natives"
        self.assertFalse(status["split"], status)
        self.assertEqual(status["dir"], str(natives / self.version), status)
        self.assertIsNotNone(status["note"], "the isolation note did not predict the natives extraction")
        self.assertIn(str(natives / self.version), status["note"])
        before = inventory(home)
        self.assertTrue(run_introspection(self.omp, env, self.root), "the real OMP did not answer get_state")
        after = inventory(home)
        store_path = home / ".omp" / "agent" / "agent.db"
        nodes = self.assert_only_natives(before, after, natives, home, "fake HOME", store=store_path)
        self.assertTrue(nodes, "OMP extracted no natives although the note predicted it (prediction is wrong)")
        self.assertTrue(stat.S_ISREG(os.lstat(store_path).st_mode), "the user's (fake) store is no regular file any more")
        # once the addon is there the prediction goes away (no note)
        self.assertIsNone(omp_home.natives_status(base, env, self.version_text)["note"])


if __name__ == "__main__":
    unittest.main()
