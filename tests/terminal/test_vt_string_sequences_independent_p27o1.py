"""Independent adversarial tests for p27-cd68-o1-fix-01 (DCS/SOS/PM/APC strings never become pane text).

Expectations come from the O1 observation (smoke-03) and the fix result: OMP 18.6.1 wraps its OSC 777 notification
in ``ESC P tmux; <payload, every ESC doubled> ESC \\`` (+ optional BEL) when TMUX is set; the pane VT stream must
swallow any DCS/SOS/PM/APC string up to ST (also across reads, CAN/SUB abort), keep memory flat on huge payloads and
leave all other output byte-for-byte as plain pyte would render it.

Rerun p27-cd68-o1-test-02 (corrected rules, derived independently): CAN/SUB always abort a string (also straight after
an ESC); inside a string ``ESC \\`` ends it, ``ESC ESC`` is tmux-doubled payload, ESC + any other byte ends the string
and that ESC is parsed again as a normal sequence (a lost ST never hides later output, RIS works); outside a string
``ESC ESC`` drops the first ESC (so ``ESC ESC P`` starts a DCS); a string is abandoned after 8 MiB.
"""

from __future__ import annotations

import itertools
import json
import random
import tracemalloc
import unittest

import pyte

from workbench.terminal.vt_g1.screen import TerminalByteStream, TerminalScreen, make_stream

PAYLOAD = json.dumps({"event": "stop", "query": "사용자 입력 원문", "response": "응답 \\ \" ; ] P tmux;"},
                     ensure_ascii=False)


def wrap(inner: bytes, intro: bytes = b"\x1bP", tmux: bool = True, st: bytes = b"\x1b\\") -> bytes:
    body = (b"tmux;" + inner.replace(b"\x1b", b"\x1b\x1b")) if tmux else inner
    return intro + body + st


def osc(num: bytes, body: str, end: bytes = b"\x07") -> bytes:
    return b"\x1b]" + num + b";" + body.encode() + end


NOTIFICATIONS = {
    "777-bel": osc(b"777", f"notify;warp://cli-agent;{PAYLOAD}"),
    "777-st": osc(b"777", f"notify;warp://cli-agent;{PAYLOAD}", b"\x1b\\"),
    "9-bel": osc(b"9", "build finished 한글"),
    "99-st": osc(b"99", "i=1:d=0;hello 한글", b"\x1b\\"),
    "99-bel": osc(b"99", ";body", b"\x07"),
}


def render(chunks, cols: int = 220, rows: int = 8):
    screen = TerminalScreen(cols, rows)
    stream = make_stream(screen)
    for chunk in chunks:
        stream.feed(chunk)
    return screen, stream


def text(chunks, **kw) -> list[str]:
    screen, _ = render(chunks, **kw)
    return [line.rstrip() for line in screen.display if line.strip()]


def plain_pyte(chunks, cols: int = 220, rows: int = 8) -> TerminalScreen:
    """The same stream class with the string filter bypassed (what pyte alone renders)."""
    screen = TerminalScreen(cols, rows)
    stream = make_stream(screen)
    for chunk in chunks:
        pyte.ByteStream.feed(stream, chunk)
    return screen


def cells(screen: TerminalScreen):
    return ([[(c.data, c.fg, c.bg, c.bold, c.reverse) for c in (screen.buffer[y][x] for x in range(screen.columns))]
             for y in range(screen.lines)], screen.cursor.x, screen.cursor.y)


def splits(data: bytes, rng: random.Random):
    out, i = [], 0
    while i < len(data):
        step = rng.choice((1, 1, 2, 3, 5, 8, 13, 40))
        out.append(data[i:i + step])
        i += step
    return out


