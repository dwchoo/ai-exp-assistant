"""Data-dir precedence/permissions, single-instance lock and start-requirement checks."""
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from workbench.backend import launcher
from workbench.backend.cli import parser
from workbench.backend.launcher import LaunchPlan, StartRequirementError
from workbench.backend.paths import (
    BackendLocked, DataDirError, DataLayout, InstanceLock, ensure_private_dir, resolve_data_dir,
)
from workbench.terminal.shell_g2.prototype import ShellChoice


class DataDirTests(unittest.TestCase):
    def test_precedence_cli_then_env_then_xdg_then_home(self):
        env = {"WORKBENCH_DATA_DIR": "/e/wb", "XDG_STATE_HOME": "/x", "HOME": "/h"}
        self.assertEqual(resolve_data_dir("/c/wb", env), Path("/c/wb"))
        self.assertEqual(resolve_data_dir(None, env), Path("/e/wb"))
        self.assertEqual(resolve_data_dir(None, {"XDG_STATE_HOME": "/x", "HOME": "/h"}), Path("/x/omp-workbench"))
        self.assertEqual(resolve_data_dir(None, {"XDG_STATE_HOME": "rel", "HOME": "/h"}),
                         Path("/h/.local/state/omp-workbench"))
        self.assertEqual(resolve_data_dir(None, {"HOME": "/h"}), Path("/h/.local/state/omp-workbench"))
        self.assertTrue(resolve_data_dir("relative", {}).is_absolute())

    def test_private_dir_is_created_or_tightened_and_symlinks_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = ensure_private_dir(root / "a" / "data")
            self.assertEqual(stat.S_IMODE(created.stat().st_mode), 0o700)
            loose = root / "loose"
            loose.mkdir(mode=0o755)
            os.chmod(loose, 0o755)
            ensure_private_dir(loose)
            self.assertEqual(stat.S_IMODE(loose.stat().st_mode), 0o700)
            (root / "link").symlink_to(loose)
            with self.assertRaises(DataDirError):
                ensure_private_dir(root / "link")
            (root / "file").write_text("x")
            with self.assertRaises(DataDirError):
                ensure_private_dir(root / "file")

    def test_socket_path_length_is_checked(self):
        with self.assertRaises(DataDirError):
            DataLayout(Path("/tmp/" + "d" * 110)).check_socket_paths()
        DataLayout(Path("/tmp/short")).check_socket_paths()

    def test_instance_lock_is_exclusive_across_processes_and_not_inherited(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backend.lock"
            first = InstanceLock(path)
            first.acquire()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(BackendLocked):
                InstanceLock(path).acquire()
            self.assertTrue(InstanceLock(path).held_elsewhere())
            code = ("import sys; sys.path.insert(0, 'src');"
                    "from pathlib import Path; from workbench.backend.paths import InstanceLock, BackendLocked\n"
                    f"try:\n    InstanceLock(Path({str(path)!r})).acquire()\nexcept BackendLocked:\n    raise SystemExit(7)")
            child = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[2],
                                   timeout=30, close_fds=False)
            self.assertEqual(child.returncode, 7)
            first.release()
            self.assertFalse(InstanceLock(path).held_elsewhere())
            second = InstanceLock(path)
            second.acquire()
            second.release()


class StartRequirementTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="cw17-path-")
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def bindir(self, name, **links):
        path = self.root / name
        path.mkdir()
        for link, target in links.items():
            (path / link).symlink_to(target)
        return str(path)

    def test_bash_first_then_sh_and_login_shell_is_ignored(self):
        both = self.bindir("both", bash="/usr/bin/bash", sh="/usr/bin/dash")
        only_sh = self.bindir("sh", sh="/usr/bin/dash")
        zsh_default = {"PATH": both, "SHELL": "/usr/bin/zsh"}
        self.assertEqual(launcher.choose_shell(zsh_default), ShellChoice("bash", "/usr/bin/bash"))
        self.assertEqual(launcher.choose_shell({"PATH": only_sh, "SHELL": "/usr/bin/zsh"}),
                         ShellChoice("sh", "/usr/bin/dash"))

    def test_missing_bash_and_sh_gives_guidance(self):
        empty = self.bindir("empty")
        with self.assertRaises(StartRequirementError) as caught:
            launcher.choose_shell({"PATH": empty, "SHELL": "/usr/bin/zsh"})
        text = str(caught.exception)
        self.assertIn("Bash or a POSIX sh", text)
        self.assertIn("No backend was started", text)
        with self.assertRaises(StartRequirementError):
            launcher.build_plan({"PATH": empty})

    def test_missing_omp_or_extension_is_reported_before_start(self):
        shells = self.bindir("shells", bash="/usr/bin/bash")
        with self.assertRaises(StartRequirementError) as caught:
            launcher.build_plan({"PATH": shells})
        self.assertIn("'omp'", str(caught.exception))
        fake_omp = self.root / "omp"
        fake_omp.write_text("#!/bin/sh\necho omp/0.0-test\n")
        fake_omp.chmod(0o700)
        with self.assertRaises(StartRequirementError):
            launcher.build_plan({"PATH": shells}, omp=str(fake_omp), bridge_extension=str(self.root / "none.ts"))
        plan = launcher.build_plan({"PATH": shells, "WORKBENCH_OMP_ARGS": "--no-session --model 'a b'"},
                                   omp=str(fake_omp))
        self.assertEqual(plan.omp_version, "omp/0.0-test")
        self.assertEqual(plan.omp_args, ("--no-session", "--model", "a b"))
        self.assertTrue(Path(plan.bridge_extension).is_file())

    def test_omp_and_shell_environments_inject_only_bridge_identity(self):
        plan = LaunchPlan(ShellChoice("bash", "/usr/bin/bash"), "/x/omp", "omp/18.2.10", "/x/bridge.ts",
                          ("--no-session",))
        base = {"PATH": "/usr/bin", "HOME": "/h", "WORKBENCH_G3_TOKEN": "leaked", "OPENAI_API_KEY": "user-own"}
        env = launcher.omp_environment(base, plan, role="worker", token="t0k", bridge_socket=Path("/d/bridge.sock"))
        self.assertEqual((env["WORKBENCH_G3_ROLE"], env["WORKBENCH_G3_TOKEN"], env["WORKBENCH_G3_GENERATION"]),
                         ("worker", "t0k", "1"))
        self.assertEqual(env["WORKBENCH_G3_BRIDGE_SOCKET"], "/d/bridge.sock")
        self.assertEqual(env["OPENAI_API_KEY"], "user-own")  # passed through, never stored
        self.assertEqual(launcher.omp_command(plan), ["/x/omp", "--no-session", "--extension", "/x/bridge.ts"])
        shell_env = launcher.shell_environment(base)
        self.assertNotIn("WORKBENCH_G3_TOKEN", shell_env)
        self.assertEqual(shell_env["TERM"], "xterm-256color")
        parsed = parser().parse_args(["_backend", "--data-dir", "/d", "--project-dir", "/p", *plan.to_argv()])
        self.assertEqual((parsed.shell_kind, parsed.shell_path, parsed.omp_arg), ("bash", "/usr/bin/bash",
                                                                                   ["--no-session"]))


if __name__ == "__main__":
    unittest.main()
