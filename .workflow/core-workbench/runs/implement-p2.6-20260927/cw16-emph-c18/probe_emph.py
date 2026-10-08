"""CW-16 G1-COLOR emphasis probe on frozen C18 (p27-cw16-emph-01). Not part of the repo test tree.

Reuses tests/integration/{cw16_harness,cw16_flow_rig,live_cw16_gap} unchanged (product entrypoint
``python -m workbench start/attach``, fake HOME with an empty agent.db, scripted local provider = ZERO model
requests, env built from scratch without TMUX*/HERDR_*).

Host-pane programs print text with SGR bold (1), underline (4), reverse (7) alone and combined with index colours.
The product UI's outer frames are observed in two outermost emulators:
  * PYTE : tests/ui/support.rep_screen_classes() (the harness Ui) — cell.bold / underscore / reverse / fg / bg
  * TMUX : an isolated tmux server (own -S socket inside the owned /tmp root) running ``workbench attach``;
           ``capture-pane -e -p`` is parsed for SGR state per character.
Run (repo root):
  env -i HOME=/tmp/<fake> PATH=... LANG=C.UTF-8 TERM=xterm-256color PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
    proxies WB_LIVE_CW16=1 WB_CW16_REPORT_DIR=<run>/cw16-emph-c18/reports WB_CW16_RUN_ID=c18-emph \
    WB_EMPH_OUT=<run>/cw16-emph-c18/emph-report.json /tmp/cw02-g1-venv/bin/python -m unittest -v <this file>
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import unittest

REPO = Path(os.environ.get("WB_EMPH_REPO", "/home/dwchoo/ai-exp-assistant"))
sys.path.insert(0, str(REPO / "tests" / "integration"))

import cw16_harness as h  # noqa: E402
import cw16_flow_rig as fr  # noqa: E402
from cw16_flow_rig import Rig  # noqa: E402
import live_cw16_gap as gap  # noqa: E402  (find_cell / as_rgb / _palette helpers only)

SKIP = fr.skip_reason()
OUT = Path(os.environ.get("WB_EMPH_OUT", "/tmp/emph-report.json"))
TMUX = "/usr/bin/tmux"
PALETTE = gap._palette()

# marker suffix -> (sgr parameters, expected {bold, underline, reverse, fg (xterm index|None), bg (index|None)})
def exp(bold=False, underline=False, reverse=False, fg=None, bg=None):
    return {"bold": bold, "underline": underline, "reverse": reverse, "fg": fg, "bg": bg}


GROUPS = {
    # alone, plus a plain control that must carry NO attribute
    "alone": [("PLN", "0", exp()), ("BLD", "1", exp(bold=True)), ("UND", "4", exp(underline=True)),
              ("REV", "7", exp(reverse=True))],
    # combined with index colours
    "coloured": [("B31", "1;31", exp(bold=True, fg=1)), ("U208", "4;38;5;208", exp(underline=True, fg=208)),
                 ("R44", "7;44", exp(reverse=True, bg=4)),
                 ("BUR", "1;4;7;32", exp(bold=True, underline=True, reverse=True, fg=2)),
                 ("B2C", "1;38;5;196;48;5;21", exp(bold=True, fg=196, bg=21))],
    "coloured2": [("U9", "4;91", exp(underline=True, fg=9)), ("R240", "7;38;5;240;48;5;250",
                                                           exp(reverse=True, fg=240, bg=250)),
                  ("BU4", "1;4;34;47", exp(bold=True, underline=True, fg=4, bg=7))],
}


def printf_line(tag: str, cases) -> str:
    parts = " ".join(f"\\033[{sgr}m%s\\033[0m" for _, sgr, _ in cases)
    args = " ".join(f'"GQ{tag}""{suffix}"' for suffix, _, _ in cases)  # the typed line never contains a marker
    return f"printf '{parts}\\n' {args}"


# ----------------------------------------------------------------------------------------------- pyte side
def idx_of(value: str | None, palette: list[str]) -> int | None:
    """pyte cell colour -> xterm palette index (None for 'default'); exact rgb of the xterm palette also maps."""
    rgb = gap.as_rgb(value or "")
    if rgb is None:
        return None
    return rgb  # compared as RGB: index 9 and 196 are both ff0000 in the xterm palette, so an index is ambiguous


def pyte_marker_state(ui, marker: str, palette: list[str]):
    where = gap.find_cell(ui, marker, skip_after="printf")
    if where is None:
        return None
    row, col = where
    cells = [ui.screen.buffer[row][col + i] for i in range(len(marker))]
    states = {(c.bold, c.underscore, c.reverse, c.fg, c.bg) for c in cells}
    c = cells[0]
    return {"text": "".join(x.data for x in cells), "uniform": len(states) == 1,
            "bold": c.bold, "underline": c.underscore, "reverse": c.reverse, "italics": c.italics,
            "fg_raw": c.fg, "bg_raw": c.bg, "fg": idx_of(c.fg, palette), "bg": idx_of(c.bg, palette)}


# ----------------------------------------------------------------------------------------------- tmux side
SGR = re.compile(r"\x1b\[([0-9;:]*)m")


def parse_capture_line(line: str, st: dict | None = None):
    """[(char, state)] for one ``capture-pane -e`` line. state = dict(bold, underline, reverse, fg, bg) (index|None).
    ``st`` is the SGR state carried in from the previous line: tmux's capture-pane -e emits only the DIFFERENCE from the
    previous cell, and that previous cell may be the last cell of the line above (it does not reset at a line start)."""
    st = dict(st) if st else {"bold": False, "underline": False, "reverse": False, "fg": None, "bg": None}
    out = []
    pos = 0
    for m in SGR.finditer(line):
        for ch in line[pos:m.start()]:
            out.append((ch, dict(st)))
        pos = m.end()
        params = [p for p in m.group(1).replace(":", ";").split(";")]
        nums = [int(p) if p else 0 for p in params] or [0]
        i = 0
        while i < len(nums):
            n = nums[i]
            if n == 0:
                st.update(bold=False, underline=False, reverse=False, fg=None, bg=None)
            elif n == 1:
                st["bold"] = True
            elif n == 4:
                st["underline"] = True
            elif n == 7:
                st["reverse"] = True
            elif n == 22:
                st["bold"] = False
            elif n == 24:
                st["underline"] = False
            elif n == 27:
                st["reverse"] = False
            elif 30 <= n <= 37:
                st["fg"] = n - 30
            elif 90 <= n <= 97:
                st["fg"] = n - 90 + 8
            elif 40 <= n <= 47:
                st["bg"] = n - 40
            elif 100 <= n <= 107:
                st["bg"] = n - 100 + 8
            elif n == 39:
                st["fg"] = None
            elif n == 49:
                st["bg"] = None
            elif n in (38, 48) and i + 2 < len(nums) + 0 and nums[i + 1] == 5:
                st["fg" if n == 38 else "bg"] = nums[i + 2]
                i += 2
            elif n in (38, 48) and i + 4 < len(nums) + 0 and nums[i + 1] == 2:
                st["fg" if n == 38 else "bg"] = f"rgb:{nums[i+2]:02x}{nums[i+3]:02x}{nums[i+4]:02x}"
                i += 4
            i += 1
    for ch in line[pos:]:
        out.append((ch, dict(st)))
    return out, st


def tmux_marker_state(capture: str, marker: str):
    carried = None
    for line in capture.splitlines():
        chars, carried_next = parse_capture_line(line, carried)
        entry, carried = carried, carried_next  # (the state at the start of THIS line is what was carried in)
        if marker not in SGR.sub("", line) or "printf" in line:
            continue
        text = "".join(c for c, _ in chars)
        at = text.find(marker)
        if at < 0 or "printf" in text:
            continue
        cells = [s for _, s in chars[at:at + len(marker)]]
        c = cells[0]
        return {"text": text[at:at + len(marker)], "uniform": all(x == c for x in cells), **c,
                "sgr_carried_in_from_previous_line": entry, "raw": line[:line.find(marker) + len(marker) + 20] if marker in line else line[:200]}
    return None


def verdict(state, want):
    """strict: the attribute flags and colour indices equal what the program wrote. swapped: reverse was applied by
    exchanging fg and bg instead of carrying the flag (visually equal) — reported, not counted as a pass."""
    if state is None:
        return {"strict": False, "why": "marker not found on the outer screen"}
    flags = all(state[k] == want[k] for k in ("bold", "underline", "reverse"))
    rgb = lambda v: None if v is None else (PALETTE[v] if isinstance(v, int) else v.replace("rgb:", ""))
    colours = rgb(state["fg"]) == rgb(want["fg"]) and rgb(state["bg"]) == rgb(want["bg"])
    swapped = (want["reverse"] and not state["reverse"] and rgb(state["fg"]) == rgb(want["bg"])
               and rgb(state["bg"]) == rgb(want["fg"])
               and state["bold"] == want["bold"] and state["underline"] == want["underline"])
    return {"strict": bool(flags and colours and state["uniform"]), "flags": flags, "colours": colours,
            "uniform": state["uniform"], "swapped_equivalent": bool(swapped)}


@unittest.skipIf(SKIP, SKIP or "")
class EmphasisProbe(unittest.TestCase):
    report: dict = {"candidate": "a2e3daf2...", "term_pyte": None, "pyte": {}, "tmux": {}}

    @classmethod
    def tearDownClass(cls):
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(cls.report, indent=1, ensure_ascii=False, default=str))

    def test_emphasis_through_the_product_ui(self):
        rig = Rig(self, "emph-G1-COLOR")
        palette = gap._palette()
        rep = self.__class__.report
        rep["omp"] = rig.report.data.get("versions")
        if os.environ.get("WB_EMPH_TERM"):
            rig.sb.env["TERM"] = os.environ["WB_EMPH_TERM"]
        rep["term_pyte"] = rig.sb.env["TERM"]
        rig.step("start", lambda: rig.start() and {"term": rig.sb.env.get("TERM")})

        def run_group(emit, name, tag):
            cases = GROUPS[name]
            seen, results = emit(tag, cases)
            return seen, results

        # ---------------- PYTE ----------------
        def emit_pyte(tag, cases):
            rig.say("host_shell", printf_line(tag, cases))
            last = f"GQ{tag}{cases[-1][0]}"
            assert rig.ui.wait_text(re.escape(last), 20), rig.ui.excerpt("GQ")
            rig.pump(0.5)
            seen, res = {}, {}
            for suffix, sgr, want in cases:
                state = pyte_marker_state(rig.ui, f"GQ{tag}{suffix}", palette)
                raw = bytes(rig.ui.raw)
                at = raw.rfind(f"GQ{tag}{suffix}".encode())
                seen[suffix] = {"sgr": sgr, "outer": state,
                                "raw_bytes_before_marker": raw[max(0, at - 48):at + len(f"GQ{tag}{suffix}")].decode("latin1")}
                res[suffix] = verdict(state, want)
                res[suffix]["expected"] = want
            return seen, res

        def pyte_step(name, tag):
            def fn():
                seen, res = emit_pyte(tag, GROUPS[name])
                rep["pyte"][name] = {"observed": seen, "verdict": res}
                bad = {k: v for k, v in res.items() if not v["strict"]}
                assert not bad, {"mismatch": bad, "observed": {k: seen[k] for k in bad}}
                return {"verdict": {k: v["strict"] for k, v in res.items()}}
            return fn

        for n, (name, tag) in enumerate((("alone", "EA"), ("coloured", "EB"), ("coloured2", "EC"))):
            rig.step(f"PYTE_{name}", pyte_step(name, tag), ("start",))

        # ---------------- TMUX ----------------
        def tmux_phase():
            # leave the plain-pty UI, keep the backend (and host shell) alive
            rig.ui.send(h.PREFIX + b"q", settle=0.5)
            rig.ui.wait_exit(10)
            rig.ui = None
            sb = rig.sb
            sock = str(sb.root / "emph.sock")
            conf = sb.root / "emph.conf"
            conf.write_text('set -g default-terminal "tmux-256color"\nset -g status off\n'
                            'set -ga terminal-overrides ",*:Tc"\nset -g escape-time 0\n')
            env = dict(sb.env)
            env["TERM"] = "xterm-256color"
            h.assert_env_isolated(env, sb.root)
            attach = " ".join(shlex.quote(a) for a in sb.attach_argv())
            cmd = ["env", f"TERM=tmux-256color", f"COLORTERM=truecolor", *[f"{k}={v}" for k, v in
                                                                            ()], "/bin/sh", "-c", f"exec {attach}"]
            tm = lambda *a, **kw: subprocess.run([TMUX, "-S", sock, "-f", str(conf), *a], env=env, cwd=str(sb.project),
                                                 capture_output=True, text=True, timeout=30, **kw)
            done = tm("new-session", "-d", "-s", "emph", "-x", str(h.COLS), "-y", str(h.ROWS), *cmd)
            assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
            srv = tm("display-message", "-p", "-t", "emph", "#{pid} #{pane_pid}")
            server_pid, pane_pid = (int(x) for x in srv.stdout.split())
            server_start, pane_start = h.ticks(server_pid), h.ticks(pane_pid)
            rep["tmux_identity"] = {"socket": sock, "server_pid": server_pid, "pane_pid": pane_pid}
            sb.own(server_pid, server_start)
            sb.own(pane_pid, pane_start)

            def kill_server():
                try:
                    tm("kill-server")
                except Exception:
                    pass
                for pid, start in ((server_pid, server_start), (pane_pid, pane_start)):
                    if h.alive(pid, start):
                        h.kill_exact(pid, start)
            sb.cleanups.append(kill_server)

            def cap():
                return tm("capture-pane", "-e", "-p", "-t", "emph").stdout

            def plain():
                return tm("capture-pane", "-p", "-t", "emph").stdout

            def wait_for(pred, timeout):
                import time
                end = time.monotonic() + timeout
                while time.monotonic() < end:
                    if pred(plain()):
                        return True
                    time.sleep(0.2)
                return pred(plain())

            assert wait_for(lambda t: "HOST SHELL" in t and "WORKER OMP" in t, 60), plain()[-1500:]
            # focus host shell: prefix + 3
            tm("send-keys", "-t", "emph", "-H", "1d")
            tm("send-keys", "-t", "emph", "-l", "3")
            import time
            time.sleep(0.5)
            rep["tmux"]["term_in_pane"] = tm("display-message", "-p", "-t", "emph", "#{pane_current_command}").stdout.strip()
            for name, tag in (("alone", "FA"), ("coloured", "FB"), ("coloured2", "FC")):
                cases = GROUPS[name]
                tm("send-keys", "-t", "emph", "-l", printf_line(tag, cases))
                tm("send-keys", "-t", "emph", "Enter")
                last = f"GQ{tag}{cases[-1][0]}"
                assert wait_for(lambda t: last in "\n".join(l for l in t.splitlines() if "printf" not in l), 20), plain()[-1500:]
                time.sleep(0.4)
                capture = cap()
                seen, res = {}, {}
                for suffix, sgr, want in cases:
                    state = tmux_marker_state(capture, f"GQ{tag}{suffix}")
                    seen[suffix] = {"sgr": sgr, "outer": state}
                    res[suffix] = verdict(state, want)
                    res[suffix]["expected"] = want
                rep["tmux"][name] = {"observed": seen, "verdict": res}
            # raw escape sample of one coloured line for the record
            rep["tmux"]["raw_sample"] = [l[:260] for l in cap().splitlines() if l.count("GQFB") and "printf" not in l and '""' not in SGR.sub("", l)]
            # control: the same SGR written straight into a tmux pane (no Workbench at all)
            ctl = tm("new-session", "-d", "-s", "ctl", "-x", "80", "-y", "10", "/bin/sh", "-c",
                     "printf '\\033[1;31mGQCTL1\\033[0m \\033[1;4;32mGQCTL2\\033[0m \\033[1mGQCTL3\\033[0m\\n'; sleep 30")
            time.sleep(0.8)
            ccap = tm("capture-pane", "-e", "-p", "-t", "ctl").stdout
            rep["tmux"]["control_without_workbench"] = {"rc": ctl.returncode, "raw": [l[:200] for l in ccap.splitlines() if "GQCTL" in l]}
            # detach the product UI inside tmux, then end tmux
            tm("send-keys", "-t", "emph", "-H", "1d")
            tm("send-keys", "-t", "emph", "-l", "q")
            time.sleep(1.0)
            kill_server()
            bad = {f"{n}:{k}": v for n in ("alone", "coloured", "coloured2") for k, v in rep["tmux"][n]["verdict"].items()
                   if not v["strict"]}
            assert not bad, {"mismatch": bad}
            return {"tmux_verdicts": {n: {k: v["strict"] for k, v in rep["tmux"][n]["verdict"].items()}
                                      for n in ("alone", "coloured", "coloured2")}}

        rig.step("TMUX_capture_pane_e", tmux_phase, ("start",))
        rig.step("shutdown", rig.shutdown_clean, ("start",))
        rep["steps"] = {k: v.get("status") for k, v in rig.report.data["steps"].items()}
        try:
            fr.assert_steps(self, rig)
        finally:
            rep["steps_final"] = {k: v.get("status") for k, v in rig.report.data["steps"].items()}
            rep["step_detail_fail"] = {k: v for k, v in rig.report.data["steps"].items() if v.get("status") != "pass"}


if __name__ == "__main__":
    unittest.main()