class TmuxWrappedNotifications(unittest.TestCase):
    def test_every_notification_form_wrapped_in_tmux_passthrough_is_invisible(self):
        for name, inner in NOTIFICATIONS.items():
            for tail in (b"", b"\x07"):
                for lead in (b"", b"\r\n", b"\x1b[1m"):
                    with self.subTest(name=name, bel=bool(tail), lead=lead):
                        data = b"AAA" + lead + wrap(inner) + tail + b"BBB"
                        shown = text([data])
                        joined = "".join(shown)
                        for leaked in ("tmux", "777", "notify", "warp", "build finished", "hello", "body", "사용자",
                                       "응답", "\x1b", "\x07"):
                            self.assertNotIn(leaked, joined)
                        self.assertTrue(joined.startswith("AAA") and joined.endswith("BBB"), shown)

    def test_unwrapped_notifications_equal_the_wrapped_rendering(self):
        for name, inner in NOTIFICATIONS.items():
            with self.subTest(name=name):
                self.assertEqual(text([b"x" + inner + b"y"]), ["xy"])
                self.assertEqual(text([b"x" + wrap(inner) + b"y"]), ["xy"])

    def test_notification_after_other_output_leaves_cursor_and_cells_identical_to_no_notification(self):
        base = b"line1\r\n\x1b[31mred\x1b[0m \x1b[2;10Hx"
        for name, inner in NOTIFICATIONS.items():
            with self.subTest(name=name):
                a = cells(render([base + wrap(inner) + b"\x07tail"])[0])
                b = cells(render([base + b"tail"])[0])
                self.assertEqual(a, b)

    def test_several_notifications_back_to_back_and_interleaved_with_text(self):
        data = b"".join(b"<%d>" % i + wrap(inner) + b"\x07" for i, inner in enumerate(NOTIFICATIONS.values()))
        self.assertEqual(text([data]), ["<0><1><2><3><4>"])

    def test_inner_escaped_esc_backslash_is_not_the_end_of_the_string(self):
        # wrapped OSC with ST terminator: ... ESC ESC \  (doubled, belongs to the payload) then the real ESC \
        data = b"A" + wrap(NOTIFICATIONS["777-st"]) + b"B"
        self.assertIn(b"\x1b\x1b\\\x1b\\", data)
        self.assertEqual(text([data]), ["AB"])

    def test_payload_with_text_that_looks_like_control_and_csi(self):
        evil = b"\x1b[2J\x1b[H\x1b[31mDEL\x1b[1;1H\x1b[?1049h\x1bc\r\n\x08\x08\x08 gone"
        self.assertEqual(text([b"keep" + wrap(evil) + b"!"]), ["keep!"])
        screen, _ = render([b"\x1b[1mkeep" + wrap(evil) + b"!"])
        self.assertEqual((screen.cursor.x, screen.cursor.y), (5, 0))
        self.assertTrue(screen.buffer[0][0].bold, "SGR set before the string is still active (not reset by payload)")


class OtherStringIntroducers(unittest.TestCase):
    def test_dcs_sos_pm_apc_swallowed_with_st(self):
        for intro in (b"\x1bP", b"\x1bX", b"\x1b^", b"\x1b_"):
            for payload in (b"", b"plain text", b"\x1b\x1b[31mnot applied", "한글 payload".encode(), b"a\x00b\x7fc",
                            b"q#0;2;0;0;0#0~~@@vv-#0!14@"):
                with self.subTest(intro=intro, payload=payload[:10]):
                    self.assertEqual(text([b"A" + intro + payload + b"\x1b\\B"]), ["AB"])

    def test_bel_does_not_end_a_dcs_but_does_not_leak_either(self):
        # BEL terminates OSC only; inside DCS/APC it is payload, so the text after it stays hidden until ST.
        self.assertEqual(text([b"A\x1b_apc\x07hidden\x1b\\B"]), ["AB"])

    def test_string_state_is_per_stream_and_ends_with_st(self):
        screen, stream = render([b"A\x1bPpartial"])
        self.assertEqual(text([b"A\x1bPpartial"]), ["A"])
        stream.feed(b" more\x1b\\Z")
        self.assertEqual(screen.display[0].rstrip(), "AZ")
        other, _ = render([b"fresh"])
        self.assertEqual(other.display[0].rstrip(), "fresh", "a new stream does not inherit a string state")

    def test_nested_and_garbage_payloads(self):
        cases = {
            "nested DCS (ST ends the whole)": b"A\x1bPtmux;\x1bPinner\x1b\\B",
            "DCS inside APC": b"A\x1b_\x1bPx\x1b\\B",
            "many ESC P in a row": b"A" + b"\x1bP" * 5 + b"junk\x1b\\B",
            "garbage bytes": b"A\x1bP" + bytes(range(0x20, 0x7F)) + bytes([0x80, 0xFF, 0xC3]) + b"\x1b\\B",
            "ESC + CAN in string aborts": b"A\x1bPab\x1b\x18B",
            "ESC + SUB in string aborts": b"A\x1b_ab\x1b\x1aB",
            "doubled ESC then ST": b"A\x1bPab\x1b\x1b\\\x1b\\B",
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                shown = text([data])
                self.assertEqual(shown, ["AB"], shown)

    def test_esc_plus_other_byte_inside_a_string_ends_it_and_is_parsed_again(self):
        # corrected rule: the string ended without ST; `ESC [ 31 m`, `ESC ] 0;t BEL` are real sequences afterwards.
        screen, _ = render([b"A\x1bPab\x1b[31mcd\x1b]0;t\x07ef\x1b\\B"])
        self.assertEqual(screen.display[0].rstrip(), "AcdefB")
        self.assertEqual(screen.buffer[0][1].fg, "red")
        self.assertEqual(screen.buffer[0][0].fg, "default")
        self.assertEqual(screen.title, "t")
        for intro in (b"\x1bP", b"\x1bX", b"\x1b^", b"\x1b_"):
            for follower in (b"[", b"]", b"(", b"7", b"M", b"=", b"c", b"a", b"~", b" ", b"\x00", b"\x7f"):
                with self.subTest(intro=intro, follower=follower):
                    shown = text([b"A" + intro + b"xy\x1b" + follower + b"Z"])
                    expected = text([b"A\x1b" + follower + b"Z"])
                    self.assertEqual(shown, expected)
                    # and split right after the ESC
                    self.assertEqual(text([b"A" + intro + b"xy\x1b", follower + b"Z"]), expected)

    def test_esc_plus_another_introducer_inside_a_string_starts_a_new_string(self):
        self.assertEqual(text([b"A\x1bPx\x1bPy\x1bXz\x1b\\B"]), ["AB"])
        self.assertEqual(text([b"A\x1b_x\x1b^y\x1bPz\x18B"]), ["AB"])

    def test_esc_p_used_as_charset_or_sharp_or_plain_text_is_not_a_string(self):
        self.assertEqual(text([b"P X ^ _ done"]), ["P X ^ _ done"])
        self.assertEqual(text([b"\x1b(Bregular \x1b)0P\x1b#8"]), [line.rstrip() for line in plain_pyte(
            [b"\x1b(Bregular \x1b)0P\x1b#8"]).display if line.strip()])
        self.assertEqual(text([b"a\x1b7b\x1b8cP"]), ["acP"])  # DECSC/DECRC (c overwrites b); the P after is plain text
        self.assertEqual(text([b"\x1b[31mP\x1b[0m"]), ["P"])
        self.assertEqual(text([b"x\x1b[10;5HX^"]), [line.rstrip() for line in plain_pyte(
            [b"x\x1b[10;5HX^"]).display if line.strip()])


class AbortAndTermination(unittest.TestCase):
    def test_can_and_sub_abort_in_every_string_kind_and_position(self):
        for intro in (b"\x1bP", b"\x1bX", b"\x1b^", b"\x1b_"):
            for abort in (b"\x18", b"\x1a"):
                with self.subTest(intro=intro, abort=abort):
                    self.assertEqual(text([b"A" + intro + b"never closed" + abort + b"B"]), ["AB"])
                    # abort byte at the start of the next read
                    self.assertEqual(text([b"A" + intro + b"never", abort + b"B"]), ["AB"])
                    # abort right after the introducer
                    self.assertEqual(text([b"A" + intro + abort + b"B"]), ["AB"])

    def test_abort_after_a_string_that_already_ended_still_acts_as_pyte_would(self):
        self.assertEqual(text([b"A\x1bPx\x1b\\\x18B"]), text([b"A\x18B"]))

    def test_after_abort_new_strings_work_again(self):
        self.assertEqual(text([b"A\x1bPx\x18B\x1bPy\x1b\\C"]), ["ABC"])

    def test_lone_st_and_lone_esc_backslash_outside_a_string_are_harmless(self):
        self.assertEqual(text([b"A\x1b\\B"]), [line.rstrip() for line in plain_pyte([b"A\x1b\\B"]).display
                                               if line.strip()])

    def test_unterminated_string_hides_following_output_until_abort(self):
        screen, stream = render([b"A\x1bPtmux;never ends"])
        stream.feed(b"later output\r\n" * 3)
        self.assertEqual(screen.display[0].rstrip(), "A")
        stream.feed(b"\x18")
        stream.feed(b"visible")
        self.assertEqual(screen.display[0].rstrip(), "Avisible")

    def test_esc_then_can_or_sub_inside_a_string_aborts_it(self):
        # was a pinned known edge; corrected rule: CAN/SUB always abort, also straight after an ESC.
        for intro in (b"\x1bP", b"\x1bX", b"\x1b^", b"\x1b_"):
            for abort in (b"\x18", b"\x1a"):
                with self.subTest(intro=intro, abort=abort):
                    self.assertEqual(text([b"A" + intro + b"xx\x1b" + abort + b"B"]), ["AB"])
                    self.assertEqual(text([b"A" + intro + b"xx\x1b", abort + b"B"]), ["AB"], "split after ESC")
                    self.assertEqual(text([b"A" + intro + b"\x1b" + abort + b"B"]), ["AB"], "ESC right after intro")
                    self.assertEqual(text([b"A" + intro + b"xx\x1b\x1b" + abort + b"B"]), ["AB"], "after a doubled ESC")

    def test_esc_esc_p_outside_a_string_is_a_dcs(self):
        # was a pinned known edge; the second ESC restarts the sequence so `ESC ESC P ... ST` is a DCS.
        self.assertEqual(text([b"A\x1b\x1bPtmux;x\x1b\\B"]), ["AB"])
        self.assertEqual(text([b"A\x1b", b"\x1bPtmux;x\x1b\\B"]), ["AB"])
        self.assertEqual(text([b"A\x1b\x1b", b"P", b"tmux;x\x1b\\B"]), ["AB"])
        self.assertEqual(text([b"A\x1b\x1b\x1b\x1bXsos\x1b\\B"]), ["AB"], "any run of ESC collapses")
        for intro in (b"P", b"X", b"^", b"_"):
            with self.subTest(intro=intro):
                self.assertEqual(text([b"A\x1b\x1b" + intro + b"payload\x1b\\B"]), ["AB"])

    def test_esc_esc_outside_a_string_acts_like_a_single_esc_for_other_sequences(self):
        for tail in (b"[31mred\x1b[0m!", b"]0;ttl\x07z", b"7a\x1b8b", b"(0lq", b"cX", b"[2J[Hq", b"M", b"\\"):
            with self.subTest(tail=tail):
                self.assertEqual(cells(render([b"x\x1b\x1b" + tail])[0]), cells(plain_pyte([b"x\x1b" + tail])))
                self.assertEqual(cells(render([b"x\x1b", b"\x1b" + tail])[0]), cells(plain_pyte([b"x\x1b" + tail])))

    def test_lost_st_never_blanks_the_pane(self):
        # review P2: `ESC P x ESC c` / crashed writer: later output (ESC c, ESC [ ...) must stay visible.
        screen, _ = render([b"old\r\nline\x1bPtmux;partial payload\x1bcRESET"])
        self.assertEqual([l.rstrip() for l in screen.display if l.strip()], ["RESET"], "RIS works and clears the screen")
        screen, _ = render([b"A\x1bPtmux;partial\x1b[31mred\x1b[0m B\r\nnext line"])
        self.assertEqual([l.rstrip() for l in screen.display if l.strip()], ["Ared B", "next line"])
        self.assertEqual(screen.buffer[0][1].fg, "red")
        screen, _ = render([b"A\x1b]0;x\x07\x1b_apc lost", b"\x1b[2;3Hmoved"])
        self.assertEqual(screen.display[1][2:7], "moved")
        for lost in (b"\x1bP", b"\x1bX", b"\x1b^", b"\x1b_"):
            for cont in (b"\x1bc", b"\x1b[1;1H", b"\x1b[2J", b"\x1b[31m", b"\x1b]0;t\x07", b"\x1b7", b"\x1bM"):
                with self.subTest(lost=lost, cont=cont):
                    self.assertEqual(text([b"A" + lost + b"unfinished" + cont + b"visible"]),
                                     text([b"A" + cont + b"visible"]))

    def test_ris_works_after_an_aborted_string(self):
        for lost in (b"\x1bPx\x18", b"\x1b_x\x1a", b"\x1bPx\x1b\x18", b"\x1bPx\x1b\\", b"\x1bPx\x1b[0m"):
            with self.subTest(lost=lost):
                screen, _ = render([b"junk\r\nmore" + lost + b"\x1bc" + b"after"])
                self.assertEqual([l.rstrip() for l in screen.display if l.strip()], ["after"])
                screen, _ = render([b"junk\r\nmore" + lost, b"\x1b", b"cafter"])
                self.assertEqual([l.rstrip() for l in screen.display if l.strip()], ["after"], "ESC | c split")

    def test_string_ended_by_cap_leaves_the_stream_usable(self):
        screen, stream = render([b"keep\r\n\x1bP" + b"q" * ((8 << 20) + 65536)])
        stream.feed(b"\x1b[2J\x1b[Hfine")
        self.assertEqual([l.rstrip() for l in screen.display if l.strip()], ["fine"])
        self.assertFalse(getattr(stream, "_pending_esc", False))


class ChunkSplitting(unittest.TestCase):
    def _samples(self):
        yield b"A" + wrap(NOTIFICATIONS["777-bel"]) + b"\x07B"
        yield b"A" + wrap(NOTIFICATIONS["777-st"]) + b"B"
        yield b"A\x1b_apc\x1b\\B\x1bXsos\x1b\\C\x1b^pm\x1b\\D"
        yield b"A\x1bPx\x1b\x1b\x1b\\B"  # ESC ESC then ST
        yield "가\x1bPtmux;한글\x1b\\나".encode()
        yield b"A\x1bPxx\x1b\x18B\x1b_yy\x1b\x1aC"  # ESC CAN / ESC SUB aborts
        yield b"A\x1b\x1bPtmux;x\x1b\\B\x1b\x1b\x1b[31mC"  # ESC ESC P and ESC ESC ESC [
        yield b"A\x1bPlost\x1b[31mred\x1bcB\x1b_lost2\x1b]0;t\x07C"  # lost ST

    def test_byte_at_a_time_and_every_two_way_split_equal_the_unsplit_rendering(self):
        for data in self._samples():
            reference = cells(render([data])[0])
            with self.subTest(sample=data[:14], mode="bytes"):
                self.assertEqual(cells(render([data[i:i + 1] for i in range(len(data))])[0]), reference)
            for cut in range(1, len(data)):
                with self.subTest(sample=data[:14], cut=cut):
                    self.assertEqual(cells(render([data[:cut], data[cut:]])[0]), reference)

    def test_every_three_way_split_around_each_escape(self):
        data = b"A" + wrap(NOTIFICATIONS["777-st"]) + b"\x07B"
        reference = cells(render([data])[0])
        escapes = [i for i, b in enumerate(data) if b == 0x1B]
        for e in escapes:
            for a in (e - 1, e, e + 1, e + 2):
                for b in (a + 1, a + 2):
                    if 0 < a < b < len(data):
                        with self.subTest(esc=e, a=a, b=b):
                            self.assertEqual(cells(render([data[:a], data[a:b], data[b:]])[0]), reference)

    def test_split_exactly_between_esc_and_its_second_byte_inside_and_outside_strings(self):
        pairs = [(b"A\x1b", b"PxyZ\x1b\\B"), (b"A\x1bPxy\x1b", b"\\B"), (b"A\x1bPxy\x1b", b"\x1b\\B"),
                 (b"A\x1bPxy\x1b\x1b", b"\\\x1b\\B"), (b"A\x1bPxy\x1b", b"\x1b"), (b"A\x1b", b"\x1b\\"),
                 (b"A\x1b", b"[31mred")]
        for first, second in pairs:
            with self.subTest(first=first, second=second):
                self.assertEqual(cells(render([first, second])[0]), cells(render([first + second])[0]))
        self.assertEqual(text([b"A\x1bPxy\x1b", b"\x1b", b"\\", b"\x1b", b"\\B"]), ["AB"])
        self.assertEqual(text([b"A\x1b", b"", b"", b"P", b"", b"x\x1b", b"\\B"]), ["AB"], "empty reads are no-ops")

    def test_empty_feed_keeps_a_pending_esc(self):
        screen, stream = render([b"A\x1b"])
        for _ in range(3):
            stream.feed(b"")
        stream.feed(b"[1mB")
        self.assertEqual(screen.display[0].rstrip(), "AB")
        self.assertTrue(screen.buffer[0][1].bold)

    def test_seeded_random_splits_of_a_mixed_stream_equal_the_unsplit_rendering(self):
        stream_bytes = (b"\x1b[1;32mstart\x1b[0m\r\n" + wrap(NOTIFICATIONS["777-bel"]) + "\x07한글 text\r\n".encode()
                        + b"\x1b_apc\x1b\\" + b"\x1b]0;title\x07" + wrap(NOTIFICATIONS["99-st"]) + b"\x1b[3;4Hmid"
                        + wrap(NOTIFICATIONS["9-bel"]) + "\x1bPq\x18end 끝\r\n".encode())
        reference = cells(render([stream_bytes])[0])
        for seed in range(300):
            rng = random.Random(seed)
            with self.subTest(seed=seed):
                self.assertEqual(cells(render(splits(stream_bytes, rng))[0]), reference)

    def test_random_fuzz_with_string_fragments_matches_a_reference_filter(self):
        """Differential fuzz against an independent, simple state-machine reference of the corrected rules."""
        rng = random.Random(0x270A2)
        atoms = [b"a", b"B", " ", "한".encode(), b"\r\n", b"\x1b[31m", b"\x1b[0m", b"\x1b[2;3H", b"\x1b7", b"\x1b8",
                 b"\x1bP", b"\x1bX", b"\x1b^", b"\x1b_", b"\x1b\\", b"\x1b\x1b", b"\x18", b"\x1a", b"\x07", b"P", b"\\",
                 b"\x1b]0;t\x07", b"\x1b]777;n;b\x07", b"tmux;", b"\x1b", b"\x1b\x18", b"\x1b\x1a", b"\x1bc",
                 b"\x1b[", b"\x1b]", b"\x1b\x1b\\"]
        atoms = [a.encode() if isinstance(a, str) else a for a in atoms]
        for case in range(600):
            data = b"".join(rng.choice(atoms) for _ in range(rng.randint(1, 40)))
            expected = reference_filter(data)
            for mode in ("whole", "split"):
                chunks = [data] if mode == "whole" else splits(data, rng)
                with self.subTest(case=case, mode=mode, data=data):
                    got = bytes(drop_via_stream(chunks))
                    self.assertEqual(got, expected)
            with self.subTest(case=case, mode="render", data=data):
                # what the pane shows equals plain pyte fed the reference bytes (single read: pyte itself renders
                # differently when a read boundary follows CAN/SUB, independent of this filter)
                self.assertEqual(cells(render([data])[0]), cells(plain_pyte([expected])))

    def test_exhaustive_short_sequences_match_the_reference_for_every_split(self):
        full = [b"\x1b", b"P", b"\\", b"\x18", b"[", b"x", b"c"]
        small = [b"\x1b", b"P", b"\\", b"\x18", b"x"]
        shapes = [(length, full) for length in range(1, 5)] + [(5, small)]
        for length, alphabet in shapes:
            for combo in itertools.product(alphabet, repeat=length):
                data = b"".join(combo)
                expected = reference_filter(data)
                for cut in range(len(data) + 1):
                    got = bytes(drop_via_stream([data[:cut], data[cut:]]))
                    if got != expected:
                        self.fail(f"data={data!r} cut={cut} got={got!r} expected={expected!r}")


def reference_filter(data: bytes) -> bytes:
    """Reference state machine written from the rules (not from the implementation).

    OUT: ESC -> OUT_ESC (withheld); other bytes forwarded.
    OUT_ESC: ESC -> stays (the earlier ESC is cancelled); P X ^ _ -> IN_STR; any other byte -> forward ESC+byte.
    IN_STR: CAN/SUB -> OUT; ESC -> STR_ESC; anything else is payload.
    STR_ESC: `\\` -> OUT (ST); ESC -> IN_STR (tmux-doubled payload); CAN/SUB -> OUT; any other byte -> the string
    ended, the ESC is parsed again together with that byte (as OUT_ESC would).
    A trailing withheld ESC is not forwarded (the stream keeps it until more data arrives)."""
    out = bytearray()
    state = "OUT"
    for b in data:
        if state == "OUT":
            if b == 0x1B:
                state = "OUT_ESC"
            else:
                out.append(b)
        elif state == "STR_ESC":
            if b == 0x5C or b in (0x18, 0x1A):
                state = "OUT"
            elif b == 0x1B:
                state = "IN_STR"
            else:
                state = "OUT_ESC"
                # re-parse `ESC b` as OUT_ESC does
                if b in b"PX^_":
                    state = "IN_STR"
                else:
                    out += bytes((0x1B, b))
                    state = "OUT"
        elif state == "IN_STR":
            if b in (0x18, 0x1A):
                state = "OUT"
            elif b == 0x1B:
                state = "STR_ESC"
        else:  # OUT_ESC
            if b == 0x1B:
                pass
            elif b in b"PX^_":
                state = "IN_STR"
            else:
                out += bytes((0x1B, b))
                state = "OUT"
    return bytes(out)


def drop_via_stream(chunks):
    """What the stream hands to pyte, observed by recording the bytes reaching ``pyte.ByteStream.feed``."""
    got = bytearray()

    class Spy(TerminalByteStream):
        def feed(self, data):  # type: ignore[override]
            got.extend(self._drop_strings(bytes(data)))

    stream = Spy(TerminalScreen(80, 5))
    for chunk in chunks:
        stream.feed(chunk)
    return got


class LongPayloads(unittest.TestCase):
    def test_huge_payload_is_not_buffered_and_memory_stays_flat(self):
        stream = make_stream(TerminalScreen(120, 6))
        stream.feed(b"warm\r\n")
        block = b"x" * 65536
        tracemalloc.start()
        try:
            stream.feed(b"A\x1bPtmux;\x1b\x1b]777;notify;")
            base = tracemalloc.get_traced_memory()[0]
            for _ in range(96):  # 6 MiB of payload in 64 KiB reads
                stream.feed(block)
            _, peak = tracemalloc.get_traced_memory()
            stream.feed(b"\x07\x1b\x1b\\\x1b\\Z")
        finally:
            tracemalloc.stop()
        self.assertLess(peak - base, 512 * 1024, f"payload bytes were retained: {peak - base}")
        self.assertEqual(stream.listener.display[0].rstrip(), "warm")
        self.assertEqual(stream.listener.display[1].rstrip(), "AZ")

    def test_payload_of_only_escapes_and_a_single_giant_read(self):
        big = b"\x1b\x1b" * 1_000_000
        self.assertEqual(text([b"A\x1bPtmux;" + big + b"\x1b\\B"]), ["AB"])
        self.assertEqual(text([b"A\x1b_" + b"y" * 8_000_000 + b"\x1b\\B"]), ["AB"])

    def test_giant_payload_with_split_inside_and_unterminated_end_leaves_no_residue(self):
        screen, stream = render([b"A\x1bP" + b"z" * 3_000_000])
        self.assertEqual(screen.display[0].rstrip(), "A")
        self.assertEqual(getattr(stream, "_pending_esc", None), False)
        stream.feed(b"\x1a")
        stream.feed(b"B")
        self.assertEqual(screen.display[0].rstrip(), "AB")

    def test_string_cap_is_8_mib_a_long_string_below_it_stays_hidden_a_longer_one_is_abandoned(self):
        cap = 8 << 20
        block = b"y" * (1 << 20)
        # well below the cap: everything up to ST is hidden, even when it arrives in many reads
        screen, stream = render([b"A\x1b_"])
        for _ in range(7):
            stream.feed(block)
        stream.feed(b"\x1b\\B")
        self.assertEqual(screen.display[0].rstrip(), "AB")
        # unterminated and far beyond the cap: the tail is shown again (the pane is not blank forever)
        screen, stream = render([b"A\x1bP"])
        for _ in range(9):
            stream.feed(block)
        stream.feed(b"\x1b[2J\x1b[H")
        stream.feed(b"visible")
        self.assertEqual(screen.display[0].rstrip(), "visible")
        self.assertEqual(cap, 8388608)

    def test_cap_abandonment_shows_only_the_overflow_not_the_hidden_part(self):
        cap = 8 << 20
        extra = 300
        screen = TerminalScreen(400, 4)
        stream = make_stream(screen)
        stream.feed(b"\x1bP" + b"h" * (cap - 1000) + b"|")
        stream.feed(b"|" * 2000)
        stream.feed(b"END")
        shown = "".join(line.rstrip() for line in screen.display) + "".join(
            "".join(c.data for c in line.values()) for line in screen.history.top)
        self.assertNotIn("h", shown, "payload below the cap is never shown")
        self.assertIn("END", shown)

    def test_many_notifications_do_not_grow_state(self):
        screen, stream = render([b""])
        for i in range(20000):
            stream.feed(wrap(NOTIFICATIONS["777-bel"]) + b"\x07")
        stream.feed(b"done")
        self.assertEqual(screen.display[0].rstrip(), "done")
        self.assertEqual(len(screen.history.top), 0)
        self.assertEqual(sum(1 for line in screen.display if line.strip()), 1)

    def test_filter_cost_is_linear_not_quadratic(self):
        import time
        t0 = time.perf_counter()
        TerminalByteStream(TerminalScreen(80, 5))._drop_strings(b"\x1bP" + b"\x1b\x1b" * 1_500_000 + b"\x1b\\")
        self.assertLess(time.perf_counter() - t0, 10.0)


class Utf8Safety(unittest.TestCase):
    KOREAN = "한국어 테스트: 가나다라마바사 ㅎㅏㄴ 😀 mixed English"

    def test_korean_text_split_at_every_byte_around_a_string_matches_unsplit(self):
        data = (self.KOREAN + "\x1bPtmux;" + "페이로드 한글").encode() + b"\x1b\\" + ("끝 " + self.KOREAN).encode()
        reference = text([data], cols=200)
        self.assertEqual(reference, [self.KOREAN + "끝 " + self.KOREAN])
        for cut in range(1, len(data)):
            with self.subTest(cut=cut):
                self.assertEqual(text([data[:cut], data[cut:]], cols=200), reference)

    def test_every_hangul_syllable_survives_and_continuation_bytes_are_not_introducers(self):
        # Hangul/emoji UTF-8 continuation bytes include 0x90 0x98 0x9E 0x9F (C1 look-alikes) and 0x5C-free ranges.
        syllables = "".join(chr(c) for c in range(0xAC00, 0xD7A4))
        screen = TerminalScreen(2000, 24)
        stream = make_stream(screen)
        for start in range(0, len(syllables), 700):
            part = syllables[start:start + 700].encode()
            stream.feed(part[:1])
            stream.feed(part[1:])
        shown = "".join(line.rstrip() for line in screen.display if line.strip())
        expected_plain = plain_pyte([syllables[s:s + 700].encode() for s in range(0, len(syllables), 700)],
                                    cols=2000, rows=24)
        self.assertEqual(shown, "".join(line.rstrip() for line in expected_plain.display if line.strip()))
        self.assertEqual(len(shown), len(syllables) - sum(1 for ch in syllables if ch == " "))

    def test_ascii_bytes_that_equal_introducers_inside_utf8_never_matter(self):
        # 0x50 'P', 0x58 'X', 0x5E '^', 0x5F '_' are only introducers right after ESC.
        self.assertEqual(text(["가PX^_나".encode()]), ["가PX^_나"])

    def test_korean_inside_the_string_split_in_the_middle_of_a_character(self):
        korean = "사용자 입력 원문".encode()
        data = b"A\x1bPtmux;" + korean + b"\x1b\\B"
        for cut in range(len(b"A\x1bPtmux;"), len(b"A\x1bPtmux;") + len(korean) + 1):
            with self.subTest(cut=cut):
                self.assertEqual(text([data[:cut], data[cut:]]), ["AB"])

    def test_invalid_utf8_inside_and_outside_strings_does_not_raise(self):
        text([b"\xff\xfe\x80A\x1bP\xc3\x1b\\\xc3B"])
        text([b"\xe3\x81" + b"\x1bP", b"\x81\x1b\\"])


class OrdinaryOutputUnchanged(unittest.TestCase):
    """Differential: output without any DCS/SOS/PM/APC string renders exactly as plain pyte, for any chunking."""

    SAMPLES = [
        b"\x1b[1mbold\x1b[0m \x1b[38;5;196mred256\x1b[0m \x1b[38;2;1;2;3mrgb\x1b[39m \x1b[7mrev\x1b[27m",
        b"\x1b]0;window title\x07\x1b]2;other\x1b\\after",
        b"line1\r\nline2\x1b[A\x1b[3Cmid\x1b[2;1H\x1b[K\x1b[1;1H\x1b[2J\x1b[Hhome",
        "한글 \x1b[31m빨강\x1b[0m 뒤 \x1b[5D\x1b[2P지움".encode(),
        b"\x1b[?1049h\x1b[?25l alt \x1b[?25h\x1b[?1049l main",
        b"\x1b7saved\x1b[3;3H*\x1b8restored\x1bM\x1bD\x1bE\x1b(0lqk\x1b(B x",
        b"\x1b[1;3r\n\n\n\n\n\x1b[S\x1b[T\x1b[r",
        b"\x1b]52;c;aGVsbG8=\x07shown\x1b]52;c;?\x1b\\!",
        b"tab\there\x08\x08xx\x07bell\x0b\x0c",
        b"\x1b[6n\x1b[c\x1b[?2004h\x1b[?2004l\x1b=\x1b>",
    ]

    def test_exact_equality_with_plain_pyte_whole_and_random_chunks(self):
        for index, data in enumerate(self.SAMPLES):
            reference = cells(plain_pyte([data]))
            with self.subTest(sample=index, mode="whole"):
                self.assertEqual(cells(render([data])[0]), reference)
            for seed in range(25):
                with self.subTest(sample=index, seed=seed):
                    self.assertEqual(cells(render(splits(data, random.Random(seed)))[0]), reference)

    def test_title_is_still_set_by_osc_0_and_2(self):
        screen, _ = render([b"\x1b]0;first\x07x", b"\x1b]2;second\x1b\\y"])
        self.assertEqual(screen.title, "second")
        screen, _ = render([b"\x1b]0;tit", b"le\x07"])
        self.assertEqual(screen.title, "title")
        screen, _ = render([b"\x1b]2;a", b"\x1b", b"\\"])
        self.assertEqual(screen.title, "a")

    def test_osc52_from_a_pane_stays_invisible_and_is_not_confused_with_strings(self):
        for end in (b"\x07", b"\x1b\\"):
            with self.subTest(end=end):
                self.assertEqual(text([b"A\x1b]52;c;aGVsbG8=" + end + b"B"]), ["AB"])
                self.assertEqual(text([b"A\x1b]52;c;aGVs", b"bG8=" + end[:1], end[1:] + b"B"]), ["AB"])

    def test_screen_after_strings_keeps_working_for_scroll_history_and_resize(self):
        screen, stream = render([b""], cols=40, rows=4)
        for i in range(30):
            stream.feed(b"row %02d\r\n" % i + wrap(NOTIFICATIONS["777-bel"]) + b"\x07")
        stream.feed(b"last")
        self.assertGreater(len(screen.history.top), 20)
        self.assertEqual(screen.display[-1].rstrip(), "last")
        screen.resize(lines=6, columns=30)
        stream.feed(b"\r\nresized" + wrap(NOTIFICATIONS["99-st"]))
        self.assertIn("resized", [line.rstrip() for line in screen.display])

    def test_all_kinds_of_chunk_types_are_accepted(self):
        for wrapper in (bytes, bytearray):
            with self.subTest(wrapper=wrapper.__name__):
                screen = TerminalScreen(40, 3)
                stream = make_stream(screen)
                stream.feed(wrapper(b"A" + wrap(NOTIFICATIONS["777-bel"]) + b"\x07B"))
                self.assertEqual(screen.display[0].rstrip(), "AB")


if __name__ == "__main__":
    unittest.main()
