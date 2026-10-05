"""Offline test suite for burner bin/burner.

100% OFFLINE GUARANTEE: every device/network touchpoint is guarded in
OfflineTestCase.setUp -- pc.adb, pc.adb_or_ensure, pc.u2sock, pc.ui_dump,
pc.fast_dump, pc.subprocess, pc.wake/wake_async, pc.tap_center,
pc.u2_invalidate, pc.scrcpy_send, pc.ensure, and socket.socket all raise
AssertionError if called without an explicit mock. Any test that accidentally
attempts live I/O fails loudly instead of hanging on a real connection.

Run from the repo root:  python3 -m unittest discover -s tests
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from importlib.machinery import SourceFileLoader
from types import SimpleNamespace
from unittest import mock

_PC_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "burner")


def _load_pc():
    # bin/burner has no .py extension, so spec_from_file_location can't find a
    # loader -- use SourceFileLoader explicitly.
    loader = SourceFileLoader("pc_under_test", _PC_PATH)
    spec = importlib.util.spec_from_loader("pc_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pc_under_test"] = mod
    loader.exec_module(mod)
    return mod


pc = _load_pc()
ROOT = pc.ROOT

SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.example" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focused="false" focusable="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[0,0][1080,2400]">
    <node index="0" text="Hello" resource-id="com.example:id/title" class="android.widget.TextView" package="com.example" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focused="false" focusable="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[100,200][500,300]"/>
    <node index="1" text="OK" resource-id="com.example:id/ok" class="android.widget.Button" package="com.example" content-desc="" checkable="false" checked="false" clickable="true" enabled="true" focused="false" focusable="true" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[100,400][400,500]"/>
    <node index="2" text="" resource-id="com.example:id/q" class="android.widget.EditText" package="com.example" content-desc="Search" checkable="false" checked="false" clickable="true" enabled="true" focused="true" focusable="true" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[100,600][900,700]"/>
  </node>
</hierarchy>"""

TAP_XML = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" clickable="false" enabled="true" focused="false" checked="false">
    <node text="Not now" class="android.widget.Button" bounds="[100,200][300,400]" clickable="true" enabled="true" focused="false" checked="false"/>
  </node>
</hierarchy>"""

OVERLAY_XML = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" clickable="false" enabled="true" focused="false" checked="false">
    <node text="Hi" class="android.widget.Button" bounds="[100,200][300,400]" clickable="true" enabled="true" focused="false" checked="false"/>
    <node text="Dialog" class="android.widget.FrameLayout" bounds="[200,900][880,1500]" clickable="false" enabled="true" focused="false" checked="false"/>
  </node>
</hierarchy>"""

TOOLBAR_XML = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" clickable="false" enabled="true" focused="false" checked="false">
    <node text="" class="android.view.ViewGroup" bounds="[0,0][1080,200]" clickable="false" enabled="true" focused="false" checked="false">
      <node text="Search" class="android.widget.EditText" bounds="[100,40][980,160]" clickable="true" enabled="true" focused="false" checked="false"/>
    </node>
  </node>
</hierarchy>"""


def _wnode(text="", desc="", cls="android.widget.TextView", clickable=False,
           enabled=True, bounds="[0,0][10,10]", center=(5, 5)):
    """Build a walk()-style node dict."""
    return {"text": text, "desc": desc, "rid": "", "class": cls,
            "bounds": bounds, "center": center, "clickable": clickable,
            "enabled": enabled, "focused": False, "checked": False,
            "parents": []}


class OfflineGuardTripped(BaseException):
    """Not an Exception: code that catches Exception (the read after an
    action does, on purpose) must not be able to swallow a tripped guard."""


class OfflineTestCase(unittest.TestCase):
    """Installs loud offline guards around every live-I/O entry point."""

    GUARDS = ["subprocess", "u2sock", "adb", "adb_or_ensure", "ui_dump",
              "fast_dump", "wake", "wake_async", "tap_center",
              "u2_invalidate", "scrcpy_send", "ensure", "phone_api",
              "reenable_debugging"]

    def setUp(self):
        super().setUp()
        self._guards = {}
        for name in self.GUARDS:
            p = mock.patch.object(pc, name)
            m = p.start()
            m.side_effect = OfflineGuardTripped(
                "OFFLINE GUARD TRIPPED: pc.%s called without a mock "
                "(live I/O attempted)" % name)
            self._guards[name] = p
            self.addCleanup(p.stop)
        p = mock.patch("socket.socket")
        m = p.start()
        m.side_effect = OfflineGuardTripped(
            "OFFLINE GUARD TRIPPED: socket.socket called (live I/O attempted)")
        self._guards["socket.socket"] = p
        self.addCleanup(p.stop)
        # No test writes the real run/commands.log.
        p = mock.patch.object(pc, "log_command")
        p.start()
        self.addCleanup(p.stop)
        # The reason the phone's helper app last gave doesn't leak between tests.
        p = mock.patch.object(pc, "_phone_api_why", "")
        p.start()
        self.addCleanup(p.stop)

    def allow(self, name, **kwargs):
        """Replace the guard on pc.<name> (or plain attribute) with a
        working mock; return it."""
        if name in self._guards:
            self._guards[name].stop()
        p = mock.patch.object(pc, name, **kwargs)
        m = p.start()
        self.addCleanup(p.stop)
        return m

    def parse(self, argv):
        return pc.build_parser().parse_args(argv)

    @contextlib.contextmanager
    def cap(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            yield out, err


# ------------------------------------------------------- 1. compact dump

class CompactDumpTests(OfflineTestCase):
    def test_is_interactive_clickable(self):
        self.assertTrue(pc.is_interactive(_wnode(cls="android.widget.TextView",
                                                 clickable=True)))

    def test_is_interactive_edittext(self):
        self.assertTrue(pc.is_interactive(_wnode(cls="android.widget.EditText",
                                                 clickable=False)))

    def test_is_interactive_button_classes(self):
        for cls in ("Button", "CheckBox", "Switch", "SeekBar"):
            with self.subTest(cls=cls):
                self.assertTrue(pc.is_interactive(
                    _wnode(cls="android.widget." + cls, clickable=False)))

    def test_is_interactive_plain_textview_false(self):
        # A plain TextView with text is NOT interactive per is_interactive
        # (compact_visible shows it because it has text -- tested below).
        self.assertFalse(pc.is_interactive(_wnode(text="Hello",
                                                  cls="android.widget.TextView",
                                                  clickable=False)))

    def test_is_interactive_imagebutton_false(self):
        # Short class name must match exactly; ImageButton is not Button.
        self.assertFalse(pc.is_interactive(_wnode(cls="android.widget.ImageButton",
                                                  clickable=False)))

    def test_compact_bounds_str(self):
        self.assertEqual(pc.compact_bounds_str("[12,34][56,78]"),
                         "12,34-56,78")

    def test_compact_bounds_str_empty(self):
        self.assertEqual(pc.compact_bounds_str(""), "")

    def test_compact_bounds_str_garbage(self):
        self.assertEqual(pc.compact_bounds_str("not-bounds"), "")

    def test_format_compact_shape(self):
        n = _wnode(text="OK", cls="android.widget.Button", clickable=True,
                   bounds="[100,400][300,500]", center=(200, 450))
        self.assertEqual(pc.format_compact(n), "OK (click) [Button] (200,450)")

    def test_format_compact_omits_empty_flags(self):
        n = _wnode(text="Hello", cls="android.widget.TextView",
                   clickable=False, center=(300, 250))
        self.assertEqual(pc.format_compact(n), "Hello [TextView] (300,250)")

    def test_format_compact_disabled_flag(self):
        n = _wnode(text="OK", cls="android.widget.Button", clickable=True,
                   enabled=False, center=(200, 450))
        self.assertEqual(pc.format_compact(n),
                         "OK (click,disabled) [Button] (200,450)")

    def test_format_compact_show_bounds(self):
        n = _wnode(text="OK", cls="android.widget.Button", clickable=True,
                   bounds="[100,400][300,500]", center=(200, 450))
        self.assertEqual(pc.format_compact(n, show_bounds=True),
                         "OK (click) [Button] 100,400-300,500 (200,450)")

    def test_format_compact_missing_center(self):
        n = _wnode(text="X", cls="android.widget.TextView", center=(None, None))
        self.assertEqual(pc.format_compact(n), "X [TextView] (?,?)")

    def test_format_compact_desc_label(self):
        n = _wnode(desc="Search", cls="android.widget.EditText",
                   clickable=True, center=(500, 650))
        self.assertEqual(pc.format_compact(n),
                         "[Search] (click) [EditText] (500,650)")

    def test_node_compact_full(self):
        n = _wnode(text="OK", cls="android.widget.Button", clickable=True,
                   center=(200, 450))
        self.assertEqual(pc.node_compact(n),
                         {"class": "Button", "text": "OK",
                          "x": 200, "y": 450, "clickable": True})

    def test_node_compact_omits_empty_fields(self):
        n = _wnode(cls="android.widget.TextView", enabled=False,
                   center=(None, None))
        self.assertEqual(pc.node_compact(n),
                         {"class": "TextView", "enabled": False})

    def test_compact_visible_include_all(self):
        self.assertTrue(pc.compact_visible(_wnode(), include_all=True))

    def test_compact_visible_interactive(self):
        self.assertTrue(pc.compact_visible(
            _wnode(cls="android.widget.Button", clickable=True),
            include_all=False))

    def test_compact_visible_text(self):
        self.assertTrue(pc.compact_visible(_wnode(text="Hello"),
                                           include_all=False))

    def test_compact_visible_desc(self):
        self.assertTrue(pc.compact_visible(_wnode(desc="Search"),
                                           include_all=False))

    def test_compact_visible_empty_filtered(self):
        self.assertFalse(pc.compact_visible(_wnode(), include_all=False))

    def test_compact_token_reduction(self):
        # 200-node synthetic dump: compact rendering must be <50% of the
        # raw XML dump's char count (the actual token win), and strictly
        # shorter than the --verbose line format.
        parts = ['<hierarchy rotation="0">']
        for i in range(200):
            parts.append(
                '<node text="Item number %d" class="android.widget.TextView" '
                'clickable="false" enabled="true" bounds="[%d,%d][%d,%d]"/>' % (
                    i, i % 100, i // 100, i % 100 + 50, i // 100 + 30))
        parts.append('</hierarchy>')
        raw_xml = "".join(parts)
        nodes = pc.walk(ET.fromstring(raw_xml))
        shown = [n for n in nodes if pc.compact_visible(n, False)]
        self.assertEqual(len(shown), 200)
        compact = "\n".join(pc.format_compact(n) for n in shown)
        verbose = "\n".join(
            "{} [{}] {}".format(pc.node_label(n),
                                n["class"].split(".")[-1], n["bounds"])
            for n in shown)
        self.assertLess(len(compact), len(verbose),
                        "compact should beat --verbose rendering")
        self.assertLess(len(compact), 0.5 * len(raw_xml),
                        "compact=%d raw_xml=%d" % (len(compact), len(raw_xml)))


# ------------------------------------------------- 2. occlusion-aware taps

def _occ_nodes():
    """Doc order: early (before target), target, cover1, cover2 (on top)."""
    early = _wnode(text="early", bounds="[0,0][100,100]")
    target = _wnode(text="target", bounds="[0,0][100,100]")
    cover1 = _wnode(text="cover1", bounds="[10,10][60,60]")
    cover2 = _wnode(text="cover2", bounds="[20,20][50,50]")
    return early, target, cover1, cover2


class OcclusionTests(OfflineTestCase):
    def test_is_point_covered_returns_cover(self):
        early, target, cover1, cover2 = _occ_nodes()
        hit = pc.is_point_covered([early, target, cover1, cover2],
                                  30, 30, target)
        self.assertIsNotNone(hit)
        # cover2 is later in document order -> on top
        self.assertEqual(hit["text"], "cover2")

    def test_is_point_covered_none_when_clear(self):
        early, target, cover1, cover2 = _occ_nodes()
        self.assertIsNone(pc.is_point_covered([early, target, cover1, cover2],
                                              90, 90, target))

    def test_is_point_covered_ignores_earlier_nodes(self):
        # 'early' covers (5,5) but is BEFORE target in doc order -> ignored.
        early, target, cover1, cover2 = _occ_nodes()
        self.assertIsNone(pc.is_point_covered([early, target, cover1, cover2],
                                              5, 5, target))

    def test_is_point_covered_exclude_none_checks_all(self):
        early, target, cover1, cover2 = _occ_nodes()
        hit = pc.is_point_covered([early, target, cover1, cover2],
                                  30, 30, None)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["text"], "cover2")

    def test_find_uncovered_point_alternate(self):
        node = _wnode(bounds="[0,0][100,100]")
        cover = _wnode(text="cover", bounds="[40,40][60,60]")
        nodes = [node, cover]
        pt = pc.find_uncovered_point(node, nodes)
        # center (50,50) covered -> first free sample is (25,50)
        self.assertEqual(pt, (25, 50))

    def test_find_uncovered_point_fully_covered(self):
        node = _wnode(bounds="[0,0][100,100]")
        cover = _wnode(text="cover", bounds="[0,0][100,100]")
        self.assertIsNone(pc.find_uncovered_point(node, [node, cover]))

    def test_find_uncovered_point_bad_bounds(self):
        node = _wnode(bounds="")
        self.assertIsNone(pc.find_uncovered_point(node, [node]))

    def test_parse_normalized_xy_valid(self):
        self.assertEqual(pc.parse_normalized_xy("0.5,0.8"), (0.5, 0.8))

    def test_parse_normalized_xy_boundaries(self):
        self.assertEqual(pc.parse_normalized_xy("0,0"), (0.0, 0.0))
        self.assertEqual(pc.parse_normalized_xy("1,1"), (1.0, 1.0))

    def test_parse_normalized_xy_whitespace(self):
        self.assertEqual(pc.parse_normalized_xy("  0.25 , 0.75  "),
                         (0.25, 0.75))

    def test_parse_normalized_xy_out_of_range(self):
        for bad in ("1.5,0.5", "0.5,1.01", "-0.1,0.5", "0.5,-2"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    pc.parse_normalized_xy(bad)

    def test_parse_normalized_xy_malformed(self):
        for bad in ("abc", "0.5", "0.5,0.8,0.1", "", "0.5,"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    pc.parse_normalized_xy(bad)

    def test_screen_dims_default(self):
        with mock.patch.object(pc, "_screen_wh", "1080 2400"):
            self.assertEqual(pc.screen_dims(), (1080, 2400))

    def test_screen_dims_rotated(self):
        with mock.patch.object(pc, "_screen_wh", "2400 1080"):
            self.assertEqual(pc.screen_dims(), (2400, 1080))

    def test_update_screen_from_dump_rotation(self):
        old = pc._screen_wh
        try:
            pc._update_screen_from_dump(ET.fromstring('<hierarchy rotation="1"/>'))
            self.assertEqual(pc._screen_wh, "2400 1080")
            pc._update_screen_from_dump(ET.fromstring('<hierarchy rotation="0"/>'))
            self.assertEqual(pc._screen_wh, "1080 2400")
            pc._update_screen_from_dump(ET.fromstring('<hierarchy rotation="3"/>'))
            self.assertEqual(pc._screen_wh, "2400 1080")
        finally:
            pc._screen_wh = old


# ------------------------------------------------------------- 3. doctor

class DoctorTests(OfflineTestCase):
    """All 7 doctor checks with mocked dependencies -- never touches adb."""

    CHECK_NAMES = ["adb binary", "adb server", "tunnel target", "u2 daemon",
                   "device state", "dump pipeline", "latency"]
    # fail key -> check name it must break
    FAIL_MAP = {"binary": "adb binary", "server": "adb server",
                "tunnel": "tunnel target", "u2": "u2 daemon",
                "state": "device state", "dump": "dump pipeline",
                "latency": "latency"}

    def _doctor_mocks(self, fail=None, raise_dep=None):
        sub = self.allow("subprocess")
        adb_bin, target = pc.ADB_BIN, pc.TARGET

        def run(cmd, **kw):
            cmd = list(cmd)
            if raise_dep == "subprocess":
                raise RuntimeError("boom")
            if cmd[:2] == [adb_bin, "version"]:
                if fail == "binary":
                    return SimpleNamespace(returncode=1, stdout="",
                                           stderr="oops")
                return SimpleNamespace(
                    returncode=0,
                    stdout="Android Debug Bridge version 1.0.41\n", stderr="")
            if cmd[:2] == [adb_bin, "devices"]:
                if fail == "server":
                    return SimpleNamespace(returncode=1, stdout="",
                                           stderr="oops")
                return SimpleNamespace(returncode=0,
                                       stdout="List of devices attached\n",
                                       stderr="")
            if cmd[:3] == [adb_bin, "connect", target]:
                if fail == "tunnel":
                    return SimpleNamespace(
                        returncode=0,
                        stdout="failed to connect to 'x': refused\n", stderr="")
                return SimpleNamespace(
                    returncode=0,
                    stdout="connected to {}\n".format(target), stderr="")
            raise AssertionError("unexpected subprocess call: %r" % (cmd,))

        sub.run.side_effect = run

        real_exists = os.path.exists
        if raise_dep == "exists":
            ex = mock.patch.object(pc.os.path, "exists",
                                   side_effect=RuntimeError("boom"))
        else:
            def _exists(pth):
                if pth == pc.U2_SOCK:
                    return fail != "u2"
                return real_exists(pth)
            ex = mock.patch.object(pc.os.path, "exists", side_effect=_exists)
        ex.start()
        self.addCleanup(ex.stop)

        def u2(cmd, arg="", timeout=30):
            if raise_dep == "u2sock":
                raise RuntimeError("boom")
            if cmd == "health":
                return "ok"
            if cmd == "dump":
                if fail == "latency":
                    return None
                return '<hierarchy rotation="0"></hierarchy>'
            raise AssertionError("unexpected u2sock cmd: %r" % cmd)

        self.allow("u2sock", side_effect=u2)

        def adb_fn(*a, **kw):
            if raise_dep == "adb":
                raise RuntimeError("boom")
            return SimpleNamespace(
                returncode=0,
                stdout="offline\n" if fail == "state" else "device\n",
                stderr="")

        self.allow("adb", side_effect=adb_fn)

        def fd():
            if raise_dep == "fast_dump":
                raise RuntimeError("boom")
            if fail == "dump":
                return None
            return ET.fromstring(
                '<hierarchy rotation="0">'
                '<node text="x" class="android.widget.TextView" '
                'bounds="[0,0][10,10]"/></hierarchy>')

        self.allow("fast_dump", side_effect=fd)

    def _run_doctor(self, argv, fail=None, raise_dep=None):
        # Parse before mocking: raise_dep="exists" breaks os.path.exists,
        # which argparse's gettext lookup calls on some Pythons.
        args = self.parse(argv)
        self._doctor_mocks(fail=fail, raise_dep=raise_dep)
        with self.cap() as (out, err):
            rc = pc.cmd_doctor(args)
        return rc, out.getvalue(), err.getvalue()

    def test_doctor_all_pass_exit_0(self):
        rc, out, err = self._run_doctor(["doctor"])
        self.assertEqual(rc, 0)
        self.assertIn("8/8 checks passed", out)

    def test_doctor_all_pass_json(self):
        rc, out, err = self._run_doctor(["doctor", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["checks"]), 8)
        self.assertTrue(all(c["ok"] for c in data["checks"]))
        self.assertIsInstance(data["latency_ms"], int)

    def test_doctor_each_failing_check(self):
        for fail, name in self.FAIL_MAP.items():
            with self.subTest(fail=fail):
                rc, out, err = self._run_doctor(["doctor", "--json"],
                                               fail=fail)
                self.assertEqual(rc, 1, "fail=%s" % fail)
                data = json.loads(out)
                self.assertFalse(data["ok"])
                by_name = {c["name"]: c for c in data["checks"]}
                self.assertFalse(by_name[name]["ok"],
                                 "check %r should be failed" % name)
                others = [c for c in data["checks"] if c["name"] != name]
                self.assertTrue(all(c["ok"] for c in others),
                                "only %r should fail, got %r"
                                % (name, [c["name"] for c in others
                                          if not c["ok"]]))

    def test_doctor_human_marks_failed_check(self):
        rc, out, err = self._run_doctor(["doctor"], fail="tunnel")
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL] tunnel target", out)

    def test_doctor_exception_safe_subprocess(self):
        rc, out, err = self._run_doctor(["doctor"], raise_dep="subprocess")
        self.assertEqual(rc, 1)  # must not raise
        self.assertIn("RuntimeError", out)

    def test_doctor_exception_safe_u2sock(self):
        rc, out, err = self._run_doctor(["doctor"], raise_dep="u2sock")
        self.assertEqual(rc, 1)
        self.assertIn("RuntimeError", out)

    def test_doctor_exception_safe_adb(self):
        rc, out, err = self._run_doctor(["doctor"], raise_dep="adb")
        self.assertEqual(rc, 1)
        self.assertIn("RuntimeError", out)

    def test_doctor_exception_safe_fast_dump(self):
        rc, out, err = self._run_doctor(["doctor"], raise_dep="fast_dump")
        self.assertEqual(rc, 1)
        self.assertIn("RuntimeError", out)

    def test_doctor_exception_safe_exists(self):
        rc, out, err = self._run_doctor(["doctor"], raise_dep="exists")
        self.assertEqual(rc, 1)
        self.assertIn("RuntimeError", out)


# ---------------------------------------------------------- 4. --json output

class JsonOutputTests(OfflineTestCase):
    def _tap_mocks(self, xml):
        self.allow("wake_async")
        self.allow("u2sock", return_value=None)
        self.allow("ui_dump", return_value=ET.fromstring(xml))
        self.allow("tap_center")
        self.allow("u2_invalidate")

    def test_json_flag_position_equivalent(self):
        a1 = self.parse(["--json", "dump"])
        a2 = self.parse(["dump", "--json"])
        a3 = self.parse(["dump"])
        self.assertTrue(pc.as_json(a1))
        self.assertTrue(pc.as_json(a2))
        self.assertFalse(pc.as_json(a3))

    def test_json_dump(self):
        self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        args = self.parse(["dump", "--json"])
        with self.cap() as (out, err):
            rc = pc.cmd_dump(args)
        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue())
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["nodes"]), 3)  # Hello, OK, Search field
        self.assertTrue(all("class" in n for n in data["nodes"]))
        self.assertEqual(err.getvalue(), "")

    def test_json_tap_ok(self):
        self._tap_mocks(TAP_XML)
        args = self.parse(["tap", "Not now", "--json"])
        with self.cap() as (out, err):
            rc = pc.cmd_tap(args)
        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue())
        self.assertTrue(data["ok"])
        self.assertEqual(data["tapped"]["text"], "Not now")
        self.assertEqual((data["tapped"]["x"], data["tapped"]["y"]),
                         (200, 300))

    def test_json_tap_no_match_errors_to_stderr(self):
        self._tap_mocks(TAP_XML)
        args = self.parse(["tap", "Nope", "--json"])
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_tap(args)
        self.assertEqual(rc, 1)
        data = json.loads(out.getvalue())
        self.assertFalse(data["ok"])
        self.assertIn("error", data)
        self.assertIn('no match for "Nope"', err.getvalue())
        pc.ui_dump.assert_any_call(fresh=True)  # the extra read skips the helper's cache

    def test_tap_reads_once_more_when_the_label_is_still_drawing(self):
        self._tap_mocks(TAP_XML)
        blank = ET.fromstring(TAP_XML.replace("Not now", ""))
        ud = self.allow("ui_dump", side_effect=[blank, ET.fromstring(TAP_XML)])
        tc = self.allow("tap_center")
        args = self.parse(["tap", "Not now", "--json", "--no-evidence"])
        with mock.patch.object(pc.time, "sleep") as slept, self.cap() as (out, err):
            rc = pc.cmd_tap(args)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["tapped"]["text"], "Not now")
        tc.assert_called_once_with(200, 300)
        slept.assert_any_call(0.7)  # the pause before the second read
        ud.assert_called_with(fresh=True)  # the second read bypasses the 2s cache

    def test_plan_tap_refuses_a_row_cut_off_at_the_edge(self):
        plan = pc.plan_tap(pc.walk(ET.fromstring(CLIPPED_XML)), 1080, 2400, text="Box Score")
        self.assertEqual((plan["action"], plan["edge"]), ("clipped", "top"))
        # a row under another row (a sticky bar's link earlier in the tree)
        plan = pc.plan_tap(pc.walk(ET.fromstring(COVERED_XML)), 1080, 2400, text="Box Score")
        self.assertEqual((plan["action"], plan["edge"], plan["cover"]["text"]),
                         ("covered", "top", "Standings"))
        native = COVERED_XML.replace("android.webkit.WebView", "android.widget.FrameLayout")
        plan = pc.plan_tap(pc.walk(ET.fromstring(native)), 1080, 2400, text="Box Score")
        self.assertEqual(plan["action"], "tap")  # later is on top on a native screen
        whole = pc.plan_tap(pc.walk(ET.fromstring(WHOLE_XML)), 1080, 2400, text="Box Score")
        self.assertEqual((whole["action"], whole["xy"]), ("tap", (796, 491)))
        # one control drawn twice is one row, not an ambiguity
        pair = pc.plan_tap(pc.walk(ET.fromstring(PAIR_XML)), 1080, 2400, text="Open")
        self.assertEqual((pair["action"], pair["xy"]), ("tap", (540, 1050)))

    def test_tap_nudges_a_row_cut_off_at_the_edge_into_view(self):
        self._tap_mocks(CLIPPED_XML)
        swipe = self.allow("_scrcpy_swipe", return_value=True)
        self.allow("read_after_root", return_value=(ET.fromstring(WHOLE_XML), ""))
        tc = self.allow("tap_center")
        args = self.parse(["tap", "Box Score", "--json", "--no-evidence"])
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_tap(args)
        self.assertEqual(rc, 0, "stdout=%r stderr=%r" % (out.getvalue(), err.getvalue()))
        # content down (the top edge cut the row off), then the whole row
        swipe.assert_called_once_with("up", length=pc.nudge_px(2400))
        tc.assert_called_once_with(796, 491)

    def test_tap_nudges_a_covered_row_from_under_the_bar(self):
        self._tap_mocks(COVERED_XML)
        swipe = self.allow("_scrcpy_swipe", return_value=True)
        self.allow("read_after_root", return_value=(ET.fromstring(WHOLE_XML), ""))
        tc = self.allow("tap_center")
        args = self.parse(["tap", "Box Score", "--json", "--no-evidence"])
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_tap(args)
        self.assertEqual(rc, 0, "stdout=%r stderr=%r" % (out.getvalue(), err.getvalue()))
        swipe.assert_called_once_with("up", length=pc.nudge_px(2400))
        tc.assert_called_once_with(796, 491)

    def test_tap_gives_up_on_a_row_that_stays_covered(self):
        self._tap_mocks(COVERED_XML)
        swipe = self.allow("_scrcpy_swipe", return_value=True)
        self.allow("read_after_root", return_value=(ET.fromstring(COVERED_XML), ""))
        tc = self.allow("tap_center")
        args = self.parse(["tap", "Box Score", "--json", "--no-evidence"])
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_tap(args)
        self.assertEqual(rc, 1)
        self.assertEqual(swipe.call_count, pc.NUDGE_ROUNDS)
        tc.assert_not_called()
        self.assertIn('"Box Score" is under "Standings"', err.getvalue())

    def test_tap_says_when_a_cut_off_row_cannot_be_nudged(self):
        self._tap_mocks(CLIPPED_XML)
        self.allow("_scrcpy_swipe", return_value=False)  # no scrcpy helper
        tc = self.allow("tap_center")
        args = self.parse(["tap", "Box Score", "--json", "--no-evidence"])
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_tap(args)
        self.assertEqual(rc, 1)
        tc.assert_not_called()
        self.assertIn("cut off at the top edge", err.getvalue())
        self.assertIn("scroll up a little", err.getvalue())

    def test_json_tap_xy_refused_by_real_overlay(self):
        self._tap_mocks(OVERLAY_XML)
        args = self.parse(["tap", "--xy", "0.5,0.5", "--json"])
        with self.cap() as (out, err):
            rc = pc.cmd_tap(args)
        self.assertEqual(rc, 1)
        data = json.loads(out.getvalue())
        self.assertFalse(data["ok"])
        self.assertIn("tap refused", err.getvalue())
        self.assertIn("tap refused", data["error"])

    def test_tap_xy_no_false_positive_from_containers(self):
        # BUG (implementation, reported not fixed): with no overlay at all,
        # the --xy occlusion check still refuses because the full-screen
        # root container covers the point (exclude_node=None includes
        # ancestors). Correct behavior: tap proceeds.
        self._tap_mocks(TAP_XML)
        args = self.parse(["tap", "--xy", "0.5,0.5", "--json"])
        with self.cap() as (out, err):
            rc = pc.cmd_tap(args)
        self.assertEqual(rc, 0, "stdout=%r stderr=%r"
                         % (out.getvalue(), err.getvalue()))
        data = json.loads(out.getvalue())
        self.assertTrue(data["ok"])

    def test_tap_xy_toolbar_container_not_blocking(self):
        # Live false positive (2026-09-30): the Amazon search toolbar
        # container covered the tap point and the --xy tap was refused.
        # An edge-to-edge layout container is the tap's natural landing
        # spot, not an obstruction: tap proceeds.
        self._tap_mocks(TOOLBAR_XML)
        args = self.parse(["tap", "--xy", "0.5,0.04", "--json"])
        with self.cap() as (out, err):
            rc = pc.cmd_tap(args)
        self.assertEqual(rc, 0, "stdout=%r stderr=%r"
                         % (out.getvalue(), err.getvalue()))
        data = json.loads(out.getvalue())
        self.assertTrue(data["ok"])

    def test_looks_like_overlay(self):
        w, h = 1080, 2400
        dialog = {"class": "android.widget.FrameLayout",
                  "bounds": "[200,900][880,1500]"}    # floating box
        icon = {"class": "android.view.ViewGroup",
                "bounds": "[914,131][1009,226]"}      # a 95px button
        toolbar = {"class": "android.view.ViewGroup",
                   "bounds": "[0,0][1080,200]"}       # edge-to-edge
        sheet_cls = {"class": "android.widget.BottomSheet",
                     "bounds": "[0,1800][1080,2400]"}  # class says sheet
        fullscreen = {"class": "android.widget.FrameLayout",
                      "bounds": "[0,0][1080,2400]"}
        self.assertTrue(pc._looks_like_overlay(dialog, w, h))
        self.assertFalse(pc._looks_like_overlay(icon, w, h))
        self.assertFalse(pc._looks_like_overlay(toolbar, w, h))
        self.assertTrue(pc._looks_like_overlay(sheet_cls, w, h))
        self.assertFalse(pc._looks_like_overlay(fullscreen, w, h))
        # Live false positive (2026-10-02, Settings home): a list row's inner
        # RelativeLayout, inside the clickable row, refused a tap on the row.
        row = ET.fromstring('<node clickable="true"/>')
        inner = {"class": "android.widget.RelativeLayout",
                 "bounds": "[189,1039][1038,1234]", "parents": [row]}
        self.assertFalse(pc._looks_like_overlay(inner, w, h))

    def test_json_wait_timeout_errors_to_stderr(self):
        self.allow("u2sock", return_value=pc.U2_NOT_FOUND)
        args = self.parse(["wait", "Never", "--json", "--no-evidence"])
        with self.cap() as (out, err):
            rc = pc.cmd_wait(args)
        self.assertEqual(rc, 1)
        data = json.loads(out.getvalue())
        self.assertFalse(data["ok"])
        self.assertIn('timeout waiting for "Never"', err.getvalue())

    def test_json_state(self):
        # The app in front comes from the screen read itself: no adb call.
        self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        args = self.parse(["state", "--json"])
        with self.cap() as (out, err):
            rc = pc.cmd_state(args)
        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue())
        self.assertTrue(data["ok"])
        self.assertEqual(data["app"], "com.example")
        self.assertIn("Hello", data["texts"])

    def test_state_prints_screen_rows(self):
        self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        args = self.parse(["state"])
        with self.cap() as (out, err):
            rc = pc.cmd_state(args)
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("screen: com.example", text)
        self.assertIn("Hello (300,250)", text)
        self.assertIn("OK (click) [Button]", text)


# ------------------------------------------------------------ 5. unicode input

class UnicodeTests(OfflineTestCase):
    def test_needs_unicode_route_ascii(self):
        self.assertFalse(pc.needs_unicode_route("hello"))

    def test_needs_unicode_route_emoji(self):
        self.assertTrue(pc.needs_unicode_route("héllo 🎉"))

    def test_needs_unicode_route_cjk(self):
        self.assertTrue(pc.needs_unicode_route("你好"))

    def test_type_via_adbkeyboard_call_sequence(self):
        calls = []
        self.allow("get_current_ime", return_value="com.example/.Ime")
        m_set = self.allow("set_ime")
        m_bc = self.allow("broadcast_adbkeyboard_text")
        m_tap = self.allow("tap_center")
        m_adb = self.allow("adb_or_ensure")
        with self.cap() as (out, err):
            rc = pc.type_via_adbkeyboard("hi", field_xy=(10, 20),
                                         clear=True, clear_keys=3)
        self.assertEqual(rc, 0)
        # get IME -> set AdbIME -> tap field -> clear -> broadcast -> restore
        m_set.assert_any_call(pc.ADBKEYBOARD_IME)
        m_tap.assert_called_once_with(10, 20)
        self.assertEqual(m_adb.call_count, 3)  # clear_keys=3 DEL keyevents
        m_bc.assert_called_once_with("hi")
        self.assertEqual(m_set.call_args_list[0],
                         mock.call(pc.ADBKEYBOARD_IME))
        self.assertEqual(m_set.call_args_list[-1],
                         mock.call("com.example/.Ime"))
        self.assertIn("adbkeyboard", out.getvalue())

    def test_broadcast_uses_es_msg(self):
        # Real broadcast_adbkeyboard_text, mocked adb: verifies the exact
        # broadcast args (--es msg) with unicode intact. The message is
        # single-quoted for the device shell: `adb shell` joins args with
        # spaces and re-parses them on-device, so an unquoted message with
        # spaces would be split and the text silently lost.
        self.allow("get_current_ime", return_value="com.example/.Ime")
        self.allow("set_ime")
        m_adb = self.allow("adb")
        m_adb.return_value = SimpleNamespace(returncode=0, stdout="",
                                             stderr="")
        pc.broadcast_adbkeyboard_text("héllo 🎉")
        self.assertEqual(
            m_adb.call_args[0],
            ("shell", "am", "broadcast", "-a", "ADB_INPUT_TEXT",
             "--es", "msg", "'héllo 🎉'"))

    def test_broadcast_quotes_single_quotes(self):
        # Embedded single quotes are escaped so the device shell still
        # sees one argument.
        m_adb = self.allow("adb")
        m_adb.return_value = SimpleNamespace(returncode=0, stdout="",
                                             stderr="")
        pc.broadcast_adbkeyboard_text("it's")
        self.assertEqual(
            m_adb.call_args[0][-1], "'it'\\''s'")

    def test_type_via_adbkeyboard_restores_ime_on_exception(self):
        self.allow("get_current_ime", return_value="com.example/.Ime")
        m_set = self.allow("set_ime")
        self.allow("broadcast_adbkeyboard_text",
                   side_effect=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            pc.type_via_adbkeyboard("hi")
        # finally block restores even though broadcast raised
        self.assertEqual(m_set.call_args_list,
                         [mock.call(pc.ADBKEYBOARD_IME),
                          mock.call("com.example/.Ime")])

    def test_cmd_type_unicode_flag_forces_adbkeyboard(self):
        m_uni = self.allow("type_via_adbkeyboard", return_value=0)
        self.allow("u2sock", return_value="100")
        self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        args = SimpleNamespace(text="hello", field=None, clear=False,
                               clear_keys=20, unicode=True, ascii=False,
                               slow=False, quiet=True)
        with self.cap():
            rc = pc.cmd_type(args)
        self.assertEqual(rc, 0)
        m_uni.assert_called_once()
        self.assertEqual(m_uni.call_args[0][0], "hello")

    def test_cmd_type_ascii_flag_forces_legacy(self):
        m_uni = self.allow("type_via_adbkeyboard", return_value=0)
        m_adb = self.allow("adb_or_ensure")
        m_adb.return_value = SimpleNamespace(returncode=0, stdout="",
                                             stderr="")
        self.allow("u2sock", return_value=None)  # no UI helper: key events
        self.allow("scrcpy_send", side_effect=RuntimeError("no scrcpy"))
        self.allow("u2_invalidate")
        self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        args = SimpleNamespace(text="hé", field=None, clear=False,
                               clear_keys=20, unicode=False, ascii=True,
                               slow=False, quiet=True)
        with mock.patch.object(pc.time, "sleep", lambda s: None):
            with self.cap():
                rc = pc.cmd_type(args)
        self.assertEqual(rc, 0)
        m_uni.assert_not_called()
        sent = [c[0] for c in m_adb.call_args_list]
        # one adb call types the whole string
        self.assertIn(("shell", "input", "text", "h\\é"), sent)

    def test_cmd_type_uses_helper_set_text_without_a_read(self):
        """The UI helper finds the focused field itself: no screen read
        and no health call before typing."""
        self.allow("u2_invalidate")
        dump = self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append(cmd)
            return SAMPLE_XML if cmd == "act" else None
        self.allow("u2sock", side_effect=u2)
        args = SimpleNamespace(text="hello", field=None, clear=False,
                               clear_keys=20, unicode=False, ascii=False,
                               slow=False, quiet=False)
        with self.cap() as (out, err):
            rc = pc.cmd_type(args)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["act"])
        dump.assert_not_called()  # no read before, and the read after rides on the act
        self.assertIn("typed 5 chars", out.getvalue())
        self.assertIn("screen: com.example", out.getvalue())

    def test_cmd_type_scrcpy_keys_when_no_field(self):
        """No editable field for the helper: key events over scrcpy."""
        self.allow("u2_invalidate")
        self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        self.allow("u2sock", return_value=None)
        sc = self.allow("scrcpy_send", return_value=True)
        args = SimpleNamespace(text="ab\ncd", field=None, clear=False,
                               clear_keys=20, unicode=False, ascii=False,
                               slow=False, quiet=True)
        with self.cap() as (out, err):
            rc = pc.cmd_type(args)
        self.assertEqual(rc, 0)
        sent = [c[0][0] for c in sc.call_args_list]
        self.assertEqual(sent, ["text ab", "key 66", "text cd"])
        self.assertIn("key events", out.getvalue())


# ----------------------------------------------- 6. existing functionality

class RegressionTests(OfflineTestCase):
    # walk()
    def test_walk_parses_nested_xml(self):
        nodes = pc.walk(ET.fromstring(SAMPLE_XML))
        self.assertEqual(len(nodes), 5)  # hierarchy + frame + 3 children
        texts = [n["text"] for n in nodes]
        self.assertIn("Hello", texts)
        self.assertIn("OK", texts)

    def test_walk_centers_and_parents(self):
        nodes = pc.walk(ET.fromstring(SAMPLE_XML))
        ok = next(n for n in nodes if n["text"] == "OK")
        self.assertEqual(ok["center"], (250, 450))  # [100,400][400,500]
        self.assertEqual(len(ok["parents"]), 2)
        self.assertEqual(ok["parents"][-1].attrib["class"],
                         "android.widget.FrameLayout")
        self.assertTrue(ok["clickable"])
        edit = next(n for n in nodes if n["desc"] == "Search")
        self.assertTrue(edit["focused"])

    # find_nodes()
    def test_find_nodes_exact(self):
        nodes = pc.walk(ET.fromstring(SAMPLE_XML))
        hits = pc.find_nodes(nodes, "OK", exact=True)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["text"], "OK")

    def test_find_nodes_exact_no_substring(self):
        nodes = pc.walk(ET.fromstring(SAMPLE_XML))
        self.assertEqual(pc.find_nodes(nodes, "O", exact=True), [])

    def test_find_nodes_fuzzy(self):
        nodes = pc.walk(ET.fromstring(SAMPLE_XML))
        hits = pc.find_nodes(nodes, "ok", exact=False)
        self.assertEqual([n["text"] for n in hits], ["OK"])

    def test_find_nodes_matches_desc(self):
        nodes = pc.walk(ET.fromstring(SAMPLE_XML))
        hits = pc.find_nodes(nodes, "Search", exact=True)
        self.assertEqual(len(hits), 1)
        self.assertIn("EditText", hits[0]["class"])

    # clickable_target()
    def test_clickable_target_own_center(self):
        n = _wnode(clickable=True, center=(10, 20))
        self.assertEqual(pc.clickable_target(n), (10, 20))

    def test_clickable_target_clickable_ancestor(self):
        parent = ET.fromstring(
            '<node clickable="true" bounds="[0,0][100,100]"/>')
        n = _wnode(clickable=False, center=(None, None))
        n["parents"] = [parent]
        self.assertEqual(pc.clickable_target(n), (50, 50))

    def test_clickable_target_falls_back_to_own(self):
        parent = ET.fromstring(
            '<node clickable="false" bounds="[0,0][100,100]"/>')
        n = _wnode(clickable=False, center=(7, 8))
        n["parents"] = [parent]
        self.assertEqual(pc.clickable_target(n), (7, 8))

    # extract_code()
    def test_extract_code_facebook_style(self):
        self.assertEqual(
            pc.extract_code("42327079 is your Facebook code", ""), "42327079")

    def test_extract_code_enter_this_code(self):
        self.assertEqual(
            pc.extract_code("", "Enter this code: 7392"), "7392")

    def test_extract_code_verification_code(self):
        self.assertEqual(
            pc.extract_code("", "Your verification code is 567812"), "567812")

    def test_extract_code_generic_code(self):
        self.assertEqual(
            pc.extract_code("", "Use code 4242 to verify"), "4242")

    def test_extract_code_fallback_digits(self):
        self.assertEqual(
            pc.extract_code("Welcome aboard",
                            "Your reference number is 987654"), "987654")

    def test_extract_code_subject_first(self):
        self.assertEqual(
            pc.extract_code("111111 is your code", "222222 is your code"),
            "111111")

    def test_extract_code_none(self):
        self.assertIsNone(pc.extract_code("hello", "nothing here"))

    # esc_char()
    def test_esc_char_space(self):
        self.assertEqual(pc.esc_char(" "), "%s")

    def test_esc_char_alnum_passthrough(self):
        for c in "aZ09":
            self.assertEqual(pc.esc_char(c), c)

    def test_esc_char_specials_escaped(self):
        self.assertEqual(pc.esc_char("!"), "\\!")
        self.assertEqual(pc.esc_char("$"), "\\$")
        self.assertEqual(pc.esc_char("&"), "\\&")
        self.assertEqual(pc.esc_char("-"), "\\-")

    # load_config()
    def _write_config(self, d, content):
        p = os.path.join(d, "config.env")
        with open(p, "w") as f:
            f.write(content)
        return p

    def test_load_config_parses_values(self):
        with tempfile.TemporaryDirectory() as d:
            self._write_config(d, 'FOO="bar"\nBAZ=qux\nEQ="a=b"\n')
            with mock.patch.object(pc, "ROOT", d):
                cfg = pc.load_config()
        self.assertEqual(cfg, {"FOO": "bar", "BAZ": "qux", "EQ": "a=b"})

    def test_load_config_ignores_comments_and_blanks(self):
        with tempfile.TemporaryDirectory() as d:
            self._write_config(d, "# comment\n\nFOO=1\n   \n#X=y\n")
            with mock.patch.object(pc, "ROOT", d):
                cfg = pc.load_config()
        self.assertEqual(cfg, {"FOO": "1"})

    def test_load_config_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(pc, "ROOT", d):
                self.assertEqual(pc.load_config(), {})

    # cmd_do()
    def test_cmd_do_runs_steps(self):
        args = SimpleNamespace(flow="sleep 0; sleep 0")
        with self.cap():
            rc = pc.cmd_do(args)
        self.assertEqual(rc, 0)

    def test_cmd_do_stops_on_failure(self):
        calls = []

        def fake_parse(argv):
            ns = mock.Mock()
            step = " ".join(argv)
            ns.fn = lambda n: calls.append(step) or (
                1 if "fail" in step else 0)
            return ns

        stub = mock.Mock()
        stub.parse_args = fake_parse
        with mock.patch.object(pc, "build_parser", return_value=stub):
            with self.cap():
                rc = pc.cmd_do(SimpleNamespace(
                    flow="step one; fail step; step three"))
        self.assertEqual(rc, 1)
        self.assertEqual(calls, ["step one", "fail step"])

    def test_cmd_do_bad_step(self):
        args = SimpleNamespace(flow="sleep 0; nosuchcommand")
        with self.cap() as (out, err):
            rc = pc.cmd_do(args)
        self.assertEqual(rc, 1)
        self.assertIn("bad step 2", err.getvalue())

    # cmd_recipe()
    def test_cmd_recipe_missing(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(pc, "ROOT", d):
                with self.cap() as (out, err):
                    rc = pc.cmd_recipe(SimpleNamespace(name="nope"))
        self.assertEqual(rc, 1)
        self.assertIn("no recipe", err.getvalue())

    def test_cmd_recipe_skips_comments_and_blanks(self):
        with tempfile.TemporaryDirectory() as d:
            rdir = os.path.join(d, "recipes")
            os.makedirs(rdir)
            with open(os.path.join(rdir, "r.burner"), "w") as f:
                f.write("# a comment\n\nsleep 0\n# another\nsleep 0\n")
            with mock.patch.object(pc, "ROOT", d):
                with mock.patch.object(pc, "cmd_do", return_value=0) as m:
                    rc = pc.cmd_recipe(SimpleNamespace(name="r"))
        self.assertEqual(rc, 0)
        self.assertEqual(m.call_args[0][0].flow, "sleep 0; sleep 0")

    def test_cmd_recipe_failing_step(self):
        with tempfile.TemporaryDirectory() as d:
            rdir = os.path.join(d, "recipes")
            os.makedirs(rdir)
            with open(os.path.join(rdir, "r.burner"), "w") as f:
                f.write("sleep 0\nnosuchcmd\n")
            with mock.patch.object(pc, "ROOT", d):
                with self.cap():
                    rc = pc.cmd_recipe(SimpleNamespace(name="r"))
        self.assertEqual(rc, 1)


    # gmail_cli()
    def test_gmail_cli_default(self):
        with mock.patch.dict(pc.CFG, {}, clear=True):
            self.assertEqual(pc.gmail_cli(), ["hatch_gws_cli"])

    def test_gmail_cli_override(self):
        with mock.patch.dict(pc.CFG, {"GMAIL_CLI": "mytool --flag x"}):
            self.assertEqual(pc.gmail_cli(), ["mytool", "--flag", "x"])


# --------------------------------------- 7. docs/implementation consistency

class DocsTests(OfflineTestCase):
    def _readme_commands(self):
        with open(os.path.join(ROOT, "SKILL.md"), encoding="utf-8") as f:
            text = f.read()
        # the "## Commands" section
        section = text.split("\n## Commands\n", 1)[1].split("\n## ", 1)[0]
        cmds = {}
        for line in section.splitlines():
            s = line.strip()
            if not s.startswith("burner "):
                continue
            parts = s.split(None, 2)
            if len(parts) < 2:
                continue
            cmd, rest = parts[1], parts[2] if len(parts) > 2 else ""
            # quoted example text (e.g. pc do '... --timeout 30 ...') holds
            # other commands' flags -- strip quotes first.
            rest = re.sub(r"'[^']*'", "", rest)
            rest = re.sub(r'"[^"]*"', "", rest)
            flags = set(re.findall(r"--([A-Za-z0-9-]+)", rest))
            cmds.setdefault(cmd, set()).update(flags)
        return cmds

    def test_readme_flags_exist_in_argparse(self):
        cmds = self._readme_commands()
        self.assertTrue(cmds, "no commands parsed from SKILL.md")
        ap = pc.build_parser()
        sub = next(a for a in ap._actions
                   if isinstance(a, argparse._SubParsersAction))
        top_opts = set()
        for a in ap._actions:
            top_opts.update(o.lstrip("-") for o in a.option_strings)
        for cmd, flags in sorted(cmds.items()):
            with self.subTest(cmd=cmd):
                self.assertIn(cmd, sub.choices,
                              "SKILL.md documents unknown command")
                parser = sub.choices[cmd]
                opts = set(top_opts)
                for a in parser._actions:
                    opts.update(o.lstrip("-") for o in a.option_strings)
                missing = flags - opts
                self.assertFalse(
                    missing,
                    "SKILL.md flags missing from argparse for %r: %s"
                    % (cmd, sorted(missing)))

    def test_every_command_is_documented(self):
        documented = set(self._readme_commands())
        ap = pc.build_parser()
        sub = next(a for a in ap._actions
                   if isinstance(a, argparse._SubParsersAction))
        missing = set(sub.choices) - documented
        self.assertFalse(missing, "commands missing from SKILL.md's "
                         "## Commands: %s" % sorted(missing))

    def test_no_muse_phone_string_in_tree(self):
        bad = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            if ".git" in dirnames:
                dirnames.remove(".git")
            if "tests" in dirnames:
                # this suite itself mentions the string; skip it
                dirnames.remove("tests")
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                if "muse-phone" in fn:
                    bad.append(p)
                    continue
                try:
                    with open(p, "r", encoding="utf-8",
                              errors="strict") as f:
                        content = f.read()
                except (UnicodeDecodeError, OSError):
                    continue
                if "muse-phone" in content:
                    bad.append(p)
        self.assertEqual(bad, [])

    def test_config_example_has_placeholders(self):
        with open(os.path.join(ROOT, "config.env.example")) as f:
            text = f.read()
        assignments = {}
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                assignments[k.strip()] = v.strip().strip('"')
        self.assertTrue(assignments)
        ipre = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
        for k, v in assignments.items():
            self.assertFalse(ipre.search(v),
                             "real-looking IP in %s=%r" % (k, v))
        self.assertIn("YOUR_PHONE_TAILSCALE_IP", assignments.values())
        # ADB_PORT is pinned to 5555 by adb-auto-enable (installed during
        # pairing); it is intentionally not a placeholder anymore.
        self.assertEqual(assignments.get("ADB_PORT"), "5555")


# --------------------------------- 8. offline safety + sanity checks

class OfflineSafetyTests(OfflineTestCase):
    def test_guard_blocks_adb(self):
        with self.assertRaises(OfflineGuardTripped):
            pc.adb("get-state")

    def test_guard_blocks_u2sock(self):
        with self.assertRaises(OfflineGuardTripped):
            pc.u2sock("dump")

    def test_guard_is_not_swallowed_by_read_after(self):
        with self.assertRaises(OfflineGuardTripped):
            pc.read_after()

    @unittest.skipIf(os.name == "nt", "Unix sockets: burner runs on Linux and macOS")
    def test_guard_blocks_socket(self):
        import socket as _socket
        with self.assertRaises(OfflineGuardTripped):
            _socket.socket(_socket.AF_UNIX)

    def test_bin_burner_compiles(self):
        r = subprocess.run(
            [sys.executable, "-m", "py_compile", os.path.join(ROOT, "bin", "burner")],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    @unittest.skipIf(os.name == "nt", "Windows can't exec bin/burner by its shebang")
    def test_bin_burner_help_offline(self):
        env = dict(os.environ, BURNER_WORKSPACE=ROOT)
        r = subprocess.run(
            [os.path.join(ROOT, "bin", "burner"), "--help"],
            capture_output=True, text=True, timeout=30, env=env, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("phone control CLI", r.stdout)


# --------------------------------- 11. ambiguous taps, snap handles, settle

AMBI_XML = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" clickable="false" enabled="true" focused="false" checked="false">
    <node text="OK" class="android.widget.Button" bounds="[100,400][400,500]" clickable="true" enabled="true" focused="false" checked="false"/>
    <node text="Cancel" class="android.widget.Button" bounds="[500,400][800,500]" clickable="true" enabled="true" focused="false" checked="false"/>
    <node text="OK" class="android.widget.Button" bounds="[100,600][400,700]" clickable="true" enabled="true" focused="false" checked="false"/>
  </node>
</hierarchy>"""

FUZZY_AMBI_XML = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" clickable="false" enabled="true" focused="false" checked="false">
    <node text="Okay" class="android.widget.Button" bounds="[100,400][400,500]" clickable="true" enabled="true" focused="false" checked="false"/>
    <node text="OK fine" class="android.widget.Button" bounds="[100,600][400,700]" clickable="true" enabled="true" focused="false" checked="false"/>
  </node>
</hierarchy>"""

SETTLE_A_XML = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" clickable="false" enabled="true" focused="false" checked="false">
    <node text="Loading" class="android.widget.TextView" bounds="[100,200][500,300]" clickable="false" enabled="true" focused="false" checked="false"/>
  </node>
</hierarchy>"""

SETTLE_B_XML = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" clickable="false" enabled="true" focused="false" checked="false">
    <node text="Results" class="android.widget.TextView" bounds="[100,200][500,300]" clickable="false" enabled="true" focused="false" checked="false"/>
    <node text="Buy" class="android.widget.Button" bounds="[100,400][400,500]" clickable="true" enabled="true" focused="false" checked="false"/>
  </node>
</hierarchy>"""


def _twelve_line_xml():
    kids = "".join(
        '<node text="Row%d" class="android.widget.TextView" '
        'bounds="[%d,200][%d,300]" clickable="false" enabled="true" '
        'focused="false" checked="false"/>' % (i, 100 + i * 10, 500 + i * 10)
        for i in range(12))
    return ('<hierarchy rotation="0"><node text="" '
            'class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" '
            'clickable="false" enabled="true" focused="false" '
            'checked="false">' + kids + "</node></hierarchy>")


class AmbiguousTapTests(OfflineTestCase):
    def _tap(self, argv, xml=AMBI_XML):
        self.allow("wake_async")
        self.allow("ui_dump", return_value=ET.fromstring(xml))
        tc = self.allow("tap_center")

        def u2(cmd, arg="", timeout=30):
            if cmd == "act":  # an old helper: the tap is decided here, not by label
                pc._u2_status = "err unknown command: act"
                return None
            return "100"  # the quiet wait after a tap
        self.allow("u2sock", side_effect=u2)
        # These test the tap decision only: no read after, no evidence.
        args = self.parse([argv[0], "--quiet", "--no-evidence"] + argv[1:])
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_tap(args)
        return rc, out.getvalue(), err.getvalue(), tc

    def test_ambiguous_tap_refused(self):
        rc, out, err, tc = self._tap(["tap", "OK"])
        self.assertEqual(rc, 1)
        self.assertIn("ambiguous tap", err)
        self.assertIn("2 candidates", err)
        self.assertIn("use --index N or a longer label", err)
        self.assertIn("[0]", err)
        self.assertIn("[1]", err)
        tc.assert_not_called()

    def test_ambiguous_tap_index_selects(self):
        rc, out, err, tc = self._tap(["tap", "OK", "--index", "1"])
        self.assertEqual(rc, 0)
        tc.assert_called_once_with(250, 650)  # second OK button

    def test_single_match_needs_no_index(self):
        rc, out, err, tc = self._tap(["tap", "Cancel"])
        self.assertEqual(rc, 0)
        tc.assert_called_once_with(650, 450)

    def test_fallback_selector_second_label_wins(self):
        rc, out, err, tc = self._tap(["tap", "Nope || Cancel"])
        self.assertEqual(rc, 0)
        tc.assert_called_once_with(650, 450)

    def test_fallback_selector_first_hit_wins(self):
        rc, out, err, tc = self._tap(["tap", "Cancel || OK"])
        self.assertEqual(rc, 0)
        tc.assert_called_once_with(650, 450)  # Cancel matched first

    def test_fallback_all_miss(self):
        rc, out, err, tc = self._tap(["tap", "Nope || Nada"])
        self.assertEqual(rc, 1)
        self.assertIn("no match", err)
        tc.assert_not_called()

    def test_fuzzy_ambiguity_flagged(self):
        rc, out, err, tc = self._tap(["tap", "ok"], xml=FUZZY_AMBI_XML)
        self.assertEqual(rc, 1)
        self.assertIn("ambiguous tap", err)
        self.assertIn("(fuzzy)", err)

    def test_ambiguous_json_shape(self):
        self.allow("wake_async")
        self.allow("ui_dump", return_value=ET.fromstring(AMBI_XML))
        self.allow("tap_center")
        args = self.parse(["tap", "--json", "--no-evidence", "OK"])
        with self.cap() as (out, err):
            rc = pc.cmd_tap(args)
        self.assertEqual(rc, 1)
        body = json.loads(out.getvalue())
        self.assertFalse(body["ok"])
        self.assertIn("ambiguous tap", body["error"])


class SnapTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._dir_patch = mock.patch.object(pc, "SNAP_DIR", self._tmp.name)
        self._dir_patch.start()
        self.addCleanup(self._dir_patch.stop)
        self._snap_patch = mock.patch.object(
            pc, "SNAP_PATH", os.path.join(self._tmp.name, "snap.json"))
        self._snap_patch.start()
        self.addCleanup(self._snap_patch.stop)

    def _save(self, xml=AMBI_XML):
        nodes = pc.walk(ET.fromstring(xml))
        return pc.snap_save(nodes)

    def test_snap_numbers_handles(self):
        numbered = self._save()
        handles = [h for h, _ in numbered]
        # Handles are generation-pinned: @eN~s<gen>
        self.assertTrue(handles[0].startswith("@e1~s"))
        self.assertTrue(all(h.startswith("@e") for h in handles))
        self.assertTrue(all("~s" in h for h in handles))
        # 3 visible nodes: the empty FrameLayout root carries no label
        self.assertEqual(len(numbered), 3)

    def test_snap_file_persists_entries(self):
        self._save()
        with open(os.path.join(self._tmp.name, "snap.json")) as f:
            snap = json.load(f)
        self.assertIn("ts", snap)
        self.assertEqual(snap["entries"]["1"]["x"], 250)

    def test_snap_resolve_roundtrip(self):
        self._save()
        self.assertEqual(pc.snap_resolve("@e1"), (250, 450))
        self.assertEqual(pc.snap_resolve("  @e3 "), (250, 650))

    def test_snap_resolve_no_file(self):
        with self.assertRaisesRegex(ValueError, "run `burner snap` first"):
            pc.snap_resolve("@e1")

    def test_snap_resolve_bad_handle(self):
        with self.assertRaisesRegex(ValueError, "not a snap handle"):
            pc.snap_resolve("OK")

    def test_snap_resolve_expired(self):
        self._save()
        path = os.path.join(self._tmp.name, "snap.json")
        with open(path) as f:
            snap = json.load(f)
        snap["ts"] -= pc.SNAP_TTL + 10
        with open(path, "w") as f:
            json.dump(snap, f)
        with self.assertRaisesRegex(ValueError, "expired"):
            pc.snap_resolve("@e1")

    def test_snap_resolve_missing_entry(self):
        self._save()
        with self.assertRaisesRegex(ValueError, "no such handle"):
            pc.snap_resolve("@e99")

    def test_u2_invalidate_drops_snap(self):
        # Every mutating action funnels through u2_invalidate(); it must
        # delete the snap file so @eN handles never outlive their screen.
        self._save()
        path = os.path.join(self._tmp.name, "snap.json")
        self.assertTrue(os.path.exists(path))
        guard = self._guards["u2_invalidate"]
        with mock.patch.object(pc, "u2_invalidate", guard.temp_original):
            self.allow("u2sock", return_value=None)
            si = self.allow("snap_invalidate")
            pc.u2_invalidate()
            si.assert_called_once_with()

    def test_cmd_snap_output(self):
        self.allow("ui_dump", return_value=ET.fromstring(AMBI_XML))
        args = self.parse(["snap"])
        with self.cap() as (out, err):
            rc = pc.cmd_snap(args)
        self.assertEqual(rc, 0)
        lines = out.getvalue().strip().split("\n")
        # Generation-pinned handle: @e1~s<gen>
        self.assertTrue(lines[0].startswith("@e1~s"))
        self.assertIn("OK", lines[0])

    def test_tap_at_snap_handle(self):
        self._save()
        self.allow("wake_async")
        self.allow("ui_dump", return_value=ET.fromstring(AMBI_XML))
        tc = self.allow("tap_center")
        self.allow("u2sock", return_value="100")
        args = self.parse(["tap", "--quiet", "@e2"])
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_tap(args)
        self.assertEqual(rc, 0)
        tc.assert_called_once_with(650, 450)  # Cancel button coords
        self.assertIn("@e2", out.getvalue())

    def test_tap_stale_snap_fails(self):
        args = self.parse(["tap", "--no-evidence", "@e1"])
        with self.cap() as (out, err):
            rc = pc.cmd_tap(args)
        self.assertEqual(rc, 1)
        self.assertIn("no snap saved", err.getvalue())


class SettleTests(OfflineTestCase):
    def _lines(self, xml):
        return pc.visible_lines(pc.walk(ET.fromstring(xml)))

    def test_settle_unchanged(self):
        root = ET.fromstring(SETTLE_A_XML)
        self.allow("u2sock", return_value=None)  # old helper: polling path
        self.allow("ui_dump", return_value=root)
        added, removed, final_fp = pc.settle_lines(self._lines(SETTLE_A_XML),
                                                   timeout=2, quiet=0.05, poll=0.01)
        self.assertEqual((added, removed), ([], []))
        self.assertIsNotNone(final_fp)

    def test_settle_detects_change(self):
        roots = [ET.fromstring(SETTLE_B_XML)] * 30
        self.allow("u2sock", return_value=None)
        self.allow("ui_dump", side_effect=roots)
        added, removed, final_fp = pc.settle_lines(self._lines(SETTLE_A_XML),
                                                   timeout=2, quiet=0.05, poll=0.01)
        self.assertTrue(any("Results" in l for l in added))
        self.assertTrue(any("Loading" in l for l in removed))

    def test_settle_partial_dump_retried(self):
        full = _twelve_line_xml()
        tiny = SETTLE_A_XML  # 2 lines vs 13: <=20% of a 12+ screen
        roots = [ET.fromstring(tiny)] + [ET.fromstring(full)] * 6
        self.allow("u2sock", return_value=None)
        self.allow("ui_dump", side_effect=roots)
        added, removed, final_fp = pc.settle_lines(self._lines(full),
                                                   timeout=5, quiet=0.05, poll=0.01)
        # The partial read must not surface as a mass disappearance.
        self.assertEqual((added, removed), ([], []))

    def test_settle_waits_on_the_phone_then_reads_once(self):
        sock = self.allow("u2sock", return_value="620")
        dump = self.allow("ui_dump", return_value=ET.fromstring(SETTLE_B_XML))
        added, removed, _fp = pc.settle_lines(self._lines(SETTLE_A_XML), timeout=2)
        self.assertEqual(sock.call_args[0][0], "idle")
        self.assertEqual(dump.call_count, 1)
        self.assertTrue(any("Results" in l for l in added))
        self.assertTrue(any("Loading" in l for l in removed))

    def test_tap_settle_reports_unchanged(self):
        self.allow("wake_async")
        self.allow("ui_dump", return_value=ET.fromstring(TAP_XML))
        tc = self.allow("tap_center")
        args = self.parse(["tap", "--settle", "Not now"])
        with self.cap() as (out, err):
            with mock.patch.object(pc, "settle_lines",
                                   return_value=([], [], "rid:test123")) as sl:
                rc = pc.cmd_tap(args)
        self.assertEqual(rc, 0)
        tc.assert_called_once()
        sl.assert_called_once()
        self.assertIn("unchanged", out.getvalue())

    def test_tap_settle_json_shape(self):
        self.allow("wake_async")
        self.allow("ui_dump", return_value=ET.fromstring(TAP_XML))
        self.allow("tap_center")
        args = self.parse(["tap", "--json", "--settle", "Not now"])
        with self.cap() as (out, err):
            with mock.patch.object(
                    pc, "settle_lines",
                    return_value=(["+ Results [TextView] (300,250)"],
                                  ["- Loading [TextView] (300,250)"],
                                  "rid:test123")):
                rc = pc.cmd_tap(args)
        self.assertEqual(rc, 0)
        body = json.loads(out.getvalue())
        self.assertTrue(body["ok"])
        self.assertIn("settled", body)
        self.assertEqual(len(body["settled"]["added"]), 1)

    def test_print_settle_diff_cap(self):
        added = ["line%d" % i for i in range(100)]
        with self.cap() as (out, err):
            pc._print_settle_diff(added, [], cap=80)
        text = out.getvalue()
        self.assertIn("settled: +100 -0", text)
        self.assertIn("... 20 more", text)


# --------------------------------- 12. setup wizard

def _setup_args(**kw):
    base = dict(list_steps=False, step=None, confirm=False, yes=True,
                code=None, ip=None, pair_port=None, connect_port=None)
    base.update(kw)
    return SimpleNamespace(**base)


class SetupWizardTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        # Redirect the setup state file to a temp dir.
        self.tmp = tempfile.mkdtemp()
        self._sp = mock.patch.object(pc, "SETUP_STATE_PATH",
                                     os.path.join(self.tmp, "setup-state.json"))
        self._sp.start()
        self.addCleanup(self._sp.stop)

    def _pair(self, proxy):
        """Run _run_pair with adb pair failing; return (spawn argv, popen)."""
        args = SimpleNamespace(code="123456", ip="100.1.2.3", pair_port="41000",
                               connect_port=None)
        env = {k: v for k, v in os.environ.items() if k != "HTTPS_PROXY"}
        if proxy:
            env["HTTPS_PROXY"] = proxy
        failed = SimpleNamespace(returncode=1, stdout="", stderr="")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(pc, "_setup_spawn", return_value=failed) as sp, \
                mock.patch.object(pc, "_setup_popen") as po, \
                mock.patch.object(pc.time, "sleep"), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(pc._run_pair(args), 1)
        return sp.call_args[0][0], po

    def test_pair_direct_without_proxy(self):
        argv, popen = self._pair(None)
        self.assertEqual(argv[1:], ["pair", "100.1.2.3:41000"])
        popen.assert_not_called()

    def test_pair_through_sandbox_proxy(self):
        argv, popen = self._pair("http://user@10.0.0.1:8080")
        self.assertEqual(argv[1:], ["pair", "127.0.0.1:15556"])
        self.assertIn("PROXY:10.0.0.1:100.1.2.3:41000", " ".join(popen.call_args[0][0]))
        popen.return_value.terminate.assert_called_once()

    def _adb(self, table):
        """Build a mock for pc.adb from {(cmd tuple): stdout}."""
        def fake(*args, **kwargs):
            key = tuple(str(a) for a in args)
            if key in table:
                return SimpleNamespace(returncode=0, stdout=table[key],
                                       stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="no")
        return fake

    def test_step_registry_order_and_kinds(self):
        names = [s["name"] for s in pc.SETUP_STEPS]
        self.assertEqual(names, ["prereqs", "tailnet", "tailscale-phone", "dev-options",
                                 "wireless-debug", "pair", "verify",
                                 "tailscale-battery", "stay-awake",
                                 "screen-lock",
                                 "install-adb-auto-enable", "self-pair",
                                 "always-on-vpn", "fix-port"])
        kinds = {s["name"]: s["kind"] for s in pc.SETUP_STEPS}
        self.assertEqual(kinds["prereqs"], "agent")
        self.assertEqual(kinds["tailscale-phone"], "human")
        self.assertEqual(kinds["pair"], "agent")
        self.assertEqual(kinds["self-pair"], "agent")
        # Human steps must carry an instruction and a screenshot path.
        for s in pc.SETUP_STEPS:
            if s["kind"] == "human":
                self.assertTrue(s["instruction"], s["name"])
                self.assertTrue(s["shot"], s["name"])
                self.assertNotIn("\u2014", s["instruction"])  # no em dashes

    def test_list_steps(self):
        m = self.allow("adb")
        m.side_effect = self._adb({("get-state",): "unknown"})
        with self.cap() as (out, err):
            rc = pc.cmd_setup(_setup_args(list_steps=True))
        self.assertEqual(rc, 0)
        text = out.getvalue()
        for name in ("prereqs", "pair", "verify"):
            self.assertIn(name, text)

    def test_human_step_prints_instruction_no_block(self):
        m = self.allow("adb")
        m.side_effect = self._adb({})
        with self.cap() as (out, err):
            rc = pc.cmd_setup(_setup_args(step="tailscale-phone"))
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("Tailscale", text)

    def test_confirm_records_human_done(self):
        with self.cap():
            rc = pc.cmd_setup(_setup_args(step="tailscale-phone", confirm=True))
        self.assertEqual(rc, 0)
        st = pc._setup_state_load()
        self.assertIn("tailscale-phone", st["done"])

    def test_uninstall_without_yes_only_lists(self):
        m = self.allow("adb")
        with self.cap() as (out, err):
            rc = pc.cmd_uninstall(SimpleNamespace(yes=False))
        self.assertEqual(rc, 0)
        self.assertIn("Run with --yes", out.getvalue())
        m.assert_not_called()

    def test_scroll_to_stops_when_text_visible(self):
        xml = '<hierarchy><node text="Checkout" bounds="[0,0][10,10]"/></hierarchy>'
        with mock.patch.object(pc, "ui_dump",
                               return_value=pc.ET.fromstring(xml)),                 mock.patch.object(pc, "adb_or_ensure") as sw:
            with self.cap() as (out, err):
                rc = pc.cmd_scroll(SimpleNamespace(direction="down", times=1,
                                                   to="Checkout"))
        self.assertEqual(rc, 0)
        sw.assert_not_called()

    def test_scrcpy_ping_sends_nothing_to_the_phone(self):
        # ping used to send a BACK key-up, which pressed Back on the phone
        # whenever a command healed the helpers.
        sys.path.insert(0, os.path.join(pc.ROOT, "lib", "scrcpy"))
        import scrcpy_ctl

        class FakeSock:
            def __init__(self, closed):
                self.closed, self.sent = closed, []

            def setblocking(self, flag):
                pass

            def settimeout(self, t):
                pass

            def recv(self, n, flags=0):
                if self.closed:
                    return b""
                raise BlockingIOError()

            def send(self, data):
                self.sent.append(data)

            sendall = send

        ctl = scrcpy_ctl.ScrcpyControl.__new__(scrcpy_ctl.ScrcpyControl)
        ctl.sock = FakeSock(closed=False)
        self.assertTrue(ctl.ping())
        self.assertEqual(ctl.sock.sent, [])  # nothing reached the phone
        ctl.sock = FakeSock(closed=True)
        self.assertFalse(ctl.ping())

    def test_screen_size_and_package_from_dump(self):
        xml = ('<hierarchy rotation="0">'
               '<node package="com.android.systemui" bounds="[0,0][1080,100]"/>'
               '<node package="com.android.settings" bounds="[0,0][1080,2400]"/>'
               '<node package="com.android.settings" bounds="[0,100][1080,300]"/>'
               '</hierarchy>')
        root = pc.ET.fromstring(xml)
        pc._update_screen_from_dump(root)
        self.assertEqual(pc.screen_dims(), (1080, 2400))
        self.assertEqual(pc.dump_package(root), "com.android.settings")

    def test_screen_size_counts_the_nav_bar_window(self):
        # An older app's window stops above the nav bar; the screen doesn't.
        xml = ('<hierarchy rotation="0">'
               '<node package="com.example" bounds="[0,0][1080,2274]"/>'
               '<node package="com.android.systemui" bounds="[0,2274][1080,2400]"/>'
               '</hierarchy>')
        pc._update_screen_from_dump(pc.ET.fromstring(xml))
        self.assertEqual(pc.screen_dims(), (1080, 2400))

    def test_dialog_only_dump_asks_the_phone_for_the_size(self):
        def live():
            pc._screen_wh = "1440 3120"
            return 1440, 3120
        dialog = ('<hierarchy rotation="0">'
                  '<node package="com.android.settings" bounds="[84,900][1356,1700]"/>'
                  '</hierarchy>')
        full = '<hierarchy rotation="0"><node bounds="[0,0][1080,2400]"/></hierarchy>'
        self.addCleanup(setattr, pc, "_screen_wh", pc._screen_wh)
        self.addCleanup(setattr, pc, "_screen_guessed", pc._screen_guessed)
        with mock.patch.object(pc, "live_screen_dims", side_effect=live) as m:
            pc._update_screen_from_dump(pc.ET.fromstring(dialog))
            self.assertEqual(pc._screen_for_tap(), (1440, 3120))
            pc._update_screen_from_dump(pc.ET.fromstring(full))
            self.assertEqual(pc._screen_for_tap(), (1080, 2400))
        self.assertEqual(m.call_count, 1)  # only the dialog-only dump asked
        with mock.patch.object(pc, "live_screen_dims", return_value=None):
            pc._update_screen_from_dump(pc.ET.fromstring(dialog))
            self.assertEqual(pc._screen_for_tap(), (1080, 2400))  # kept guess

    def test_commands_queue_behind_each_other(self):
        # A fake fcntl so the logic is tested on every platform: flock
        # raises OSError while another "process" holds the lock.
        class FakeFcntl:
            LOCK_EX, LOCK_NB = 2, 4
            held = False

            @classmethod
            def flock(cls, f, flags):
                if cls.held:
                    raise OSError("locked")
                cls.held = True

        path = os.path.join(tempfile.mkdtemp(), "burner.lock")
        env = {k: v for k, v in os.environ.items() if k != "BURNER_LOCK_HELD"}
        with mock.patch.dict(os.environ, env, clear=True),                 mock.patch.dict(sys.modules, {"fcntl": FakeFcntl}):
            first = pc._acquire_lock(path, timeout=1)
            self.assertIsNotNone(first)
            self.assertEqual(os.environ.get("BURNER_LOCK_HELD"), "1")
            # Child burner processes inherit the flag and skip the lock.
            self.assertIsNone(pc._acquire_lock(path, timeout=0.5))
            # A second, separate command waits, then gives up and runs anyway.
            os.environ.pop("BURNER_LOCK_HELD")
            with self.cap():
                self.assertIsNone(pc._acquire_lock(path, timeout=0.5))
            first.close()

    def test_unlabeled_button_over_label_is_not_an_occluder(self):
        # Play Store: the "Install" text sits under an unlabeled Button that
        # is the real tap surface; burner used to refuse the tap as covered.
        label = {"text": "Install", "desc": "", "class": "android.widget.TextView",
                 "bounds": "[470,1200][610,1260]", "clickable": False}
        button = {"text": "", "desc": "", "class": "android.widget.Button",
                  "bounds": "[64,1176][1017,1281]", "clickable": True}
        nodes = [label, button]
        self.assertIsNone(pc.is_point_covered(nodes, 540, 1230, label))
        # Compose draws tabs as plain, unlabeled Views (the Play Store bottom
        # bar "Search" tab): also the tap surface, not a blocker.
        tab = {"text": "Search", "desc": "", "class": "android.widget.TextView",
               "bounds": "[470,2230][610,2280]", "clickable": False}
        view = {"text": "", "desc": "", "class": "android.view.View",
                "bounds": "[432,2169][648,2337]", "clickable": False}
        self.assertIsNone(pc.is_point_covered([tab, view], 540, 2253, tab))
        # Big unlabeled page content (a web page's container view) isn't a
        # blocker either; it only ever hid the page's own controls.
        page = {"text": "", "desc": "", "class": "android.view.View",
                "bounds": "[0,200][1080,2200]", "clickable": False}
        self.assertIsNone(pc.is_point_covered([label, page], 540, 1230, label))
        # The same control twice (YouTube Music: a "Close" icon and a
        # "Close" ViewGroup over it) is one button, not a blocker.
        icon = {"text": "", "desc": "Close", "class": "android.widget.ImageView",
                "bounds": "[940,150][990,200]", "clickable": False}
        group = {"text": "", "desc": "Close", "class": "android.view.ViewGroup",
                 "bounds": "[914,131][1009,226]", "clickable": True}
        self.assertIsNone(pc.is_point_covered([icon, group], 961, 178, icon))
        # Something else with its own label on top does block.
        other = {"text": "Start free trial", "desc": "", "class": "android.view.ViewGroup",
                 "bounds": "[914,131][1009,226]", "clickable": True}
        self.assertIs(pc.is_point_covered([icon, other], 961, 178, icon), other)
        # A real dialog covering the label still counts.
        dialog = {"text": "Update?", "desc": "", "class": "android.app.Dialog",
                  "bounds": "[64,1000][1017,1400]", "clickable": False}
        self.assertIs(pc.is_point_covered([label, dialog], 540, 1230, label),
                      dialog)
        # A full-screen unlabeled button (a scrim) still counts too.
        scrim = {"text": "", "desc": "", "class": "android.widget.Button",
                 "bounds": "[0,0][1080,2400]", "clickable": True}
        self.assertIs(pc.is_point_covered([label, scrim], 540, 1230, label),
                      scrim)

    def test_pick_dismiss_prefers_the_safest_label(self):
        def n(text, cx=500, cy=900):
            return {"text": text, "desc": "", "center": (cx, cy)}
        nodes = [n("Close"), n("Got it"), n("Not now"), n("Allow")]
        self.assertEqual(pc.pick_dismiss(nodes)["text"], "Not now")
        self.assertEqual(pc.pick_dismiss([n("Close"), n("Got it")])["text"],
                         "Got it")
        # Never picks agreeing buttons.
        self.assertIsNone(pc.pick_dismiss([n("Allow"), n("OK"), n("Accept")]))
        self.assertIsNone(pc.pick_dismiss([]))

    def test_dedupe_same_control(self):
        # Play Store: Install is a View described "Install" and a TextView
        # reading "Install" at the same spot. That's one button.
        view = {"text": "", "desc": "Install", "bounds": "[64,1176][1017,1281]"}
        text = {"text": "Install", "desc": "", "bounds": "[470,1200][610,1260]"}
        other = {"text": "Install", "desc": "", "bounds": "[64,300][400,380]"}
        self.assertEqual(pc.dedupe_same_control([view, text]), [view])
        # A different Install elsewhere on screen stays separate.
        self.assertEqual(len(pc.dedupe_same_control([view, text, other])), 2)

    def test_check_save_steps(self):
        ok = ['open "https://example.com/orders"', 'wait "Your Orders"',
              'tap "Orders"', 'type "$QUERY"', 'press BACK']
        self.assertIsNone(pc.check_save_steps(ok))
        # Handles last for one screen only.
        self.assertIn("snap handle", pc.check_save_steps(['tap @e3']))
        # Fixed typed text would save personal text into the recipe.
        self.assertIn("fixed text", pc.check_save_steps(['type "hunter2"']))
        self.assertIsNone(pc.check_save_steps(['type "chill"'], allow_text=True))
        # Only screen-driving verbs, and valid ones.
        self.assertIn("must start with", pc.check_save_steps(['uninstall --yes']))
        self.assertIn("unbalanced", pc.check_save_steps(['tap "Orders']))
        self.assertIn("isn't a valid", pc.check_save_steps(['scroll sideways']))

    def test_save_writes_a_recipe(self):
        tmp = tempfile.mkdtemp()
        with mock.patch.object(pc, "ROOT", tmp):
            args = SimpleNamespace(name="weekly-orders", desc="Open my orders",
                                   steps=['open "https://example.com"',
                                          'wait "Orders"'],
                                   force=False, allow_text=False)
            with self.cap():
                self.assertEqual(pc.cmd_save(args), 0)
                self.assertEqual(pc.cmd_save(args), 1)  # exists, no --force
            path = os.path.join(tmp, "recipes", "weekly-orders.burner")
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("# Open my orders", text)
            self.assertIn('wait "Orders"', text)
            args.name = "Bad Name"
            with self.cap():
                self.assertEqual(pc.cmd_save(args), 1)

    def test_split_marked(self):
        nl = chr(10)
        text = nl.join(["@@a", "one", "two", "@@b", "three", ""])
        parts = pc.split_marked(text)
        self.assertEqual(parts["a"], "one" + nl + "two" + nl)
        self.assertEqual(parts["b"], "three" + nl)
        self.assertEqual(pc.split_marked(""), {})

    def test_app_words_map_names_to_packages(self):
        self.assertEqual(pc.APP_WORDS["messages"], "messaging")
        self.assertIn("messaging", "com.google.android.apps.messaging")

    def test_parse_notifications(self):
        def row(y1, y2, *texts, clickable="true"):
            kids = "".join('<node package="com.android.systemui" text="{}"'
                           ' bounds="[231,{}][996,{}]"/>'.format(t, y1, y2)
                           for t in texts)
            return ('<node package="com.android.systemui" clickable="{}"'
                    ' bounds="[42,{}][1038,{}]">{}</node>'.format(
                        clickable, y1, y2, kids))
        xml = "<hierarchy>" + "".join([
            row(622, 1069, "Muse", "Stopped.", "Reply", "2 hours ago"),
            row(1116, 1305, "Reduce screen timeout", "•", "2 hours ago",
                "Long timeout drains battery"),
            row(1400, 1500, "Reduce screen timeout", "Long timeout drains battery"),
            row(1600, 1700, "ignored", clickable="false"),
            row(100, 1900, "Whole group"),
            row(1800, 1900, "Muse - Stopped."),
            '<node package="com.other" clickable="true" bounds="[42,0][1038,300]">'
            '<node package="com.other" text="not mine"/></node>',
        ]) + "</hierarchy>"
        self.assertEqual(pc.parse_notifications(ET.fromstring(xml)), [
            "Muse - Stopped.",
            "Reduce screen timeout - Long timeout drains battery"])
        self.assertEqual(pc.notification_rows(ET.fromstring(xml))[-1][1], 1305)
        self.assertEqual(pc.parse_notifications(ET.fromstring("<hierarchy/>")), [])

    def test_drop_repeats(self):
        self.assertEqual(
            pc.drop_repeats(["Muse - Stopped.", "Stopped.", "Timeout",
                             "Timeout - Long", "Muse - Stopped."]),
            ["Muse - Stopped.", "Timeout - Long"])

    def test_parse_plain_tailscale_status(self):
        f = pc.parse_plain_tailscale_status
        self.assertEqual(f("Connected"), {"BackendState": "Running"})
        self.assertEqual(f("Tailscale is running"), {"BackendState": "Running"})
        self.assertEqual(f("Logged out."), {"BackendState": "Stopped"})
        self.assertEqual(f("Tailscale is stopped."), {"BackendState": "Stopped"})
        self.assertEqual(f("Not connected"), {"BackendState": "Stopped"})
        self.assertIsNone(f(""))
        self.assertIsNone(f("something else"))

    def test_main_reports_a_timed_out_phone_without_a_traceback(self):
        fake = SimpleNamespace(fn=mock.Mock(
            side_effect=subprocess.TimeoutExpired("adb", 30)))
        err = io.StringIO()
        with mock.patch.object(pc, "build_parser") as bp,                 mock.patch.object(pc, "record_command_line", return_value=None),                 mock.patch.object(pc, "_arm_watchdog"),                 mock.patch.object(sys, "argv", ["burner", "status"]),                 contextlib.redirect_stderr(err):
            bp.return_value.parse_args.return_value = fake
            with self.assertRaises(SystemExit) as cm:
                pc.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("didn't answer in time", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_parse_phone_ping(self):
        f = pc.parse_phone_ping
        self.assertTrue(f(0, "1 packets transmitted, 1 received")[0])
        self.assertFalse(f(1, "ping: unknown host connectivitycheck.gstatic.com")[0])
        self.assertIn("DNS", f(1, "ping: bad address 'x'")[1])
        self.assertFalse(f(1, "connect: Network is unreachable")[0])
        self.assertTrue(f(127, "ping: not found")[0])
        self.assertTrue(f(1, "something odd")[0])

    def test_main_reports_an_unreadable_screen_without_a_traceback(self):
        fake = SimpleNamespace(fn=mock.Mock(side_effect=RuntimeError(
            "uiautomator dump kept coming back empty: ''")))
        err = io.StringIO()
        with mock.patch.object(pc, "build_parser") as bp,                 mock.patch.object(pc, "record_command_line", return_value=None),                 mock.patch.object(pc, "_arm_watchdog"),                 mock.patch.object(sys, "argv", ["burner", "state"]),                 contextlib.redirect_stderr(err):
            bp.return_value.parse_args.return_value = fake
            with self.assertRaises(SystemExit) as cm:
                pc.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("couldn't read the phone's screen", err.getvalue())

    def test_parse_status(self):
        nl = chr(10)
        st = pc.parse_status(
            nl.join(["  AC powered: false", "  USB powered: true",
                     "  level: 78", ""]),
            "  mWakefulness=Awake" + nl,
            "  mCurrentFocus=Window{abc u0 com.snapchat.android/com.snap.Main}"
            + nl,
            nl.join(["Filesystem 1K-blocks Used Available Use% Mounted on",
                     "/dev/x 100 41 59 41% /data", ""]),
            nl.join(["package:com.a", "package:com.b", ""]),
            "Pixel 7a" + nl, "16" + nl)
        self.assertEqual(st["battery_percent"], 78)
        self.assertTrue(st["charging"])
        self.assertEqual(st["screen"], "on")
        self.assertEqual(st["app"], "com.snapchat.android")
        self.assertEqual(st["storage_used_percent"], 41)
        self.assertEqual(st["installed_apps"], 2)
        self.assertEqual(st["model"], "Pixel 7a")

    def test_read_pair_dialog(self):
        xml = ('<hierarchy><node text="Pair with device" bounds="[0,0][1,1]"/>'
               '<node text="Wi-Fi pairing code" bounds="[0,0][1,1]"/>'
               '<node text="482915" bounds="[0,0][1,1]"/>'
               '<node text="IP address &amp; Port" bounds="[0,0][1,1]"/>'
               '<node text="100.64.1.2:37129" bounds="[0,0][1,1]"/>'
               '</hierarchy>')
        with mock.patch.object(pc, "ui_dump",
                               return_value=pc.ET.fromstring(xml)):
            self.assertEqual(pc._setup_read_pair_dialog(),
                             ("482915", "37129"))

    def test_self_pair_already_paired(self):
        with mock.patch.object(pc, "_setup_app_status",
                               return_value={"isPaired": True}):
            with self.cap() as (out, err):
                rc = pc._run_self_pair(_setup_args())
        self.assertEqual(rc, 0)
        self.assertIn("already paired", out.getvalue())

    def test_unknown_status_falls_back_to_state_file(self):
        pc._setup_state_save("self-pair")
        m = self.allow("adb")
        m.side_effect = self._adb({})
        step = next(s for s in pc.SETUP_STEPS if s["name"] == "self-pair")
        self.assertEqual(pc._setup_step_status(step), "done")

    def test_live_false_beats_state_file(self):
        pc._setup_state_save("wireless-debug")
        m = self.allow("adb")
        m.side_effect = self._adb({("shell", "settings", "get", "global",
                                    "adb_wifi_enabled"): "0"})
        step = next(s for s in pc.SETUP_STEPS if s["name"] == "wireless-debug")
        self.assertEqual(pc._setup_step_status(step), "pending")

    def test_check_wireless_debug_true(self):
        m = self.allow("adb")
        m.side_effect = self._adb({("shell", "settings", "get", "global",
                                    "adb_wifi_enabled"): "1"})
        self.assertTrue(pc._check_wireless_debug())

    def test_check_always_on_vpn(self):
        m = self.allow("adb")
        m.side_effect = self._adb({("shell", "settings", "get", "secure",
                                    "always_on_vpn_app"): "com.tailscale.ipn"})
        self.assertTrue(pc._check_always_on_vpn())

    def test_run_always_on_vpn_refuses_lockdown(self):
        m = self.allow("adb")
        m.side_effect = self._adb({("shell", "settings", "get", "secure",
                                    "always_on_vpn_lockdown"): "1"})
        with self.cap() as (out, err):
            rc = pc._run_always_on_vpn(_setup_args())
        self.assertEqual(rc, 1)
        self.assertIn("lockdown", err.getvalue().lower())

    def test_run_always_on_vpn_success(self):
        calls = []

        def fake_adb(*args, **kwargs):
            calls.append(tuple(str(a) for a in args))
            key = tuple(str(a) for a in args)
            if key == ("shell", "settings", "get", "secure",
                       "always_on_vpn_lockdown"):
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if key == ("shell", "settings", "put", "secure",
                       "always_on_vpn_app", "com.tailscale.ipn"):
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if key == ("shell", "settings", "get", "secure",
                       "always_on_vpn_app"):
                return SimpleNamespace(returncode=0, stdout="com.tailscale.ipn",
                                       stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        m = self.allow("adb")
        m.side_effect = fake_adb
        with self.cap():
            rc = pc._run_always_on_vpn(_setup_args())
        self.assertEqual(rc, 0)

    def test_pair_needs_code_ip_port(self):
        self.allow("adb", return_value=SimpleNamespace(returncode=1, stdout="", stderr=""))
        with self.cap() as (out, err):
            rc = pc.cmd_setup(_setup_args(step="pair"))
        self.assertEqual(rc, 1)
        self.assertIn("--code", err.getvalue())

    def test_pair_rejects_bad_code(self):
        self.allow("adb", return_value=SimpleNamespace(returncode=1, stdout="", stderr=""))
        with self.cap():
            rc = pc.cmd_setup(_setup_args(step="pair", code="abc",
                                          ip="100.0.0.1", pair_port="1234"))
        self.assertEqual(rc, 1)

    def test_pair_code_never_written_to_disk(self):
        popen = self.allow("_setup_popen")
        popen.return_value = mock.Mock()
        spawn = self.allow("_setup_spawn")
        spawn.return_value = SimpleNamespace(returncode=0, stdout="paired",
                                             stderr="")
        # get-state: offline until pairing runs, then device.
        states = {"n": 0}

        def fake_adb(*args, **kwargs):
            key = tuple(str(a) for a in args)
            if key == ("get-state",):
                states["n"] += 1
                return SimpleNamespace(
                    returncode=0,
                    stdout="device" if states["n"] > 1 else "offline",
                    stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="no")
        m = self.allow("adb")
        m.side_effect = fake_adb
        write_cfg = self.allow("_setup_write_config")
        with self.cap():
            rc = pc.cmd_setup(_setup_args(step="pair", code="482913",
                                          ip="100.99.0.1", pair_port="37001",
                                          connect_port="5555"))
        self.assertEqual(rc, 0)
        # The code went to `adb pair` on stdin, not argv or disk.
        pair_call = [c for c in spawn.call_args_list
                     if c[0][0][:2] == [pc.ADB_BIN, "pair"]]
        self.assertTrue(pair_call)
        self.assertNotIn("482913", " ".join(pair_call[0][0][0]))
        self.assertEqual(pair_call[0][1].get("input_text").strip(), "482913")
        for root, _dirs, files in os.walk(self.tmp):
            for f in files:
                with open(os.path.join(root, f)) as fh:
                    self.assertNotIn("482913", fh.read())
        written = write_cfg.call_args[0][0]
        self.assertNotIn("482913", json.dumps(written))

    def test_setup_not_recorded(self):
        args = self.parse(["setup", "--step", "pair", "--code", "482913"])
        with mock.patch.object(pc.sys, "argv",
                               ["pc", "setup", "--step", "pair",
                                "--code", "482913"]):
            self.assertIsNone(pc.record_command_line(args))

    def test_install_app_downloads_apk(self):
        urlopen = self.allow("_setup_urlopen")
        urlopen.side_effect = [
            json.dumps({"assets": [
                {"name": "adb-auto-enable-v0.3.5.apk",
                 "browser_download_url": "https://example/x.apk"}]}).encode(),
            b"fake-apk-bytes",
        ]
        m = self.allow("adb")
        m.side_effect = self._adb({})
        with mock.patch.object(pc, "WORKSPACE", self.tmp):
            with self.cap():
                path = pc._setup_find_apk()
        self.assertTrue(path and path.endswith(".apk"))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"fake-apk-bytes")

    def test_install_app_uses_local_apk_first(self):
        apk = os.path.join(self.tmp, "adb-auto-enable-local.apk")
        with open(apk, "wb") as f:
            f.write(b"x")
        urlopen = self.allow("_setup_urlopen")
        with mock.patch.object(pc, "WORKSPACE", self.tmp):
            path = pc._setup_find_apk()
        self.assertEqual(path, apk)
        urlopen.assert_not_called()

    def test_write_config_preserves_comments(self):
        cfg = os.path.join(self.tmp, "config.env")
        with open(cfg, "w") as f:
            f.write('# comment\nADB_PORT="1234"\nLOCAL_PORT="15555"\n')
        with mock.patch.object(pc, "ROOT", self.tmp):
            pc._setup_write_config({"ADB_PORT": "5555",
                                    "PHONE_TAILSCALE_IP": "100.1.2.3"})
            with open(cfg) as f:
                text = f.read()
        self.assertIn("# comment", text)
        self.assertIn('ADB_PORT="5555"', text)
        self.assertIn('PHONE_TAILSCALE_IP="100.1.2.3"', text)
        self.assertNotIn('"1234"', text)

    def test_verify_reports_red_on_doctor_failure(self):
        with mock.patch.object(pc, "cmd_doctor", return_value=1):
            with self.cap() as (out, err):
                rc = pc._run_verify(_setup_args())
        self.assertEqual(rc, 1)
        self.assertIn("RED", err.getvalue())



# ------------------------------------------------------- fast paths via u2 mux

class FastPathTests(OfflineTestCase):
    def unguard(self, name):
        self._guards[name].stop()

    def test_tap_uses_mux_and_skips_adb(self):
        self.unguard("tap_center")
        self.allow("wake")
        self.allow("scrcpy_send", side_effect=RuntimeError("no scrcpy helper"))
        u2 = self.allow("u2sock", return_value="")
        adb = self.allow("adb_or_ensure")
        pc.tap_center(10, 20)
        u2.assert_called_once_with("tap", "10 20", timeout=30)
        adb.assert_not_called()

    def test_tap_falls_back_to_adb_when_mux_unreached(self):
        self.unguard("tap_center")
        self.allow("wake")
        self.allow("scrcpy_send", side_effect=RuntimeError("no scrcpy helper"))
        self.allow("u2sock", return_value=None)
        self.allow("_u2_status", new="unsent")
        adb = self.allow("adb_or_ensure")
        self.allow("u2_invalidate")
        pc.tap_center(10, 20)
        adb.assert_called_once_with("shell", "input", "tap", 10, 20)

    def test_tap_lost_reply_does_not_tap_again(self):
        self.unguard("tap_center")
        self.allow("wake")
        self.allow("scrcpy_send", side_effect=RuntimeError("no scrcpy helper"))
        self.allow("u2sock", return_value=None)
        self.allow("_u2_status", new="lost")
        adb = self.allow("adb_or_ensure")
        with self.cap() as (out, err):
            pc.tap_center(10, 20)
        adb.assert_not_called()
        self.assertIn("not retrying", err.getvalue())

    def test_tap_old_mux_without_command_uses_adb(self):
        self.unguard("tap_center")
        self.allow("wake")
        self.allow("scrcpy_send", side_effect=RuntimeError("no scrcpy helper"))
        self.allow("u2sock", return_value=None)
        self.allow("_u2_status", new="err unknown command: tap")
        adb = self.allow("adb_or_ensure")
        self.allow("u2_invalidate")
        pc.tap_center(10, 20)
        adb.assert_called_once_with("shell", "input", "tap", 10, 20)

    def test_shot_fast_writes_png_when_asked(self):
        import base64
        waker = mock.Mock()
        self.allow("wake_async", return_value=waker)
        u2 = self.allow("u2sock", return_value=base64.b64encode(b"\x89PNGdata").decode())
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "s.png")
            self.assertEqual(pc.shot_fast(out), out)
            with open(out, "rb") as f:
                self.assertEqual(f.read(), b"\x89PNGdata")
        u2.assert_called_once_with("shot", "png", timeout=30)
        waker.join.assert_called_once()

    def test_shot_fast_none_when_mux_down(self):
        self.allow("wake_async", return_value=mock.Mock())
        self.allow("u2sock", return_value=None)
        self.assertIsNone(pc.shot_fast(os.path.join(tempfile.gettempdir(), "x.jpg")))


    def test_start_one_adb_call_when_app_comes_up(self):
        self.allow("scrcpy_send", side_effect=RuntimeError("no scrcpy"))
        adb = self.allow("adb_or_ensure", return_value=SimpleNamespace(
            stdout="  mFocusedApp=ActivityRecord{1 u0 com.example/.Main t3}\n",
            stderr="", returncode=0))
        self.allow("u2_invalidate")
        self.allow("nav_record")
        self.allow("u2sock", return_value="100")  # the quiet wait after a launch
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_start(SimpleNamespace(package="com.example", quiet=True))
        self.assertEqual(rc, 0)
        self.assertEqual(adb.call_count, 1)
        self.assertIn("monkey -p 'com.example'", adb.call_args[0][1])

    def test_tap_prefers_scrcpy(self):
        self.unguard("tap_center")
        self.allow("wake")
        sc = self.allow("scrcpy_send", return_value=True)
        inv = self.allow("u2_invalidate")
        u2 = self.allow("u2sock")
        adb = self.allow("adb_or_ensure")
        pc.tap_center(10, 20)
        sc.assert_called_once_with("tap 10 20")
        inv.assert_called_once()
        u2.assert_not_called()
        adb.assert_not_called()

    def test_plain_scroll_via_scrcpy_reads_once_and_catches_leaving_the_app(self):
        self.allow("wake")
        self.allow("u2_invalidate")
        # The read after the swipes says which app is in front; only when
        # that differs from the app before them is the active window asked
        # too (another app's dialog in front reads as that app while the
        # window is still ours), so like is compared with like.
        screens = iter(["1080 2400 com.example", "1080 2400 com.android.launcher"])
        launcher_xml = SAMPLE_XML.replace('package="com.example"', 'package="com.android.launcher"')

        def u2(cmd, arg="", timeout=30):
            return next(screens) if cmd == "screen" else launcher_xml
        u2sock = self.allow("u2sock", side_effect=u2)
        sc = self.allow("scrcpy_send", return_value=True)
        adb = self.allow("adb_or_ensure")
        dump = self.allow("ui_dump")
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_scroll(SimpleNamespace(direction="down", times=3, to=None,
                                               quiet=False))
        self.assertEqual(rc, 1)
        self.assertEqual(sc.call_count, 3)
        sc.assert_called_with("swipe 540 1920 540 480 250")
        adb.assert_not_called()
        dump.assert_not_called()  # the one read rides on the helper's act
        self.assertEqual([c[0][0] for c in u2sock.call_args_list],
                         ["screen", "act", "screen"])
        self.assertIn("scroll left com.example", err.getvalue())
        self.assertIn("screen: com.android.launcher", out.getvalue())

    def test_plain_scroll_reads_after_a_short_quiet_wait(self):
        self.allow("wake")
        self.allow("u2_invalidate")
        for name in ("_screen_wh", "_screen_pkg", "_screen_kbd"):
            self.addCleanup(setattr, pc, name, getattr(pc, name))  # the real screen call sets them
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append((cmd, arg))
            return "1080 2400 com.example" if cmd == "screen" else SAMPLE_XML
        self.allow("u2sock", side_effect=u2)
        self.allow("scrcpy_send", return_value=True)
        self.allow("adb_or_ensure")
        self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_scroll(SimpleNamespace(direction="down", times=1, to=None,
                                               quiet=False))
        self.assertEqual(rc, 0)
        acts = [json.loads(a) for c, a in calls if c == "act"]
        self.assertEqual([a["idle"] for a in acts], [pc.IDLE_SCROLL_MS])

    def test_plain_scroll_via_scrcpy_prints_the_screen(self):
        self.allow("wake")
        self.allow("u2_invalidate")
        self.allow("u2sock", side_effect=lambda cmd, arg="", timeout=30:
                   "1080 2400 com.example" if cmd == "screen" else SAMPLE_XML)
        sc = self.allow("scrcpy_send", return_value=True)
        self.allow("adb_or_ensure")
        self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_scroll(SimpleNamespace(direction="down", times=1, to=None,
                                               quiet=False))
        self.assertEqual(rc, 0)
        self.assertEqual(sc.call_count, 1)
        self.assertIn("scrolled down x1", out.getvalue())
        self.assertIn("screen: com.example", out.getvalue())
        self.assertIn("Hello (300,250)", out.getvalue())
        # still in the app per the read: no second screen-info round trip
        self.assertEqual([c[0][0] for c in pc.u2sock.call_args_list], ["screen", "act"])

    def test_plain_scroll_one_adb_call_and_no_dump(self):
        self.allow("wake")
        self.allow("u2_invalidate")
        self.allow("live_screen_dims", return_value=(1080, 2400))
        dump = self.allow("ui_dump")
        focus = "  mCurrentFocus=Window{1 u0 com.example/com.example.Main}\n"
        adb = self.allow("adb_or_ensure", return_value=SimpleNamespace(
            stdout=focus + "@@burner@@\n@@burner@@\n" + focus, stderr="", returncode=0))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_scroll(SimpleNamespace(direction="down", times=1, to=None,
                                               quiet=True))
        self.assertEqual(rc, 0)
        dump.assert_not_called()
        self.assertEqual(adb.call_count, 1)
        self.assertIn("input swipe 540 1920 540 480 350", adb.call_args[0][1])

    def test_plain_scroll_stops_when_app_changes(self):
        self.allow("wake")
        self.allow("u2_invalidate")
        self.allow("live_screen_dims", return_value=(1080, 2400))
        out_txt = ("  mCurrentFocus=Window{1 u0 com.example/com.example.Main}\n"
                   "@@burner@@\n@@burner@@\n"
                   "  mCurrentFocus=Window{2 u0 com.android.launcher/x.Home}\n")
        self.allow("adb_or_ensure", return_value=SimpleNamespace(
            stdout=out_txt, stderr="", returncode=0))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_scroll(SimpleNamespace(direction="down", times=3, to=None,
                                               quiet=True))
        self.assertEqual(rc, 1)
        self.assertIn("scroll left com.example", err.getvalue())

    def test_start_retries_from_home_when_another_app_stays_in_front(self):
        self.allow("scrcpy_send", side_effect=RuntimeError("no scrcpy"))
        other = SimpleNamespace(
            stdout="  mFocusedApp=ActivityRecord{1 u0 com.android.vending/.X t3}\n",
            stderr="", returncode=0)
        adb = self.allow("adb_or_ensure", return_value=other)
        self.allow("u2_invalidate")
        with self.cap() as (out, err):
            rc = pc.cmd_start(SimpleNamespace(package="com.example"))
        self.assertEqual(rc, 1)
        self.assertEqual(adb.call_count, 2)
        self.assertIn("KEYCODE_HOME", adb.call_args[0][1])
        self.assertIn("com.android.vending is still open", err.getvalue())


# --------------------------------- 12. the screen printed after an action

SCREEN_XML = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" package="com.android.systemui" bounds="[0,0][1080,120]" clickable="false" enabled="true" focused="false" checked="false">
    <node text="4:05" class="android.widget.TextView" package="com.android.systemui" bounds="[40,20][120,100]" clickable="false" enabled="true" focused="false" checked="false"/>
  </node>
  <node text="" class="android.widget.FrameLayout" package="com.example" bounds="[0,120][1080,2400]" clickable="false" enabled="true" focused="false" checked="false">
    <node text="" class="android.widget.LinearLayout" package="com.example" bounds="[0,200][1080,400]" clickable="true" enabled="true" focused="false" checked="false">
      <node text="Connected devices" class="android.widget.TextView" package="com.example" bounds="[100,250][600,350]" clickable="false" enabled="true" focused="false" checked="false"/>
    </node>
    <node text="" content-desc="Bluetooth" class="android.widget.Switch" package="com.example" bounds="[900,250][1050,350]" clickable="true" enabled="true" focused="false" checked="true" checkable="true"/>
    <node text="" content-desc="Open" class="android.view.View" package="com.example" bounds="[800,500][1000,600]" clickable="true" enabled="true" focused="false" checked="false">
      <node text="Open" class="android.widget.TextView" package="com.example" bounds="[820,510][980,590]" clickable="false" enabled="true" focused="false" checked="false"/>
    </node>
    <node text="" content-desc="Vinted: Shop &amp; sell&#10;Installed&#10;" class="android.view.View" package="com.example" bounds="[0,700][1080,900]" clickable="false" enabled="true" focused="false" checked="false"/>
    <node text="" class="android.widget.EditText" package="com.example" bounds="[100,1000][900,1100]" clickable="true" enabled="true" focused="true" checked="false"/>
    <node text="" content-desc="More options" class="android.widget.ImageButton" package="com.example" bounds="[980,1000][1060,1080]" clickable="true" enabled="true" focused="false" checked="false"/>
    <node text="" class="android.view.View" package="com.example" bounds="[980,1200][1060,1280]" clickable="true" enabled="true" focused="false" checked="false"/>
  </node>
  <node text="" class="android.widget.FrameLayout" package="com.google.android.inputmethod.latin" bounds="[0,1500][1080,2400]" clickable="false" enabled="true" focused="false" checked="false">
    <node text="" content-desc="q" class="android.widget.FrameLayout" package="com.google.android.inputmethod.latin" bounds="[0,1600][100,1700]" clickable="true" enabled="true" focused="false" checked="false"/>
    <node text="" content-desc="w" class="android.widget.FrameLayout" package="com.google.android.inputmethod.latin" bounds="[100,1600][200,1700]" clickable="true" enabled="true" focused="false" checked="false"/>
  </node>
</hierarchy>"""


class ScreenRowsTests(OfflineTestCase):
    def rows(self, xml=SCREEN_XML):
        root = ET.fromstring(xml)
        pc._update_screen_from_dump(root)
        return pc.screen_lines(pc.walk(root), 1080, 2400)

    def test_status_bar_and_keyboard_left_out(self):
        lines, keyboard = self.rows()
        self.assertTrue(keyboard)
        text = "\n".join(lines)
        self.assertNotIn("4:05", text)
        self.assertNotIn("[q]", text)
        self.assertNotIn("[w]", text)

    def test_one_row_per_control(self):
        lines, _ = self.rows()
        # the row container has no words of its own: its child is the row
        self.assertEqual([l for l in lines if "Connected devices" in l],
                         ["Connected devices (350,300)"])
        # a control drawn twice is one row
        self.assertEqual(len([l for l in lines if "Open" in l]), 1)
        self.assertIn("[Open] (click) (900,550)", lines)

    def test_switch_state_field_and_icon(self):
        lines, _ = self.rows()
        self.assertIn("[Bluetooth] (click,on) [Switch] (975,300)", lines)
        self.assertIn("[text field] (click,focused) [EditText] (500,1050)", lines)
        self.assertIn("[More options] (click) [ImageButton] (1020,1040)", lines)
        # an icon-sized button with no words at all still shows (snap taps it)
        self.assertIn("(click) (1020,1240)", lines)

    def test_description_whitespace_collapsed(self):
        lines, _ = self.rows()
        self.assertIn("[Vinted: Shop & sell Installed] (540,800)", lines)

    def test_app_in_front_ignores_keyboard(self):
        root = ET.fromstring(SCREEN_XML)
        self.assertEqual(pc.dump_package(root), "com.example")

    def test_print_screen_header_and_cap(self):
        root = ET.fromstring(SCREEN_XML)
        with self.cap() as (out, err):
            pc.print_screen(root, cap=2)
        text = out.getvalue()
        self.assertTrue(text.startswith("screen: com.example (keyboard open)\n"))
        self.assertIn("more rows", text)

    def test_read_after_waits_then_reads_fresh(self):
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append((cmd, arg))
            return SAMPLE_XML
        self.allow("u2sock", side_effect=u2)
        dump = self.allow("ui_dump")
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            pc.read_after()
        self.assertEqual(calls, [("act", '{"idle": 1200}')])
        dump.assert_not_called()
        self.assertIn("screen: com.example", out.getvalue())

    def test_read_after_two_steps_with_an_old_helper(self):
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append(cmd)
            if cmd == "act":
                pc._u2_status = "err unknown command: act"
                return None
            return "500"
        self.allow("u2sock", side_effect=u2)
        dump = self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            pc.read_after()
        self.assertEqual(calls, ["act", "idle"])
        dump.assert_called_once_with(fresh=True)
        self.assertIn("screen: com.example", out.getvalue())

    def test_read_after_rereads_a_half_drawn_screen(self):
        self.allow("u2sock", return_value="100")
        empty = ET.fromstring('<hierarchy rotation="0"></hierarchy>')
        dump = self.allow("ui_dump", side_effect=[empty, ET.fromstring(SAMPLE_XML)])
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            pc.read_after()
        self.assertEqual(dump.call_count, 2)
        self.assertIn("screen: com.example", out.getvalue())

    def test_looks_half_drawn(self):
        self.assertTrue(pc.looks_half_drawn(0, None))
        self.assertTrue(pc.looks_half_drawn(2, 20))
        self.assertFalse(pc.looks_half_drawn(3, 20))   # a search box and its keyboard
        self.assertFalse(pc.looks_half_drawn(2, 8))    # a small screen before too
        self.assertFalse(pc.looks_half_drawn(6, 20))
        self.assertFalse(pc.looks_half_drawn(3, None))

    def test_quiet_still_waits_for_the_ui(self):
        calls = []
        self.allow("u2sock", side_effect=lambda cmd, arg="", timeout=30: calls.append(cmd) or "100")
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            pc.read_after(quiet=True)
        self.assertEqual(calls, ["idle"])
        self.assertEqual(out.getvalue(), "")

    def test_read_after_quiet_reads_nothing(self):
        self.allow("u2sock", return_value="100")
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            pc.read_after(quiet=True)
        self.assertEqual(out.getvalue(), "")

    def test_read_after_never_fails_the_action(self):
        self.allow("u2sock", return_value=None)
        self.allow("ui_dump", side_effect=RuntimeError("uiautomator dump kept coming back empty"))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            pc.read_after()
        self.assertIn("couldn't read the screen afterwards", err.getvalue())

    def test_tap_prints_the_screen_it_ends_on(self):
        self.allow("wake_async", return_value=mock.Mock())
        self.allow("ui_dump", return_value=ET.fromstring(TAP_XML))
        self.allow("tap_center")
        self.allow("u2sock", return_value=SAMPLE_XML)
        args = self.parse(["tap", "Not now"])
        with self.cap() as (out, err):
            rc = pc.cmd_tap(args)
        self.assertEqual(rc, 0)
        self.assertIn("tapped Not now", out.getvalue())
        self.assertIn("screen: com.example", out.getvalue())
        self.assertIn("OK (click) [Button]", out.getvalue())

    def test_tap_quiet_skips_the_read(self):
        # with an old helper (no label tap): one read to find the row, the
        # tap, the quiet wait, and no screen printed
        self.allow("wake_async", return_value=mock.Mock())
        dump = self.allow("ui_dump", return_value=ET.fromstring(TAP_XML))
        self.allow("tap_center")

        def u2(cmd, arg="", timeout=30):
            if cmd == "act":
                pc._u2_status = "err unknown command: act"
                return None
            return "100"  # quiet: the wait, no read
        self.allow("u2sock", side_effect=u2)
        args = self.parse(["tap", "--quiet", "Not now"])
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_tap(args)
        self.assertEqual(rc, 0)
        self.assertEqual(dump.call_count, 1)
        self.assertNotIn("screen:", out.getvalue())

    def test_do_reads_only_after_the_last_step(self):
        seen = []

        def fake(ns):
            seen.append(getattr(ns, "quiet", None))
            return 0
        ap = pc.build_parser()
        with mock.patch.object(pc, "build_parser", return_value=ap):
            for name in ("press", "tap", "start"):
                ap._subparsers._group_actions[0].choices[name].set_defaults(fn=fake)
            with self.cap():
                rc = pc.cmd_do(SimpleNamespace(flow="press BACK; tap X; start com.a"))
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [True, True, False])

    def test_log_line_masks_typed_text(self):
        line = pc.log_line(["type", "secret words", "--field", "Search"], 1234.5, 0)
        self.assertNotIn("secret", line)
        self.assertIn("burner type '…' --field Search", line)
        self.assertIn("1234ms exit 0", line)
        self.assertIn("tap 'Not now'", pc.log_line(["tap", "Not now"], 10, 1))


# --------------------------------- 13. review fixes: typing, log, branch

class TypingSafetyTests(OfflineTestCase):
    def _args(self, **kw):
        base = dict(text="hello", field=None, clear=False, clear_keys=20,
                    unicode=False, ascii=False, slow=False, quiet=True)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_lost_set_text_is_not_typed_again(self):
        self.allow("u2_invalidate")
        self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        sc = self.allow("scrcpy_send", return_value=True)

        def u2(cmd, arg="", timeout=30):
            pc._u2_status = "lost"
            return None
        self.allow("u2sock", side_effect=u2)
        with self.cap() as (out, err):
            rc = pc.cmd_type(self._args())
        self.assertEqual(rc, 1)
        sc.assert_not_called()
        self.assertIn("didn't confirm the typing", err.getvalue())

    def test_no_focused_field_falls_back_to_keys_and_says_so(self):
        self.allow("u2_invalidate")
        sc = self.allow("scrcpy_send", return_value=True)

        def u2(cmd, arg="", timeout=30):
            pc._u2_status = "err no editable field found (no field has the focus)"
            return None
        self.allow("u2sock", side_effect=u2)
        with self.cap() as (out, err):
            rc = pc.cmd_type(self._args())
        self.assertEqual(rc, 0)
        self.assertEqual([c[0][0] for c in sc.call_args_list], ["text hello"])
        self.assertIn("no text field had the focus", out.getvalue())
        self.assertIn("added to the field", out.getvalue())

    def test_lost_field_tap_is_not_tapped_again(self):
        tc = self.allow("tap_center")

        def u2(cmd, arg="", timeout=30):
            pc._u2_status = "lost"
            return None
        self.allow("u2sock", side_effect=u2)
        self.assertEqual(pc.focus_field("Search"), "unsure")
        tc.assert_not_called()

    def test_missing_field(self):
        def u2(cmd, arg="", timeout=30):
            if cmd == "act":
                pc._u2_status = "err act not sent: not on the page"
                return None
            return pc.U2_NOT_FOUND
        self.allow("u2sock", side_effect=u2)
        self.assertEqual(pc.focus_field("Search"), "missing")

    def test_field_focused_by_the_helper_through_its_label(self):
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append((cmd, json.loads(arg)))
            return SAMPLE_XML
        self.allow("u2sock", side_effect=u2)
        self.assertEqual(pc.focus_field("Date picker"), "done")
        self.assertEqual(calls, [("act", {"tap_label": "Date picker", "idle": 1200})])

    def test_type_keys_resumes_with_adb_after_scrcpy_stops(self):
        adb = self.allow("adb_or_ensure", return_value=SimpleNamespace(
            returncode=0, stdout="", stderr=""))
        sent = []

        def sc(line):
            sent.append(line)
            if line == "key 66":
                raise RuntimeError("gone")
            return True
        self.allow("scrcpy_send", side_effect=sc)
        with self.cap():
            pc.type_keys("abc\ndef")
        self.assertEqual(sent, ["text abc", "key 66"])
        self.assertEqual([c[0] for c in adb.call_args_list],
                         [("shell", "input", "keyevent", "66"),
                          ("shell", "input", "text", "def")])

    def test_type_keys_spaces_at_the_ends_go_as_keys(self):
        sc = self.allow("scrcpy_send", return_value=True)
        pc.type_keys(" hi  ")
        self.assertEqual([c[0][0] for c in sc.call_args_list],
                         ["key 62", "text hi", "key 62", "key 62"])


class CommandLogTests(OfflineTestCase):
    def test_type_arguments_masked_in_any_order(self):
        line = pc.log_line(["type", "--field", "Password", "hunter2"], 10, 0)
        self.assertNotIn("hunter2", line)
        self.assertIn("type --field Password '…'", line)
        line = pc.log_line(["type", "--clear", "s3cret"], 10, 0)
        self.assertNotIn("s3cret", line)
        self.assertIn("type --clear '…'", line)
        line = pc.log_line(["type", "--", "--starts-with-dashes"], 10, 0)
        self.assertNotIn("starts-with", line)

    def test_long_labels_still_deduplicated(self):
        long = "x" * 200
        root = ET.fromstring(SCREEN_XML.replace('content-desc="Open"', 'content-desc="%s"' % long)
                             .replace('text="Open"', 'text="%s"' % long))
        pc._update_screen_from_dump(root)
        lines, _ = pc.screen_lines(pc.walk(root), 1080, 2400)
        self.assertEqual(len([l for l in lines if "xxxx" in l]), 1)

    def test_do_flow_and_pairing_code_masked(self):
        line = pc.log_line(["do", "tap Password; type hunter2; press ENTER"], 10, 0)
        self.assertNotIn("hunter2", line)
        self.assertIn("type …", line)
        self.assertIn("press ENTER", line)
        line = pc.log_line(["setup", "--step", "pair", "--code", "123456", "--ip", "100.1.2.3"], 10, 0)
        self.assertNotIn("123456", line)
        self.assertIn("--ip 100.1.2.3", line)


class BranchInstallTests(OfflineTestCase):
    def test_install_ref_reads_the_marker(self):
        d = tempfile.mkdtemp()
        with mock.patch.object(pc, "ROOT", d):
            self.assertEqual(pc.install_ref(), "master")
            with open(os.path.join(d, ".burner-ref"), "w") as f:
                f.write("fast\n")
            self.assertEqual(pc.install_ref(), "fast")
            with open(os.path.join(d, ".burner-ref"), "w") as f:
                f.write("bad ref;rm\n")
            self.assertEqual(pc.install_ref(), "master")

    def test_skill_path_follows_the_branch(self):
        d = tempfile.mkdtemp()
        with mock.patch.object(pc, "ROOT", d):
            with open(os.path.join(d, ".burner-ref"), "w") as f:
                f.write("fast")
            self.assertTrue(pc.skill_path().endswith("SKILL.md"))
            with open(os.path.join(d, "SKILL-fast.md"), "w") as f:
                f.write("# fast")
            self.assertTrue(pc.skill_path().endswith("SKILL-fast.md"))

    def test_version_names_the_branch(self):
        with mock.patch.object(pc, "build_commit", return_value="abcdef1234"), \
                mock.patch.object(pc, "install_ref", return_value="fast"):
            with self.cap() as (out, err):
                pc.cmd_version(self.parse(["version"]))
            self.assertIn("burner abcdef1 on branch fast", out.getvalue())
            with self.cap() as (out, err):
                pc.cmd_version(self.parse(["version", "--json"]))
            self.assertEqual(json.loads(out.getvalue())["ref"], "fast")

    def test_update_waits_for_the_helper(self):
        run = self.allow("subprocess")
        run.run.return_value = SimpleNamespace(returncode=0)
        with mock.patch.object(pc, "install_ref", return_value="fast"), \
                mock.patch.object(pc, "wait_for_helper", return_value=True) as wait, \
                mock.patch.object(pc, "u2_start_background"), \
                mock.patch.object(pc, "_stop_helper_daemons"):
            with self.cap() as (out, err):
                rc = pc.cmd_update(self.parse(["update"]))
        self.assertEqual(rc, 0)
        wait.assert_called_once_with(30)
        self.assertIn("helpers restarted", out.getvalue())
        self.assertIn("Re-read", out.getvalue())

    def test_start_opens_the_first_screen(self):
        self.allow("scrcpy_send", side_effect=RuntimeError("no scrcpy"))
        adb = self.allow("adb_or_ensure", return_value=SimpleNamespace(
            stdout="  mFocusedApp=ActivityRecord{1 u0 com.example/.Main t3}\n",
            stderr="", returncode=0))
        self.allow("u2_invalidate")
        self.allow("nav_record")
        self.allow("u2sock", return_value="100")
        with mock.patch.object(pc.time, "sleep"), self.cap():
            pc.cmd_start(SimpleNamespace(package="com.example", quiet=True))
        self.assertIn("-f 0x10008000", adb.call_args[0][1])  # NEW_TASK|CLEAR_TASK

    def test_update_fails_loudly_on_a_bad_download(self):
        run = self.allow("subprocess")
        run.run.return_value = SimpleNamespace(returncode=22)
        with mock.patch.object(pc, "install_ref", return_value="fast"), \
                mock.patch.object(pc, "wait_for_helper", return_value=True), \
                mock.patch.object(pc, "u2_start_background"), \
                mock.patch.object(pc, "_stop_helper_daemons") as stop:
            with self.cap() as (out, err):
                rc = pc.cmd_update(self.parse(["update"]))
        self.assertEqual(rc, 22)
        stop.assert_not_called()
        self.assertIn("update failed", err.getvalue())
        cmd = run.run.call_args[0][0]
        self.assertIn("pipefail", cmd[2])
        self.assertIn("fast/docs/install.sh", cmd[-1])


SYSTEM_DIALOG_XML = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" package="com.android.systemui" bounds="[0,0][1080,120]" clickable="false" enabled="true" focused="false" checked="false">
    <node text="4:05" class="android.widget.TextView" package="com.android.systemui" bounds="[40,20][120,100]" clickable="false" enabled="true" focused="false" checked="false"/>
  </node>
  <node text="Hello" class="android.widget.TextView" package="com.example" bounds="[100,300][500,400]" clickable="false" enabled="true" focused="false" checked="false"/>
  <node text="" class="android.widget.FrameLayout" package="com.android.systemui" bounds="[100,900][980,1500]" clickable="false" enabled="true" focused="false" checked="false">
    <node text="Start recording or casting?" class="android.widget.TextView" package="com.android.systemui" bounds="[150,950][930,1050]" clickable="false" enabled="true" focused="false" checked="false"/>
    <node text="Start now" class="android.widget.Button" package="com.android.systemui" bounds="[600,1300][930,1400]" clickable="true" enabled="true" focused="false" checked="false"/>
  </node>
  <node text="" content-desc="Back" class="android.widget.ImageView" package="com.android.systemui" bounds="[100,2300][200,2380]" clickable="true" enabled="true" focused="false" checked="false"/>
</hierarchy>"""


class SystemDialogRowsTests(OfflineTestCase):
    def test_system_dialog_rows_stay_bars_go(self):
        root = ET.fromstring(SYSTEM_DIALOG_XML)
        pc._update_screen_from_dump(root)
        lines, _ = pc.screen_lines(pc.walk(root), 1080, 2400)
        text = "\n".join(lines)
        self.assertIn("Start recording or casting?", text)
        self.assertIn("Start now (click) [Button]", text)
        self.assertNotIn("4:05", text)
        self.assertNotIn("[Back]", text)

    def test_system_ui_only_screen_says_so(self):
        xml = SYSTEM_DIALOG_XML.replace('package="com.example"', 'package="com.android.systemui"')
        with self.cap() as (out, err):
            pc.print_screen(ET.fromstring(xml))
        self.assertIn("screen: system UI", out.getvalue())

    def test_unchanged_screen_is_reported(self):
        same = ET.fromstring(SAMPLE_XML)
        self.allow("u2sock", return_value="100")
        dump = self.allow("ui_dump", return_value=same)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            pc.read_after(prev_root=ET.fromstring(SAMPLE_XML))
        self.assertEqual(dump.call_count, 3)
        self.assertIn("screen: com.example (unchanged)", out.getvalue())

    def test_dismiss_reports_what_closed_even_if_the_read_fails(self):
        self.allow("tap_center")
        self.allow("u2sock", return_value=None)
        self.allow("ui_dump", side_effect=[ET.fromstring(TAP_XML), RuntimeError("gone")])
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_dismiss(self.parse(["dismiss"]))
        self.assertEqual(rc, 0)
        self.assertIn("closed: Not now", out.getvalue())
        self.assertIn("couldn't read the screen afterwards", err.getvalue())


# --------------------------------- 14. a phone that can't be reached

class UnreachableTests(OfflineTestCase):
    def test_parse_proxy_probe(self):
        self.assertEqual(pc.parse_proxy_probe("HTTP/1.1 200 Connection established"), "open")
        self.assertEqual(pc.parse_proxy_probe("HTTP/1.1 503 Service Unavailable"), "notopen")
        self.assertEqual(pc.parse_proxy_probe(""), "notopen")

    def test_describe_unreachable(self):
        self.assertIn("Wireless debugging is off", pc.describe_unreachable("closed"))
        self.assertIn("tailnet", pc.describe_unreachable("unreachable"))
        self.assertIn("pair again", pc.describe_unreachable("open"))
        self.assertIn("off the tailnet", pc.describe_unreachable("notopen"))
        self.assertIn("PHONE_TAILSCALE_IP", pc.describe_unreachable("unknown"))
        self.assertIn("PHONE_TAILSCALE_IP", pc.describe_unreachable("???"))

    def test_probe_without_an_address(self):
        with mock.patch.dict(pc.CFG, {"PHONE_TAILSCALE_IP": "YOUR_PHONE_TAILSCALE_IP"}):
            self.assertEqual(pc.probe_phone_port(), "unknown")

    def test_probe_closed_port_direct(self):
        import socket as _socket
        self._guards["socket.socket"].stop()
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "", "https_proxy": ""}), \
                mock.patch.object(_socket, "create_connection",
                                  side_effect=ConnectionRefusedError()):
            self.assertEqual(pc.probe_phone_port("100.64.0.9", "5555"), "closed")
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "", "https_proxy": ""}), \
                mock.patch.object(_socket, "create_connection",
                                  side_effect=TimeoutError()):
            self.assertEqual(pc.probe_phone_port("100.64.0.9", "5555"), "unreachable")

    def test_probe_through_the_proxy(self):
        import socket as _socket
        self._guards["socket.socket"].stop()
        fake = mock.Mock()
        fake.recv.return_value = b"HTTP/1.1 200 Connection established\r\n\r\n"
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://user:pw@proxy.example:3128"}), \
                mock.patch.object(_socket, "create_connection", return_value=fake) as cc:
            self.assertEqual(pc.probe_phone_port("100.64.0.9", "5555"), "open")
        cc.assert_called_once_with(("proxy.example", 3130), 4)
        sent = fake.sendall.call_args[0][0].decode()
        self.assertIn("CONNECT 100.64.0.9:5555 HTTP/1.1", sent)
        self.assertIn("Proxy-Authorization: Basic dXNlcjpwdw==", sent)

    def test_ensure_says_why_and_stops_early(self):
        self._guards["ensure"].stop()
        self.allow("subprocess")
        adb = self.allow("adb", return_value=SimpleNamespace(returncode=1, stdout="", stderr=""))
        sc = self.allow("scrcpy_send")
        # the try fails and leaves its reason, as the real one does
        again = self.allow("reenable_debugging", side_effect=lambda *a, **k: setattr(
            pc, "_phone_api_why", "port 9093 refused (the app isn't running on the phone)"))
        pc._phone_api_why = "stale reason from an earlier call"  # reset by ensure()
        with mock.patch.object(pc, "probe_phone_port", return_value="closed"), \
                mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            ok = pc.ensure()
        self.assertFalse(ok)
        again.assert_called_once_with("closed", wait=90, force=True)  # explicit: long wait, no skip
        self.assertIn("phone not reachable (state: none): the phone is online, but "
                      "Wireless debugging is off", err.getvalue())
        self.assertIn("(the phone's adb-auto-enable app: port 9093 refused", err.getvalue())
        self.assertNotIn("stale reason", err.getvalue())
        # behind the proxy the port looks "notopen": asked just the same;
        # a failure inside the try is printed, not fatal
        again = self.allow("reenable_debugging", side_effect=RuntimeError("boom"))
        with mock.patch.object(pc, "probe_phone_port", return_value="notopen"), \
                mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            self.assertFalse(pc.ensure(heal_fast_paths=False))
        again.assert_called_once_with("notopen", wait=20, force=False)  # implicit: short wait
        self.assertIn("turning Wireless debugging back on failed: boom", err.getvalue())
        self.assertIn("off the tailnet", err.getvalue())
        # no adb restart and no helper heal against a phone that isn't there
        self.assertEqual([c[0][0] for c in adb.call_args_list], ["connect", "get-state"] * 2)
        sc.assert_not_called()
        # a phone that is off the tailnet gets no request
        again.reset_mock()
        with mock.patch.object(pc, "probe_phone_port", return_value="unreachable"), \
                mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            self.assertFalse(pc.ensure())
        again.assert_not_called()

    def test_ensure_turns_wireless_debugging_back_on(self):
        self._guards["ensure"].stop()
        self.allow("subprocess")
        states = iter(["", "device"])
        adb = self.allow("adb", side_effect=lambda *a, **k: SimpleNamespace(
            returncode=0, stdout=next(states) if a[0] == "get-state" else "", stderr=""))
        again = self.allow("reenable_debugging", return_value=True)
        with mock.patch.object(pc, "probe_phone_port", return_value="closed"), \
                mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            ok = pc.ensure(heal_fast_paths=False)
        self.assertTrue(ok)
        again.assert_called_once_with("closed", wait=20, force=False)
        self.assertIn("kill-server", [c[0][0] for c in adb.call_args_list])

    def test_ensure_carries_on_when_the_port_opens_by_itself(self):
        # the try "failed" (rate-limited, say) but a fresh probe finds the
        # port open: ensure restarts adb and goes on
        self._guards["ensure"].stop()
        self.allow("subprocess")
        states = iter(["", "device"])
        adb = self.allow("adb", side_effect=lambda *a, **k: SimpleNamespace(
            returncode=0, stdout=next(states) if a[0] == "get-state" else "", stderr=""))
        self.allow("reenable_debugging", return_value=False)
        probes = iter(["closed", "open"])
        with mock.patch.object(pc, "probe_phone_port", side_effect=lambda: next(probes)), \
                mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            self.assertTrue(pc.ensure(heal_fast_paths=False))
        self.assertIn("kill-server", [c[0][0] for c in adb.call_args_list])

    def _stamp_dir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(pc, "REENABLE_STAMP", os.path.join(tmp.name, "reenable.json"))
        p.start()
        self.addCleanup(p.stop)
        return pc.REENABLE_STAMP

    def test_reenable_debugging_asks_the_app_and_waits_for_the_port(self):
        self._guards["reenable_debugging"].stop()
        stamp = self._stamp_dir()
        api = self.allow("phone_api", side_effect=lambda path, timeout=6:
                         {"success": True, "message": "Boot test started"}
                         if path == "/api/test" else {"adb5555Available": False})
        probes = iter(["closed", "closed", "open"])
        with mock.patch.object(pc, "probe_phone_port", side_effect=lambda: next(probes)), \
                mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            self.assertTrue(pc.reenable_debugging())
        self.assertEqual([c[0][0] for c in api.call_args_list], ["/api/status", "/api/test"])
        self.assertIn("back on", err.getvalue())
        self.assertFalse(os.path.exists(stamp))  # a success leaves no stamp

    def test_reenable_debugging_once_per_two_minutes(self):
        self._guards["reenable_debugging"].stop()
        self._stamp_dir()
        api = self.allow("phone_api", return_value={"success": True})
        with mock.patch.object(pc, "probe_phone_port", return_value="closed"), \
                mock.patch.object(pc.time, "sleep"), \
                mock.patch.object(pc.time, "monotonic", side_effect=iter(range(0, 400, 10))), \
                self.cap() as (out, err):
            self.assertFalse(pc.reenable_debugging(wait=20))
        self.assertIn("didn't come back within 20s; the app's routine takes about a minute",
                      err.getvalue())
        self.assertEqual(pc.recent_reenable()[1], "the port didn't come back within 20s")
        # the next implicit try (another command, another process) skips
        api.reset_mock()
        with mock.patch.object(pc, "probe_phone_port") as probe, self.cap() as (out, err):
            self.assertFalse(pc.reenable_debugging(wait=20))
        api.assert_not_called()
        probe.assert_not_called()
        self.assertIn("tried to turn it back on 0s ago (the port didn't come back within 20s)",
                      err.getvalue())
        # an explicit `burner ensure` tries anyway
        probes = iter(["open"])
        with mock.patch.object(pc, "probe_phone_port", side_effect=lambda: next(probes)), \
                mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            self.assertTrue(pc.reenable_debugging(force=True))
        self.assertIsNone(pc.recent_reenable())

    def test_reenable_debugging_failures_say_why(self):
        self._guards["reenable_debugging"].stop()
        self._stamp_dir()
        # the app isn't answering: no request, no wait, the reason shown
        def unanswered(path, timeout=6):
            pc._phone_api_why = "port 9093 refused (the app isn't running on the phone)"
            return None
        api = self.allow("phone_api", side_effect=unanswered)
        with mock.patch.object(pc, "probe_phone_port") as probe, self.cap() as (out, err):
            self.assertFalse(pc.reenable_debugging())
        self.assertEqual([c[0][0] for c in api.call_args_list], ["/api/status"])
        probe.assert_not_called()
        self.assertIn("isn't answering (port 9093 refused", err.getvalue())
        self.assertIn("the app wasn't answering", pc.recent_reenable()[1])
        pc.note_reenable(None)
        # the app answers but refuses: its error shown, no wait
        self.allow("phone_api", side_effect=lambda path, timeout=6:
                   {"success": False, "error": "not paired"} if path == "/api/test" else {})
        with mock.patch.object(pc, "probe_phone_port") as probe, self.cap() as (out, err):
            self.assertFalse(pc.reenable_debugging())
        self.assertIn("didn't take the request (not paired)", err.getvalue())
        probe.assert_not_called()
        pc.note_reenable(None)
        # no success flag at all counts as refused
        self.allow("phone_api", side_effect=lambda path, timeout=6:
                   {"error": "busy"} if path == "/api/test" else {})
        with mock.patch.object(pc, "probe_phone_port"), self.cap() as (out, err):
            self.assertFalse(pc.reenable_debugging())
        self.assertIn("didn't take the request (busy)", err.getvalue())
        pc.note_reenable(None)
        # /api/test refused outright (not a lost answer): no wait
        def refused(path, timeout=6):
            if path == "/api/test":
                pc._phone_api_why = "the app answered 500 Error: adb error"
                return None
            return {}
        self.allow("phone_api", side_effect=refused)
        with mock.patch.object(pc, "probe_phone_port") as probe, self.cap() as (out, err):
            self.assertFalse(pc.reenable_debugging())
        self.assertIn("didn't take the request (the app answered 500 Error: adb error)",
                      err.getvalue())
        probe.assert_not_called()
        pc.note_reenable(None)
        # a status page that isn't JSON still means the app is alive
        calls = []

        def unescaped(path, timeout=6):
            calls.append(path)
            if path == "/api/status":
                pc._phone_api_why = "the app's answer wasn't JSON"
                return None
            return {"success": True}
        self.allow("phone_api", side_effect=unescaped)
        probes = iter(["open"])
        with mock.patch.object(pc, "probe_phone_port", side_effect=lambda: next(probes)), \
                mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            self.assertTrue(pc.reenable_debugging())
        self.assertEqual(calls, ["/api/status", "/api/test"])
        # the app says the port is open: its routine isn't run, the port is probed again
        api = self.allow("phone_api", return_value={"adb5555Available": True})
        with mock.patch.object(pc, "probe_phone_port", return_value="open"), self.cap() as (out, err):
            self.assertTrue(pc.reenable_debugging())
        self.assertEqual([c[0][0] for c in api.call_args_list], ["/api/status"])
        # ... and when the probe still fails, the advice says where to look
        with mock.patch.object(pc, "probe_phone_port", return_value="closed"), self.cap() as (out, err):
            self.assertFalse(pc.reenable_debugging("notopen"))
        self.assertIn("check Tailscale on the phone", pc._phone_api_why)
        # behind the proxy the wording claims no more than burner knows
        def unanswered2(path, timeout=6):
            pc._phone_api_why = "the proxy refused the connection"
            return None
        self.allow("phone_api", side_effect=unanswered2)
        with mock.patch.object(pc, "probe_phone_port"), self.cap() as (out, err):
            self.assertFalse(pc.reenable_debugging("notopen", force=True))
        self.assertIn("the phone's debugging port isn't answering, and the phone's "
                      "adb-auto-enable app isn't answering (the proxy refused the connection)",
                      err.getvalue())
        pc.note_reenable(None)
        # the request's answer was lost: the port is still waited for
        def lost(path, timeout=6):
            if path == "/api/test":
                pc._phone_api_why = "no answer on port 9093 within 6s"
                return None
            return {}
        self.allow("phone_api", side_effect=lost)
        probes = iter(["open"])
        with mock.patch.object(pc, "probe_phone_port", side_effect=lambda: next(probes)), \
                mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            self.assertTrue(pc.reenable_debugging())
        self.assertIn("answer was lost; waiting for the port anyway", err.getvalue())

    def test_parse_json_body(self):
        self.assertEqual(pc.parse_json_body(b'{"a": 1}'), {"a": 1})
        self.assertIsNone(pc.parse_json_body(b"not json"))
        self.assertIsNone(pc.parse_json_body(b"[1]"))
        self.assertIsNone(pc.parse_json_body(b""))
        self.assertIsNone(pc.parse_json_body(None))
        self.assertIsNone(pc.parse_json_body(b"[" * 100000))  # no traceback for nonsense

    def test_phone_api_over_http(self):
        import http.client
        self._guards["phone_api"].stop()
        made = []

        class FakeConn:
            status, reason, body = 200, "OK", b'{"success": true}'
            fail = None

            def __init__(self, host, port, timeout=None):
                self.host, self.port, self.timeout, self.tunnel = host, port, timeout, None
                self.closed = False
                made.append(self)

            def set_tunnel(self, host, port, headers=None):
                self.tunnel = (host, port, headers)

            def request(self, method, path, headers=None):
                self.req = (method, path, headers)
                if FakeConn.fail:
                    raise FakeConn.fail

            def getresponse(self):
                return SimpleNamespace(status=FakeConn.status, reason=FakeConn.reason,
                                       read=lambda n=-1: FakeConn.body)

            def close(self):
                self.closed = True
        env = {"HTTPS_PROXY": "", "https_proxy": ""}
        with mock.patch.dict(pc.CFG, {"PHONE_TAILSCALE_IP": "100.64.0.9"}), \
                mock.patch.dict(os.environ, env), \
                mock.patch.object(http.client, "HTTPConnection", FakeConn):
            self.assertEqual(pc.phone_api("/api/test"), {"success": True})
            self.assertEqual((made[-1].host, made[-1].port, made[-1].timeout), ("100.64.0.9", 9093, 6))
            self.assertIsNone(made[-1].tunnel)
            self.assertEqual(made[-1].req, ("GET", "/api/test", {"Connection": "close"}))
            self.assertTrue(made[-1].closed)
            # the app's own error in a 500 is kept
            FakeConn.status, FakeConn.reason, FakeConn.body = 500, "Error", b'{"error": "not paired"}'
            self.assertIsNone(pc.phone_api("/api/test"))
            self.assertEqual(pc._phone_api_why, "the app answered 500 Error: not paired")
            FakeConn.status, FakeConn.reason, FakeConn.body = 200, "OK", b"<html>"
            self.assertIsNone(pc.phone_api("/api/status"))
            self.assertEqual(pc._phone_api_why, "the app's answer wasn't JSON")
            # refused, timed out, and the proxy's refusal each say so
            for fail, why in ((ConnectionRefusedError(), "port 9093 refused (the app isn't running"),
                              (TimeoutError(), "no answer on port 9093 within 6s"),
                              (OSError("Tunnel connection failed: 403 Forbidden"),
                               "port 9093: Tunnel connection failed: 403 Forbidden")):
                FakeConn.fail = fail
                self.assertIsNone(pc.phone_api("/api/status"))
                self.assertTrue(pc._phone_api_why.startswith(why), pc._phone_api_why)
                self.assertTrue(made[-1].closed)
            FakeConn.fail = None
        # through the proxy: a CONNECT tunnel with the proxy's credentials
        with mock.patch.dict(pc.CFG, {"PHONE_TAILSCALE_IP": "100.64.0.9"}), \
                mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://user:pw@proxy.example:3128"}), \
                mock.patch.object(http.client, "HTTPConnection", FakeConn):
            FakeConn.status, FakeConn.reason, FakeConn.body = 200, "OK", b"{}"
            self.assertEqual(pc.phone_api("/api/status"), {})
            FakeConn.fail = ConnectionRefusedError()  # the proxy itself: not "the app"
            self.assertIsNone(pc.phone_api("/api/status"))
            self.assertEqual(pc._phone_api_why, "the proxy refused the connection")
            FakeConn.fail = None
        self.assertEqual((made[-2].host, made[-2].port), ("proxy.example", 3130))
        self.assertEqual(made[-2].tunnel, ("100.64.0.9", 9093,
                                           {"Proxy-Authorization": "Basic dXNlcjpwdw=="}))

    def test_probe_through_the_proxy_says_notopen_when_it_refuses(self):
        import socket as _socket
        self._guards["socket.socket"].stop()
        fake = mock.Mock()
        fake.recv.return_value = b"HTTP/1.1 503 Service Unavailable\r\n\r\n"
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://user:pw@proxy.example:3128"}), \
                mock.patch.object(_socket, "create_connection", return_value=fake):
            self.assertEqual(pc.probe_phone_port("100.64.0.9", "5555"), "notopen")
        fake.close.assert_called_once_with()

    def test_ensure_restarts_adb_when_the_port_answers(self):
        self._guards["ensure"].stop()
        self.allow("subprocess")
        states = iter(["", "device"])
        adb = self.allow("adb", side_effect=lambda *a, **k: SimpleNamespace(
            returncode=0, stdout=next(states) if a[0] == "get-state" else "", stderr=""))
        with mock.patch.object(pc, "probe_phone_port", return_value="open"), \
                mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            ok = pc.ensure(heal_fast_paths=False)
        self.assertTrue(ok)
        self.assertIn("kill-server", [c[0][0] for c in adb.call_args_list])


class WaitEitherTests(OfflineTestCase):
    def test_legacy_wait_accepts_either_label(self):
        self.allow("u2sock", return_value=None)  # no helper: legacy polling
        self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        args = self.parse(["wait", "Install || OK", "--timeout", "2", "--quiet", "--no-evidence"])
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_wait(args)
        self.assertEqual(rc, 0)
        self.assertIn("found: OK", out.getvalue())

    def test_helper_find_node_accepts_either_label(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "u2mux_under_test", os.path.join(ROOT, "lib", "u2", "u2mux.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        n = mod.find_node(SAMPLE_XML, "Install || OK")
        self.assertEqual(n["text"], "OK")
        self.assertIsNone(mod.find_node(SAMPLE_XML, "Install || Buy"))
        self.assertEqual(mod.find_node(SAMPLE_XML, "hello")["text"], "Hello")


class OpenLinkTests(OfflineTestCase):
    def test_open_quotes_the_link_and_package_for_the_phone_shell(self):
        adb = self.allow("adb_or_ensure", return_value=SimpleNamespace(
            returncode=0, stdout="", stderr=""))
        self.allow("u2_invalidate")
        args = self.parse(["open", "--quiet", "market://search?q=Vinted&c=apps",
                           "com.android.vending"])
        self.allow("u2sock", return_value="100")
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_open(args)
        self.assertEqual(rc, 0)
        script = adb.call_args[0][1]
        self.assertIn("-d 'market://search?q=Vinted&c=apps' 'com.android.vending'", script)
        self.assertIn("opened market://search?q=Vinted&c=apps", out.getvalue())


class KeyboardScrollTests(OfflineTestCase):
    def test_swipe_stays_above_an_open_keyboard(self):
        self.allow("u2sock", return_value="1080 2400 com.example kbd")
        self.assertEqual(pc.live_screen_dims(), (1080, 2400))
        self.assertEqual(pc._screen_pkg, "com.example")
        self.assertTrue(pc._screen_kbd)
        x1, y1, x2, y2 = pc._swipe_coords("down")
        self.assertLessEqual(max(y1, y2), 2400 * 0.5)  # never on the keys
        self.assertGreater(y1, y2)  # still a scroll down (finger moves up)
        self.allow("u2sock", return_value="1080 2400 com.example")
        pc.live_screen_dims()
        self.assertFalse(pc._screen_kbd)
        x1, y1, x2, y2 = pc._swipe_coords("down")
        self.assertEqual((y1, y2), (1920, 480))

    def test_helper_reports_a_keyboard_in_its_newest_read(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "u2mux_under_test", os.path.join(ROOT, "lib", "u2", "u2mux.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(mod.keyboard_in(SCREEN_XML))
        self.assertFalse(mod.keyboard_in(SAMPLE_XML))
        self.assertFalse(mod.keyboard_in(""))


AMAZON_XML = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" package="com.amazon.mShop.android.shopping" bounds="[0,0][1080,2400]" clickable="false" enabled="true" focused="false" checked="false">
    <node text="Your Orders" class="android.widget.TextView" package="com.amazon.mShop.android.shopping" bounds="[100,200][500,300]" clickable="false" enabled="true" focused="false" checked="false"/>
    <node text="" content-desc="Anker USB-C Cable, Order placed September 28, 2026, Delivered September 30. Buy again" class="android.view.View" package="com.amazon.mShop.android.shopping" bounds="[0,400][1080,700]" clickable="true" enabled="true" focused="false" checked="false"/>
    <node text="" content-desc="Anker USB-C Cable, Order placed September 28, 2026, Delivered September 30." class="android.view.View" package="com.amazon.mShop.android.shopping" bounds="[0,400][1080,700]" clickable="true" enabled="true" focused="false" checked="false"/>
  </node>
</hierarchy>"""


class AmazonStatusTests(OfflineTestCase):
    def test_reads_the_first_order_from_one_settled_read(self):
        adb = self.allow("adb_or_ensure", return_value=SimpleNamespace(
            returncode=0, stdout="", stderr=""))
        self.allow("u2_invalidate")
        calls = []
        self.allow("u2sock", side_effect=lambda cmd, arg="", timeout=30: calls.append(cmd) or "")
        self.allow("ui_dump", return_value=ET.fromstring(AMAZON_XML))
        swipe = mock.patch.object(pc, "_swipe_once").start()
        self.addCleanup(mock.patch.stopall)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_amazon_status(self.parse(["amazon-status"]))
        self.assertEqual(rc, 0)
        self.assertIn("Anker USB-C Cable", out.getvalue())
        self.assertIn("Delivered — ordered September 28, 2026", out.getvalue())
        self.assertIn("'https://www.amazon.com/gp/css/order-history' 'com.amazon.mShop.android.shopping'",
                      adb.call_args[0][1])
        self.assertEqual(calls[0], "wait_for")
        swipe.assert_not_called()

    def test_swipes_when_the_first_read_has_no_order(self):
        self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.allow("u2_invalidate")
        self.allow("u2sock", return_value="")
        self.allow("ui_dump", side_effect=[ET.fromstring(SAMPLE_XML), ET.fromstring(AMAZON_XML)])
        swipe = mock.patch.object(pc, "_swipe_once").start()
        self.addCleanup(mock.patch.stopall)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_amazon_status(self.parse(["amazon-status"]))
        self.assertEqual(rc, 0)
        swipe.assert_called_once_with("down")


class LaunchReadBudgetTests(OfflineTestCase):
    def test_open_rereads_a_thin_screen_once_only(self):
        self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.allow("u2_invalidate")
        calls = []
        EMPTY = '<hierarchy rotation="0"></hierarchy>'

        def u2(cmd, arg="", timeout=30):
            calls.append((cmd, arg))
            if cmd == "act" and "open" in arg:
                pc._u2_status = "err act not sent: not a page"  # no Chrome in front
                return None
            return EMPTY if cmd == "act" else "100"
        self.allow("u2sock", side_effect=u2)
        dump = self.allow("ui_dump", return_value=ET.fromstring(EMPTY))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_open(self.parse(["open", "https://example.com"]))
        self.assertEqual(rc, 0)
        self.assertEqual(calls[1], ("act", '{"idle": 1000}'))
        self.assertEqual(dump.call_count, 1)  # one re-read of a thin screen
        self.assertIn("(may still be loading)", out.getvalue())


# --------------------------------- 15. one round trip per action

def _u2mux():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "u2mux_under_test", os.path.join(ROOT, "lib", "u2", "u2mux.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class OneRoundTripTests(OfflineTestCase):
    def test_key_codes(self):
        self.assertEqual(pc.key_code("BACK"), 4)
        self.assertEqual(pc.key_code("keycode_home"), 3)
        self.assertEqual(pc.key_code("66"), 66)
        self.assertIsNone(pc.key_code("CTRL+A"))

    def test_act_and_read_returns_the_screen(self):
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append((cmd, json.loads(arg)))
            return SAMPLE_XML
        self.allow("u2sock", side_effect=u2)
        status, root, note = pc.act_and_read({"tap": [10, 20]})
        self.assertEqual(status, "ok")
        self.assertEqual(calls, [("act", {"tap": [10, 20], "idle": 1200})])
        self.assertEqual(pc.dump_package(root), "com.example")

    def test_act_and_read_statuses(self):
        def lost(cmd, arg="", timeout=30):
            pc._u2_status = "lost"
            return None
        self.allow("u2sock", side_effect=lost)
        self.assertEqual(pc.act_and_read({"key": 4})[0], "lost")

        def failed(cmd, arg="", timeout=30):
            pc._u2_status = "err act failed after sending: UiObjectNotFoundException"
            return None
        self.allow("u2sock", side_effect=failed)
        self.assertTrue(pc.act_and_read({"set_text": "x"})[0].startswith("failed UiObjectNotFound"))

        def unsent(cmd, arg="", timeout=30):
            pc._u2_status = "err unknown command: act"
            return None
        self.allow("u2sock", side_effect=unsent)
        self.assertEqual(pc.act_and_read({"tap": [1, 2]})[0], "unsent")

        def no_helper(cmd, arg="", timeout=30):
            pc._u2_status = "unsent"
            return None
        self.allow("u2sock", side_effect=no_helper)
        self.assertEqual(pc.act_and_read({"tap": [1, 2]})[0], "unsent")

        # any other error came back after the helper had the request: the
        # action may have happened, so it is "failed", never "unsent"
        def other(cmd, arg="", timeout=30):
            pc._u2_status = "err the phone's UI server couldn't be restarted: x"
            return None
        self.allow("u2sock", side_effect=other)
        status = pc.act_and_read({"tap": [1, 2]})[0]
        self.assertTrue(status.startswith("failed the phone's UI server"), status)

    def test_tap_is_one_round_trip(self):
        self.allow("wake_async", return_value=mock.Mock())
        self.allow("ui_dump", return_value=ET.fromstring(TAP_XML))
        tc = self.allow("tap_center")
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append(cmd)
            return SAMPLE_XML if cmd == "act" else "100"
        self.allow("u2sock", side_effect=u2)
        with self.cap() as (out, err):
            rc = pc.cmd_tap(self.parse(["tap", "Not now"]))
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["act"])
        tc.assert_not_called()
        self.assertIn("tapped Not now (label)", out.getvalue())
        self.assertIn("screen: com.example", out.getvalue())

    def test_quiet_tap_is_one_helper_op_without_a_read(self):
        # a step of `burner do`: the helper taps by label and waits; no
        # screen comes back and none is read through the CLI
        self.allow("wake_async", return_value=mock.Mock())
        self.allow("ui_dump", side_effect=AssertionError("a quiet label tap must not read through the CLI"))
        tc = self.allow("tap_center")
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append((cmd, json.loads(arg) if cmd == "act" else arg))
            return "ok" if cmd == "act" else "100"
        self.allow("u2sock", side_effect=u2)
        with mock.patch.object(pc.time, "sleep") as sl, self.cap() as (out, err):
            rc = pc.cmd_tap(self.parse(["tap", "1", "--quiet"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(calls, [("act", {"tap_label": "1", "quiet": True, "idle": 1200})])
        tc.assert_not_called()
        sl.assert_not_called()
        self.assertEqual(out.getvalue().strip(), "tapped 1 (label)")
        # an older helper sends the screen anyway: unread, the tap still counts
        calls.clear()

        def u2_old(cmd, arg="", timeout=30):
            calls.append(cmd)
            return SAMPLE_XML if cmd == "act" else "100"
        self.allow("u2sock", side_effect=u2_old)
        with self.cap() as (out, err):
            rc = pc.cmd_tap(self.parse(["tap", "1", "--quiet"]))
        self.assertEqual((rc, calls, out.getvalue().strip()), (0, ["act"], "tapped 1 (label)"))

    def test_tap_falls_back_to_two_steps_with_an_old_helper(self):
        self.allow("wake_async", return_value=mock.Mock())
        self.allow("ui_dump", side_effect=[ET.fromstring(TAP_XML), ET.fromstring(SAMPLE_XML)])
        tc = self.allow("tap_center")

        def u2(cmd, arg="", timeout=30):
            if cmd == "act":
                pc._u2_status = "err unknown command: act"
                return None
            return "100"
        self.allow("u2sock", side_effect=u2)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_tap(self.parse(["tap", "Not now"]))
        self.assertEqual(rc, 0)
        tc.assert_called_once_with(200, 300)
        self.assertIn("screen: com.example", out.getvalue())

    def test_tap_lost_reply_is_not_tapped_again(self):
        self.allow("wake_async", return_value=mock.Mock())
        self.allow("ui_dump", return_value=ET.fromstring(TAP_XML))
        tc = self.allow("tap_center")

        def u2(cmd, arg="", timeout=30):
            pc._u2_status = "lost"
            return None
        self.allow("u2sock", side_effect=u2)
        with self.cap() as (out, err):
            rc = pc.cmd_tap(self.parse(["tap", "Not now"]))
        self.assertEqual(rc, 1)
        tc.assert_not_called()
        self.assertIn("didn't confirm the tap", err.getvalue())

    def test_press_is_one_round_trip(self):
        self.allow("u2_invalidate")
        self.allow("nav_record")
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append((cmd, json.loads(arg)))
            return SAMPLE_XML
        self.allow("u2sock", side_effect=u2)
        sc = self.allow("scrcpy_send")
        with self.cap() as (out, err):
            rc = pc.cmd_press(self.parse(["press", "BACK"]))
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("act", {"key": 4, "idle": 1200})])
        sc.assert_not_called()
        self.assertIn("pressed BACK", out.getvalue())
        self.assertIn("screen: com.example", out.getvalue())

    def test_press_with_modifiers_keeps_the_old_path(self):
        self.allow("u2_invalidate")
        self.allow("nav_record")
        sc = self.allow("scrcpy_send", return_value=True)
        self.allow("u2sock", return_value="100")
        self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_press(self.parse(["press", "--ctrl", "A"]))
        self.assertEqual(rc, 0)
        self.assertEqual([c[0][0] for c in sc.call_args_list],
                         ["key KEYCODE_CTRL_LEFT", "key A", "key KEYCODE_CTRL_LEFT"])

    def test_type_is_one_round_trip(self):
        self.allow("u2_invalidate")
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append((cmd, json.loads(arg)))
            return SAMPLE_XML
        self.allow("u2sock", side_effect=u2)
        args = SimpleNamespace(text="hello", field=None, clear=False, clear_keys=20,
                               unicode=False, ascii=False, slow=False, quiet=False)
        with self.cap() as (out, err):
            rc = pc.cmd_type(args)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("act", {"set_text": "hello", "idle": 1200})])
        self.assertIn("typed 5 chars", out.getvalue())
        self.assertIn("screen: com.example", out.getvalue())

    def test_type_without_a_focused_field_uses_keys(self):
        self.allow("u2_invalidate")
        sc = self.allow("scrcpy_send", return_value=True)

        def u2(cmd, arg="", timeout=30):
            if cmd == "act":
                pc._u2_status = "err act failed after sending: UiObjectNotFoundException"
            else:
                pc._u2_status = "err no editable field found (no field has the focus)"
            return None
        self.allow("u2sock", side_effect=u2)
        self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        args = SimpleNamespace(text="hello", field=None, clear=False, clear_keys=20,
                               unicode=False, ascii=False, slow=False, quiet=False)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_type(args)
        self.assertEqual(rc, 0)
        self.assertEqual([c[0][0] for c in sc.call_args_list], ["text hello"])
        self.assertIn("no text field had the focus", out.getvalue())

    def test_launch_read_waits_for_the_window(self):
        self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.allow("u2_invalidate")
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append((cmd, json.loads(arg)))
            if "open" in calls[-1][1]:
                pc._u2_status = "err act not sent: not a page"  # no Chrome in front
                return None
            return SAMPLE_XML
        self.allow("u2sock", side_effect=u2)
        with self.cap() as (out, err):
            rc = pc.cmd_open(self.parse(["open", "https://example.com"]))
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("act", {"open": "https://example.com", "idle": 1000}),
                                 ("act", {"idle": 1000})])
        self.assertIn("screen: com.example", out.getvalue())

    def test_helper_batch_results_and_act_calls(self):
        mod = _u2mux()
        res = mod.batch_results([{"id": 2, "result": "<x/>"}, {"id": 1, "error": {"code": -1}}], 2)
        self.assertIsInstance(res[0], Exception)
        self.assertEqual(res[1], "<x/>")
        with self.assertRaises(RuntimeError):
            mod.batch_results({"result": 1}, 1)
        calls = mod.act_calls({"tap": [10, 20], "idle": 1500})
        self.assertEqual([c[0] for c in calls],
                         ["wakeUp", "click", "dumpWindowHierarchy", "waitForIdle",
                          "dumpWindowHierarchy"])
        self.assertEqual(calls[1][1], [10, 20])
        self.assertEqual(calls[3][1], [1500])
        calls = mod.act_calls({"key": 4})
        self.assertEqual([c[0] for c in calls],
                         ["wakeUp", "pressKeyCode", "dumpWindowHierarchy", "waitForIdle",
                          "dumpWindowHierarchy"])
        calls = mod.act_calls({})
        self.assertEqual([c[0] for c in calls], ["waitForIdle", "dumpWindowHierarchy"])
        self.assertFalse(any(c[0] == "waitForWindowUpdate" for c in mod.act_calls({"tap": [1, 1]})))


# --------------------------------- 16. primitives in one round trip

class LabelTapTests(OfflineTestCase):
    def test_tap_by_label_is_one_round_trip_with_no_read_before(self):
        self.allow("wake_async", return_value=mock.Mock())
        dump = self.allow("ui_dump")
        tc = self.allow("tap_center")
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append((cmd, json.loads(arg)))
            return SAMPLE_XML
        self.allow("u2sock", side_effect=u2)
        self.allow("nav_record")
        with self.cap() as (out, err):
            rc = pc.cmd_tap(self.parse(["tap", "OK"]))
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("act", {"tap_label": "OK", "idle": 1200})])
        dump.assert_not_called()
        tc.assert_not_called()
        self.assertIn("tapped OK (label)", out.getvalue())
        self.assertIn("screen: com.example", out.getvalue())

    def test_tap_reads_first_when_the_label_is_not_on_the_last_read(self):
        self.allow("wake_async", return_value=mock.Mock())
        self.allow("ui_dump", return_value=ET.fromstring(TAP_XML))
        self.allow("tap_center")
        self.allow("nav_record")
        calls = []

        def u2(cmd, arg="", timeout=30):
            spec = json.loads(arg)
            calls.append(spec)
            if "tap_label" in spec:
                pc._u2_status = "err act not sent: not on the last read"
                return None
            return SAMPLE_XML
        self.allow("u2sock", side_effect=u2)
        with self.cap() as (out, err):
            rc = pc.cmd_tap(self.parse(["tap", "Not now"]))
        self.assertEqual(rc, 0)
        self.assertEqual([("tap_label" in c, "tap" in c) for c in calls], [(True, False), (False, True)])
        self.assertIn("tapped Not now @ (200, 300)", out.getvalue())

    def test_index_fuzzy_and_handles_read_first(self):
        self.allow("wake_async", return_value=mock.Mock())
        self.allow("ui_dump", return_value=ET.fromstring(AMBI_XML))
        self.allow("tap_center")
        self.allow("nav_record")
        calls = []

        def u2(cmd, arg="", timeout=30):
            spec = json.loads(arg)
            calls.append(spec)
            if "tap_label" in spec:
                pc._u2_status = "err act not sent: --index needs a read"  # a native screen
                return None
            return SAMPLE_XML
        self.allow("u2sock", side_effect=u2)
        with self.cap():
            pc.cmd_tap(self.parse(["tap", "OK", "--index", "1"]))
        # the helper is asked first (a page picks by index); a native
        # screen says no and the second OK is read, planned and tapped
        self.assertEqual(calls[0], {"tap_label": "OK", "index": 1, "idle": 1200})
        self.assertEqual(calls[1]["tap"], [250, 650])

    def test_tap_nudges_a_cut_off_row_then_taps_it_by_label(self):
        # espn.com, Oct 4: the row is cut off at the top edge on the first
        # read; after the nudge the helper reads afresh and taps by label
        # (the read right after the swipe may still give the old place)
        self.allow("wake_async", return_value=mock.Mock())
        self.allow("ui_dump", return_value=ET.fromstring(CLIPPED_XML))
        tc = self.allow("tap_center")
        self.allow("nav_record")
        swipe = self.allow("_scrcpy_swipe", return_value=True)
        acts = []

        def u2(cmd, arg="", timeout=30):
            acts.append(json.loads(arg))
            if len(acts) == 1:
                pc._u2_status = "err act not sent: 'Box Score' is cut off at the screen's edge"
                return None
            pc._u2_status = "ok"
            return WHOLE_XML
        self.allow("u2sock", side_effect=u2)
        with self.cap() as (out, err):
            rc = pc.cmd_tap(self.parse(["tap", "Box Score"]))
        self.assertEqual(rc, 0, "stdout=%r stderr=%r" % (out.getvalue(), err.getvalue()))
        swipe.assert_called_once_with("up", length=pc.nudge_px(2400))  # content down
        self.assertEqual([("tap_label" in a) for a in acts], [True, True])
        tc.assert_not_called()
        self.assertIn("tapped Box Score (label)", out.getvalue())

    def test_helper_label_target(self):
        mod = _u2mux()
        # the node's centre, by text or by description, case-insensitive
        self.assertEqual(mod.label_target(SAMPLE_XML, "ok"), (250, 450))
        self.assertEqual(mod.label_target(SAMPLE_XML, "Missing || Search"), (500, 650))
        with self.assertRaises(RuntimeError):
            mod.label_target(AMBI_XML, "OK")  # two rows read OK
        with self.assertRaises(RuntimeError):
            mod.label_target(SAMPLE_XML, "Nope")
        # one control drawn twice is one row: the View described Open over
        # the words Open, tapped at the View's centre
        self.assertEqual(mod.label_target(PAIR_XML, "open"), (540, 1050))
        # a row whose words are a sliver is cut off at an edge: not a tap
        with self.assertRaises(RuntimeError) as cm:
            mod.label_target(CLIPPED_XML, "Box Score")
        self.assertIn("cut off", str(cm.exception))
        self.assertEqual(mod.label_target(WHOLE_XML, "Box Score"), (796, 491))
        # a row with another row over it (a sticky bar's link) is not a tap
        with self.assertRaises(RuntimeError) as cm:
            mod.label_target(COVERED_XML, "Box Score")
        self.assertEqual(str(cm.exception), "'Box Score' is under 'Standings'")
        # on a native screen later is on top: the same rows outside a
        # WebView are a sheet over a page, and the earlier row is no cover
        native = COVERED_XML.replace("android.webkit.WebView", "android.widget.FrameLayout")
        self.assertEqual(mod.label_target(native, "Box Score"), (796, 491))
        # the card around a link holds all of it: no cover; the link's
        # own words sit inside it: no cover
        card = WHOLE_XML.replace(
            '<node text="" class="android.webkit.WebView"',
            '<node text="" content-desc="Dolphins 10 Vikings 15 Final" class="android.view.View"'
            ' package="com.android.chrome" bounds="[0,283][1080,2400]" clickable="true" enabled="true"/>'
            '<node text="" class="android.webkit.WebView"')
        self.assertEqual(mod.label_target(card, "Box Score"), (796, 491))
        # a tap by label reads the screen afresh and taps the row where it
        # is now, by coordinates: a touch, no lookup on the phone (the
        # selector click threw on a web node, Oct 4)
        dm = mod.U2Daemon.__new__(mod.U2Daemon)
        import threading
        dm._lock, dm._cache_lock = threading.RLock(), threading.Lock()
        dm._gen, dm._cache = 0, None
        dm._last_restart, dm._restart_error = -1e9, None
        dm._mute_since, dm._wordless_seen = None, False
        dm._last_xml, dm._last_xml_t = SAMPLE_XML, mod._time.monotonic()
        mod.log = lambda *a: None
        dm.d = _FakeServer([SAMPLE_XML], screen_on=True)
        sent = []

        def batch(calls, timeout=45.0):
            sent.append(calls)
            return [None] * (len(calls) - 1) + [SAMPLE_XML]
        dm._batch = batch
        dm.cmd_act(json.dumps({"tap_label": "OK"}))
        self.assertEqual(sent[0][1], ("click", [250, 450]))
        self.assertEqual(dm.d.calls, ["dumpWindowHierarchy"])  # one fresh read, which agreed
        # the row moved since the assistant's read (a web page's rows report
        # their old place for a moment after a scroll): it is read until
        # two reads agree on its place, three at most
        moved = SAMPLE_XML.replace("[100,400][400,500]", "[100,440][400,540]")
        moved2 = SAMPLE_XML.replace("[100,400][400,500]", "[100,460][400,560]")
        dm.d = _FakeServer([moved, moved2, moved2], screen_on=True)
        dm.cmd_act(json.dumps({"tap_label": "OK"}))
        self.assertEqual(sent[-1][1], ("click", [250, 510]))
        self.assertEqual(dm.d.calls, ["dumpWindowHierarchy"] * 3)
        # still moving after three reads: the newest place is tapped
        moved3 = SAMPLE_XML.replace("[100,400][400,500]", "[100,480][400,580]")
        dm.d = _FakeServer([moved, moved2, moved3], screen_on=True)
        dm.cmd_act(json.dumps({"tap_label": "OK"}))
        self.assertEqual(sent[-1][1], ("click", [250, 530]))
        self.assertEqual(dm.d.calls, ["dumpWindowHierarchy"] * 3)
        # the assistant's read didn't have the row whole (cut off at an
        # edge, then nudged): two reads that agree, then the tap
        dm._last_xml = CLIPPED_XML
        dm.d = _FakeServer([SAMPLE_XML, SAMPLE_XML], screen_on=True)
        dm.cmd_act(json.dumps({"tap_label": "OK"}))
        self.assertEqual(sent[-1][1], ("click", [250, 450]))
        self.assertEqual(dm.d.calls, ["dumpWindowHierarchy"] * 2)
        # the read before the tap fails: nothing was sent, and it says so
        dm.d = _FakeServer([], screen_on=True)
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"tap_label": "OK"}))
        self.assertTrue(str(cm.exception).startswith("act not sent: the read before it failed"),
                        str(cm.exception))
        # a quiet tap (a step of `burner do`): one fresh read before the
        # tap, the tap, the wait for the UI to go quiet, and no screen
        # sent back; the read before the tap stays the newest one known
        dm._last_xml, dm._last_xml_t = SAMPLE_XML, mod._time.monotonic()
        dm.d = _FakeServer([SAMPLE_XML], screen_on=True)
        sent.clear()
        dm._batch = lambda calls, timeout=45.0: sent.append(calls) or [None] * len(calls)
        self.assertEqual(dm.cmd_act(json.dumps({"tap_label": "OK", "quiet": True, "idle": 1200})), b"ok")
        self.assertEqual([m for m, _ in sent[0]], ["wakeUp", "click", "dumpWindowHierarchy", "waitForIdle"])
        self.assertEqual(sent[0][1], ("click", [250, 450]))
        self.assertEqual(dm.d.calls, ["dumpWindowHierarchy"])
        self.assertEqual(dm._last_xml, SAMPLE_XML)
        self.assertEqual([m for m, _ in mod.act_calls({"key": 66, "quiet": True, "idle": 500})],
                         ["wakeUp", "pressKeyCode", "dumpWindowHierarchy", "waitForIdle"])


class StartAndSettingsTests(OfflineTestCase):
    def test_start_goes_over_scrcpy_and_reads_once(self):
        sc = self.allow("scrcpy_send", return_value=True)
        adb = self.allow("adb_or_ensure")
        self.allow("u2_invalidate")
        self.allow("nav_record")
        calls = []
        self.allow("u2sock", side_effect=lambda cmd, arg="", timeout=30:
                   calls.append(cmd) or SAMPLE_XML)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_start(self.parse(["start", "com.example"]))
        self.assertEqual(rc, 0)
        sc.assert_called_once_with("startapp +com.example")
        adb.assert_not_called()
        self.assertEqual(calls, ["act"])
        self.assertIn("launched com.example", out.getvalue())
        self.assertIn("screen: com.example", out.getvalue())

    def test_start_reports_another_app_in_front(self):
        self.allow("scrcpy_send", return_value=True)
        self.allow("u2_invalidate")
        self.allow("nav_record")
        reads = self.allow("u2sock", return_value=SAMPLE_XML)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_start(self.parse(["start", "com.other"]))
        self.assertEqual(rc, 1)
        self.assertIn("com.other didn't come to the front; com.example is still open",
                      err.getvalue())
        # read again while the window may still be coming, then the verdict
        self.assertEqual(reads.call_count, 1 + pc.LAUNCH_REREADS)

    def test_start_waits_for_the_window_to_come_up(self):
        # a start from the home screen: the first read still shows the
        # launcher (Oct 5: "didn't come to the front" 0.8s in, exit 1)
        self.allow("scrcpy_send", return_value=True)
        self.allow("u2_invalidate")
        self.allow("nav_record")
        launcher = SAMPLE_XML.replace("com.example", "com.android.launcher")
        calls = []
        self.allow("u2sock", side_effect=lambda cmd, arg="", timeout=30:
                   calls.append(cmd) or (launcher if len(calls) == 1 else SAMPLE_XML))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_start(self.parse(["start", "com.example"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(calls, ["act", "act"])
        self.assertIn("screen: com.example", out.getvalue())
        self.assertNotIn("screen: com.android.launcher", out.getvalue())

    def test_start_uses_adb_without_the_scrcpy_helper(self):
        self.allow("scrcpy_send", side_effect=RuntimeError("no scrcpy"))
        adb = self.allow("adb_or_ensure", return_value=SimpleNamespace(
            stdout="  mFocusedApp=ActivityRecord{1 u0 com.example/.Main t3}\n",
            stderr="", returncode=0))
        self.allow("u2_invalidate")
        self.allow("nav_record")
        self.allow("u2sock", return_value="100")
        with mock.patch.object(pc.time, "sleep"), self.cap():
            rc = pc.cmd_start(SimpleNamespace(package="com.example", quiet=True))
        self.assertEqual(rc, 0)
        self.assertIn("-f 0x10008000", adb.call_args[0][1])

    def test_settings_page_by_name(self):
        adb = self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.allow("u2_invalidate")
        self.allow("u2sock", return_value=SAMPLE_XML)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_settings(self.parse(["settings", "bluetooth"]))
        self.assertEqual(rc, 0)
        self.assertIn("am start -a 'android.settings.BLUETOOTH_SETTINGS'", adb.call_args[0][1])
        self.assertIn("opened settings: bluetooth", out.getvalue())
        self.assertIn("screen: com.example", out.getvalue())

    def test_settings_app_page_and_listing(self):
        adb = self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.allow("u2_invalidate")
        self.allow("u2sock", return_value=SAMPLE_XML)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_settings(self.parse(["settings", "app", "com.tinder"]))
        self.assertEqual(rc, 0)
        self.assertIn("APPLICATION_DETAILS_SETTINGS -d 'package:com.tinder'", adb.call_args[0][1])
        with self.cap() as (out, err):
            self.assertEqual(pc.cmd_settings(self.parse(["settings"])), 0)
        self.assertIn("bluetooth", out.getvalue())
        with self.cap() as (out, err):
            self.assertEqual(pc.cmd_settings(self.parse(["settings", "nope"])), 1)
        self.assertIn("no settings page called", err.getvalue())


PS_LISTING = """PID ARGS
1 init second_stage
2843 app_process / com.wetest.uia2.Main -p 9008
2840 sh -c CLASSPATH=/data/local/tmp/u2.jar app_process / com.wetest.uia2.Main -p 9008
3110 com.google.android.apps.messaging
3200 app_process /system/bin com.android.commands.uiautomator.Launcher dump /sdcard/x.xml
3300 ps -A -o PID,ARGS
"""

EMPTY_XML = '<?xml version="1.0"?><hierarchy rotation="0" />'
# The system UI's bare window: an off screen reads like this.
SHADE_NODE = ('<node index="0" text="" class="android.widget.FrameLayout" '
              'package="com.android.systemui" bounds="[0,0][1080,2400]" />')
SHADE_XML = '<?xml version="1.0"?><hierarchy rotation="0">' + SHADE_NODE + '</hierarchy>'
# An app's nodes without one word on them: what a badly reading server
# returned for a Settings page (Oct 4), and what a screen mid-draw looks like.
MUTE_XML = ('<?xml version="1.0"?><hierarchy rotation="0">'
            '<node index="0" text="" class="android.widget.FrameLayout" package="com.android.settings"'
            ' content-desc="" bounds="[0,0][1080,2400]">'
            '<node index="0" text="" class="android.widget.LinearLayout" package="com.android.settings"'
            ' content-desc="" bounds="[0,0][1080,2400]">'
            '<node index="0" text="" class="androidx.recyclerview.widget.RecyclerView"'
            ' package="com.android.settings" content-desc="" bounds="[0,200][1080,2400]" />'
            '</node></node></hierarchy>')


class _FakeDev:
    """An adb device: records shells, answers `ps` with its listings in
    turn (the last one repeats), and says nothing about the screen
    unless `awake` is set."""
    awake = None

    def __init__(self, *listings):
        self.listings = list(listings)
        self.shells = []

    def shell(self, cmd, timeout=None):
        self.shells.append(cmd)
        assert timeout, "a phone shell needs a timeout"
        if cmd.startswith("dumpsys power"):
            return {True: "mWakefulness=Awake", False: "mWakefulness=Asleep"}.get(self.awake, "")
        if not cmd.startswith("ps"):
            return ""
        return self.listings.pop(0) if len(self.listings) > 1 else self.listings[0]


class _FakeServer:
    """A uiautomator2 device: deviceInfo and a queue of reads. One read
    more than queued is a failed test."""
    def __init__(self, reads, screen_on=True):
        self.reads = list(reads)
        self.info = {"screenOn": screen_on, "sdkInt": 35}
        self.calls = []
        self.jsonrpc = SimpleNamespace(wakeUp=lambda: None, getConfigurator=lambda: {},
                                       setConfigurator=lambda cfg: None)

    def jsonrpc_call(self, method, params, timeout=10):
        self.calls.append(method)
        return self.reads.pop(0)


PAIR_XML = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" package="com.android.vending" bounds="[0,0][1080,2400]" clickable="false" enabled="true">
    <node text="" content-desc="Open" class="android.view.View" package="com.android.vending" bounds="[0,1000][1080,1100]" clickable="true" enabled="true">
      <node text="Open" class="android.widget.TextView" package="com.android.vending" bounds="[400,1030][680,1070]" clickable="false" enabled="true"/>
    </node>
  </node>
</hierarchy>"""

# espn.com in Chrome, Oct 4: a "Box Score" link scrolled to the top edge of
# the page (its words read 2px tall), the "Where to watch" link under it.
CLIPPED_XML = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" package="com.android.chrome" bounds="[0,0][1080,2400]" clickable="false" enabled="true">
    <node text="" class="android.webkit.WebView" package="com.android.chrome" bounds="[0,283][1080,2400]" clickable="false" enabled="true">
      <node text="" content-desc="Box Score" class="android.view.View" package="com.android.chrome" bounds="[553,283][1039,306]" clickable="true" enabled="true">
        <node text="Box Score" class="android.widget.TextView" package="com.android.chrome" bounds="[729,283][863,285]" clickable="false" enabled="true"/>
      </node>
      <node text="" content-desc="Where to watch" class="android.view.View" package="com.android.chrome" bounds="[553,306][1039,350]" clickable="true" enabled="true">
        <node text="Where to watch" class="android.widget.TextView" package="com.android.chrome" bounds="[729,310][863,346]" clickable="false" enabled="true"/>
      </node>
    </node>
  </node>
</hierarchy>"""

# The same page nudged 180px down: the whole "Box Score" row, centre (796,491).
WHOLE_XML = (CLIPPED_XML.replace("[553,283][1039,306]", "[553,463][1039,520]")
             .replace("[729,283][863,285]", "[729,470][863,510]")
             .replace("[553,306][1039,350]", "[553,520][1039,564]")
             .replace("[729,310][863,346]", "[729,524][863,560]"))

# The same page with the sticky bar's Standings link over the Box Score
# row (earlier in the tree: document order says nothing about what is on
# top on a web page). Its rectangle holds the row's centre (796,491) and
# neither holds nor sits inside the row.
COVERED_XML = WHOLE_XML.replace(
    '<node text="" content-desc="Box Score"',
    '<node text="Standings" class="android.view.View" package="com.android.chrome"'
    ' bounds="[640,440][960,500]" clickable="true" enabled="true"/>\n'
    '      <node text="" content-desc="Box Score"')


def _cdp():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cdp_under_test", os.path.join(ROOT, "lib", "u2", "cdp.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CHROME_XML = CLIPPED_XML  # Chrome in front, its WebView 283px down a 2400px screen

WEB_SCREEN = {"title": "NFL on ESPN", "url": "https://www.espn.com/nfl/", "ready": "complete",
              "dpr": 2.0, "vw": 540, "vh": 1027,
              "rows": [{"text": "Box Score", "desc": "", "kind": "link", "click": True,
                        "l": 276.5, "t": 90, "w": 243, "h": 28.5},
                       {"text": "", "desc": "", "kind": "field", "value": "", "placeholder": "Search",
                        "focused": True, "l": 20, "t": 10, "w": 400, "h": 30},
                       {"text": "Standings", "desc": "", "kind": "text", "click": False,
                        "l": 0, "t": 2000, "w": 100, "h": 20}]}


class _FakePage:
    """A page session without Chrome: no events, never loading."""
    loading = False
    visible_at = 0.0

    def listen(self, seconds):
        pass

    def wait_parsed(self, cap_s):
        return True

    def close(self):
        pass


class _ScriptedPage(_FakePage):
    """A page that answers each of cdp's scripts by name ({"FIND_JS":
    result, ...}; a list answers in turn, its last answer repeated) and
    records every call: ("eval", name), ("many", [methods]) or (method,
    params). call_many answers the first command with the SELECT_JS
    answer, as type_text's batch wants."""

    def __init__(self, cdp, answers):
        self.cdp, self.answers, self.calls = cdp, answers, []

    def _name(self, expression):
        for name in ("FIND_JS", "TARGET_JS", "FILL_JS", "FILLED_JS", "READ_JS", "PLACE_JS",
                     "SELECT_JS", "SETTLE_JS", "SCROLL_JS"):
            if expression.startswith("(" + getattr(self.cdp, name)):
                return name
        return expression[:40]

    def _answer(self, name):
        a = self.answers.get(name)
        if isinstance(a, list):
            return a.pop(0) if len(a) > 1 else a[0]
        return a

    def eval(self, expression, timeout=10.0):
        name = self._name(expression)
        self.calls.append(("eval", name))
        return self._answer(name)

    def call(self, method, timeout=10.0, **params):
        self.calls.append((method, params))
        return {}

    def call_many(self, cmds, timeout=10.0):
        self.calls.append(("many", [c[0] for c in cmds]))
        return [{"result": {"value": self._answer("SELECT_JS")}}] + [{}] * (len(cmds) - 1)


class WebPathTests(OfflineTestCase):
    def test_websocket_frames_round_trip(self):
        cdp = _cdp()
        for text in ("x", "a" * 200, "b" * 70000):
            f = cdp.frame(text)
            self.assertEqual(f[0], 0x81)
            n = f[1] & 0x7F
            if n < 126:
                mask, body = f[2:6], f[6:]
            elif n == 126:
                self.assertEqual(int.from_bytes(f[2:4], "big"), len(text))
                mask, body = f[4:8], f[8:]
            else:
                self.assertEqual(int.from_bytes(f[2:10], "big"), len(text))
                mask, body = f[10:14], f[14:]
            self.assertEqual(cdp.xor_mask(body, mask).decode(), text)
        self.assertTrue(cdp.is_chrome("com.android.chrome"))
        self.assertFalse(cdp.is_chrome("com.example"))

    def test_page_commands_sent_together_come_back_in_order(self):
        cdp = _cdp()
        sent, answers = [], []

        class FakeWS:
            class s:
                @staticmethod
                def settimeout(t):
                    pass

            def send(self, text):
                m = json.loads(text)
                sent.append(m["method"])
                # answered out of order, with an event in between
                answers.insert(0, json.dumps({"id": m["id"], "result": {"n": m["id"]}}))
                answers.insert(0, json.dumps({"method": "Page.someEvent", "params": {}}))

            def recv(self):
                return answers.pop()
        page = object.__new__(cdp.Page)
        page.ws, page.n, page.visible_at = FakeWS(), 0, 0.0
        page.loading, page.main_frame, page.url = False, "F1", ""
        out = page.call_many([("Input.dispatchTouchEvent", {"type": "touchStart"}),
                              ("Input.dispatchTouchEvent", {"type": "touchEnd"})])
        self.assertEqual(sent, ["Input.dispatchTouchEvent"] * 2)
        self.assertEqual([r["n"] for r in out], [1, 2])

    def test_page_hears_its_navigation(self):
        cdp = _cdp()
        events = []

        class FakeWS:
            buf = b""

            class s:
                @staticmethod
                def settimeout(t):
                    pass

            def recv(self):
                return json.dumps(events.pop(0))
        page = object.__new__(cdp.Page)
        page.ws, page.n, page.visible_at = FakeWS(), 0, 0.0
        page.loading, page.main_frame, page.url = False, "F1", "https://a/"
        page._readable = lambda timeout: bool(events)
        # a subframe (an ad) loading is not a navigation
        events[:] = [{"method": "Page.frameStartedLoading", "params": {"frameId": "AD"}}]
        page.listen(0.01)
        self.assertFalse(page.loading)
        # the main frame loading is, until its document is parsed
        events[:] = [{"method": "Page.frameStartedLoading", "params": {"frameId": "F1"}},
                     {"method": "Page.frameNavigated", "params": {"frame": {"id": "F1", "url": "https://b/"}}}]
        page.listen(0.01)
        self.assertTrue(page.loading)
        self.assertEqual(page.url, "https://b/")
        events[:] = [{"method": "Page.domContentEventFired", "params": {"timestamp": 1}}]
        self.assertTrue(page.wait_parsed(0.5))
        self.assertFalse(page.loading)
        # after a touch: nothing heard, read at once; a navigation, waited out
        self.assertEqual(cdp.after_touch(page, listen_s=0.01), {"ready": "complete"})
        events[:] = [{"method": "Page.frameStartedLoading", "params": {"frameId": "F1"}}]
        page.loading = False
        self.assertEqual(cdp.after_touch(page, idle_ms=50, listen_s=0.01), {"ready": "loading"})

    def test_http_responses_are_read_by_their_headers(self):
        cdp = _cdp()

        def feed(*chunks):
            it = iter(chunks)
            return lambda n: next(it, b"")
        body = b'[{"id": "1"}]'
        # by Content-Length, with the connection left open (no close)
        status, got = cdp.recv_http(feed(
            b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body), body))
        self.assertEqual((status, got), ("HTTP/1.1 200 OK", body))
        # chunked
        status, got = cdp.recv_http(feed(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\n[{\"id\r\n",
            b"8\r\n\": \"1\"}]\r\n0\r\n\r\n"))
        self.assertEqual(got, body)
        # to the close
        status, got = cdp.recv_http(feed(b"HTTP/1.1 200 OK\r\n\r\n[{\"id\": ", b"\"1\"}]"))
        self.assertEqual(got, body)

    def test_helper_leaves_an_unreachable_page_alone_for_a_while(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = self._fake_cdp(mod, calls)

        def down(dev, current=None):
            raise TimeoutError("timed out")
        fake.front_page = down
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([SAMPLE_XML, SAMPLE_XML])
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        self.assertEqual(dm._dump(fresh=True), SAMPLE_XML)  # the screen reader instead
        self.assertEqual(calls, [])
        self.assertGreater(dm._web_retry_at, mod._time.monotonic())
        dm._last_xml = CHROME_XML
        fake.front_page = lambda dev, current=None: self.fail("asked again within the cooldown")
        self.assertEqual(dm._dump(fresh=True), SAMPLE_XML)

    def test_form_rows_read_like_native_controls(self):
        cdp = _cdp()
        screen = dict(WEB_SCREEN, rows=[
            {"text": "", "desc": "Password", "kind": "field", "value": "", "placeholder": "", "focused": True,
             "l": 20, "t": 100, "w": 400, "h": 30},
            {"text": "", "desc": "Default checkbox", "kind": "check", "click": True, "checked": False,
             "l": 20, "t": 200, "w": 20, "h": 20},
            {"text": "Two", "desc": "Dropdown (select example)", "kind": "select", "click": True,
             "l": 20, "t": 300, "w": 300, "h": 30},
            {"text": "One", "desc": "", "kind": "option", "click": True, "selected": False,
             "l": 20, "t": 330, "w": 300, "h": 30},
            {"text": "Two", "desc": "", "kind": "option", "click": True, "selected": True,
             "l": 20, "t": 360, "w": 300, "h": 30}])
        root = ET.fromstring(cdp.page_xml(screen, 283, 2400))
        lines, _ = pc.screen_lines(pc.walk(root), 1080, 2400)
        self.assertIn("[Password] (click,focused) [EditText] (440,513)", lines)
        self.assertIn("[Default checkbox] (click,off) [CheckBox] (60,703)", lines)
        self.assertIn("Two (click) [Spinner] (340,913)", lines)  # the dropdown, by its selected option
        self.assertIn("One (click) (340,973)", lines)  # its options, listed under it
        self.assertIn("Two (click,selected) (340,1033)", lines)
        nodes = pc.walk(root)
        self.assertEqual(pc.plan_tap(nodes, 1080, 2400, text="Default checkbox")["action"], "tap")

    def test_page_read_becomes_a_screen_read(self):
        cdp = _cdp()
        xml = cdp.page_xml(WEB_SCREEN, 283, 2400)
        root = ET.fromstring(xml)
        nodes = pc.walk(root)
        lines, _ = pc.screen_lines(nodes, 1080, 2400)
        # the address bar, the link at its device-pixel place (CSS x2,
        # 283px down), the field; the row below the viewport is left out
        self.assertEqual(lines, ["https://www.espn.com/nfl/ (click) [EditText] (442,213)",
                                 "[NFL on ESPN] [WebView] (540,1310)",
                                 "Box Score (click) (796,491)",
                                 "[Search] (click,focused) [EditText] (440,333)"])
        self.assertEqual(pc.dump_package(root), "com.android.chrome")
        pc._update_screen_from_dump(root)
        self.assertEqual(pc.screen_dims(), (1080, 2400))  # the nav bar keeps the screen whole
        plan = pc.plan_tap(nodes, 1080, 2400, text="Box Score")
        self.assertEqual((plan["action"], plan["xy"]), ("tap", (796, 491)))
        mod = _u2mux()
        self.assertEqual(mod.webview_top(xml), 283)
        self.assertEqual(mod.screen_of(xml), (1080, 2400, "com.android.chrome"))
        self.assertTrue(mod.has_words(xml))

    def _fake_cdp(self, mod, calls):
        import types
        real = mod._cdp()

        def tap(page, label, index=None, idle_ms=1200):
            calls.append(("tap", label, index, idle_ms))
            return {"found": label != "Nope", "count": 2 if label == "Twice" else 1,
                    "label": label, "screen": WEB_SCREEN}

        def scroll(page, direction="down", times=1, fraction=0.6, idle_ms=500):
            calls.append(("scroll", direction, times, idle_ms))
            return {"moved": 1200, "screen": WEB_SCREEN}
        fake = types.SimpleNamespace(
            is_chrome=real.is_chrome, page_xml=real.page_xml, NotSent=real.NotSent, NotDone=real.NotDone,
            front_page=lambda dev, current=None: _FakePage(), visible=lambda page, timeout=1.5: True,
            QUICK_PROBE_S=0.7,
            read=lambda page, cap=160: calls.append(("read",)) or WEB_SCREEN,
            tap=tap, scroll=scroll)
        mod._CDP = fake
        self.addCleanup(setattr, mod, "_CDP", None)
        return fake

    def test_helper_reads_the_page_when_chrome_is_in_front(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        self._fake_cdp(mod, calls)
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([SAMPLE_XML])
        dm._last_xml, dm._last_xml_t = SAMPLE_XML, mod._time.monotonic()
        # com.example in front: the screen reader, not the page
        self.assertEqual(dm._dump(fresh=True), SAMPLE_XML)
        self.assertEqual(calls, [])
        # Chrome in front: the page, as a screen read
        dm._last_xml = CHROME_XML
        xml = dm._dump(fresh=True)
        self.assertEqual(calls, [("read",)])
        self.assertIn('text="Box Score"', xml)
        self.assertEqual(mod.screen_of(xml), (1080, 2400, "com.android.chrome"))
        self.assertEqual(dm.d.calls, ["dumpWindowHierarchy"])  # only the first read
        # the next read builds on the page read (its WebView top, its screen)
        self.assertIn('bounds="[0,283][1080,2337]"', dm._dump(fresh=True))

    def test_helper_taps_and_scrolls_through_the_page(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        self._fake_cdp(mod, calls)
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([])
        dm._batch = lambda calls, timeout=45.0: self.fail("a page tap must not touch the screen reader")
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        xml = dm.cmd_act(json.dumps({"tap_label": "Box Score", "idle": 900})).decode()
        self.assertEqual(calls[-1], ("tap", "Box Score", None, 900))
        self.assertIn('text="Box Score"', xml)
        self.assertEqual(dm._last_xml, xml)
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"tap_label": "Nope"}))
        self.assertEqual(str(cm.exception), "act not sent: not on the page")
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"tap_label": "Twice"}))
        self.assertEqual(str(cm.exception), "act not sent: 2 rows read 'Twice'")
        xml = dm.cmd_act(json.dumps({"scroll": "down", "times": 2, "idle": 500})).decode()
        self.assertEqual(calls[-1], ("scroll", "down", 2, 500))
        self.assertIn("WebView", xml)
        # not a page: the scroll is not sent, the CLI swipes
        dm._last_xml = SAMPLE_XML
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"scroll": "down"}))
        self.assertEqual(str(cm.exception), "act not sent: not a page")

    def test_open_loads_the_link_in_the_page_when_chrome_is_in_front(self):
        page = _cdp().page_xml(WEB_SCREEN, 283, 2400)
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append((cmd, json.loads(arg)))
            return page
        self.allow("u2sock", side_effect=u2)
        adb = self.allow("adb_or_ensure")
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_open(self.parse(["open", "https://www.espn.com/nfl/"]))
        self.assertEqual(rc, 0, err.getvalue())
        adb.assert_not_called()  # no launch, no new tab
        self.assertEqual(calls, [("act", {"open": "https://www.espn.com/nfl/", "idle": 1000})])
        self.assertIn("opened https://www.espn.com/nfl/", out.getvalue())
        self.assertIn("Box Score (click) (796,491)", out.getvalue())
        # a link that isn't a web page, or a quiet open, is launched as before
        adb = self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.allow("u2_invalidate")
        calls.clear()
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            pc.cmd_open(self.parse(["open", "--quiet", "market://details?id=x", "com.android.vending"]))
        self.assertEqual(adb.call_count, 1)
        self.assertFalse(any(isinstance(c[1], dict) and "open" in c[1] for c in calls))

    def test_helper_types_into_the_page(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = self._fake_cdp(mod, calls)

        def type_text(page, text, idle_ms=800):
            calls.append(("type", text, idle_ms))
            if text == "nowhere":
                raise fake.NotSent("no field has the focus on the page")
            return {"screen": WEB_SCREEN, "ready": "complete"}
        fake.type_text = type_text
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([])
        dm._batch = lambda calls, timeout=45.0: self.fail("typing into a page must not use the screen reader")
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        xml = dm.cmd_act(json.dumps({"set_text": "Pixel 7 battery", "idle": 800})).decode()
        self.assertEqual(calls[-1], ("type", "Pixel 7 battery", 800))
        self.assertIn("WebView", xml)
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"set_text": "nowhere"}))
        self.assertEqual(str(cm.exception), "act not sent: no field has the focus on the page")
        # a tap by label with an index: the page picks
        xml = dm.cmd_act(json.dumps({"tap_label": "Box Score", "index": 1, "idle": 900})).decode()
        self.assertEqual(calls[-1], ("tap", "Box Score", 1, 900))

    def test_helper_waits_by_asking_the_page(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = self._fake_cdp(mod, calls)
        answers = iter([{"found": False}, {"found": False},
                        {"found": True, "text": "Top Stories", "desc": "", "bounds": "[0,2600][540,2660]",
                         "enabled": True, "inview": False, "count": 1}])

        def find(page, label, top=0):
            calls.append(("find", label, top))
            return next(answers)
        fake.find = find
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([])  # the screen reader is never asked
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        out = json.loads(dm.cmd_wait_for(json.dumps({"text": "Top Stories", "timeout": 5})))
        self.assertTrue(out["found"])
        self.assertEqual((out["text"], out["bounds"], out["polls"]), ("Top Stories", "[0,2600][540,2660]", 3))
        self.assertEqual([c for c in calls if c[0] == "find"], [("find", "Top Stories", 283)] * 3)
        self.assertIn('text="Box Score"', dm._last_xml)  # the page read once the words were there
        # gone: the page no longer has the words
        fake.find = lambda page, label, top=0: {"found": False}
        out = json.loads(dm.cmd_wait_for(json.dumps({"text": "Top Stories", "timeout": 5, "absent": True})))
        self.assertTrue(out["gone"])
        # a timeout is the usual miss
        with self.assertRaises(mod.U2NotFound):
            dm.cmd_wait_for(json.dumps({"text": "Top Stories", "timeout": 0.3}))

    def test_helper_looks_at_the_screen_when_the_page_left_the_front(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = self._fake_cdp(mod, calls)
        fake.visible = lambda page, timeout=1.5: False  # the page in hand is hidden now
        fake.front_page = lambda dev, current=None: self.fail("no tab scan with another app in front")

        class Gone:
            def close(self):
                calls.append(("close",))
        dm = EmptyScreenTests._daemon(self, mod)
        dm._web = Gone()
        dm.d = _FakeServer([SAMPLE_XML])  # com.example is in front now
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        self.assertIsNone(dm._page())
        self.assertEqual(calls, [("close",)])
        self.assertEqual(dm.d.calls, ["dumpWindowHierarchy"])
        self.assertEqual(dm._last_xml, SAMPLE_XML)  # and that look is the newest read
        self.assertIsNone(dm._web)

    def test_helper_fills_a_field_by_its_label(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = self._fake_cdp(mod, calls)

        def fill(page, label, text, index=None):
            calls.append(("fill", label, text, index))
            if label == "Nope":
                return {"found": False, "screen": WEB_SCREEN}
            return {"found": True, "count": 1, "label": label, "mode": "value", "value": text,
                    "screen": WEB_SCREEN}
        fake.fill = fill
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([])
        dm._batch = lambda calls, timeout=45.0: self.fail("a page fill must not use the screen reader")
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        xml = dm.cmd_act(json.dumps({"set_text": "2026-10-05", "field": "Date picker"})).decode()
        self.assertEqual(calls[-1], ("fill", "Date picker", "2026-10-05", None))
        self.assertIn("WebView", xml)
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"set_text": "x", "field": "Nope"}))
        self.assertEqual(str(cm.exception), "act not sent: no field labelled 'Nope' on the page")

    def test_fill_on_a_button_taps_it_and_types_into_the_field_that_opens(self):
        # mobile Wikipedia at phone width: the search box is hidden and
        # "Search" is the magnifier button; its touch opens a box with the focus
        cdp = _cdp()
        on_button = {"found": True, "count": 1, "label": "search", "notField": True, "tag": "span",
                     "x": 305, "y": 27, "url": "https://en.m.wikipedia.org/"}
        page = _ScriptedPage(cdp, {
            "FIND_JS": on_button, "TARGET_JS": {"ok": True, "how": "focus", "label": "search wikipedia"},
            "FILL_JS": {"ok": True, "mode": "insert"}, "FILLED_JS": {"ok": True, "value": "Pixel 7"},
            "READ_JS": WEB_SCREEN})
        with mock.patch.object(cdp.time, "sleep"):
            r = cdp.fill(page, "Search", "Pixel 7")
        self.assertEqual((r["found"], r["how"], r["value"]), (True, "tap+focus", "Pixel 7"))
        self.assertEqual([c[1] if c[0] == "eval" else c[0] for c in page.calls],
                         ["FIND_JS", "many", "TARGET_JS", "FILL_JS", "Input.insertText", "FILLED_JS", "READ_JS"])
        self.assertEqual(page.calls[1], ("many", ["Input.dispatchTouchEvent", "Input.dispatchTouchEvent"]))
        # the touch led to a page with the box (no focus): the field with the label
        page = _ScriptedPage(cdp, {
            "FIND_JS": on_button, "TARGET_JS": [{"ok": False, "fields": 0}, {"ok": True, "how": "label", "label": "search wikipedia"}],
            "FILL_JS": {"ok": True, "mode": "insert"}, "FILLED_JS": {"ok": True, "value": "Pixel 7"},
            "READ_JS": WEB_SCREEN})
        with mock.patch.object(cdp.time, "sleep"):
            r = cdp.fill(page, "Search", "Pixel 7")
        self.assertEqual(r["how"], "tap+label")
        self.assertEqual([c[1] for c in page.calls if c[0] == "eval"],
                         ["FIND_JS", "TARGET_JS", "TARGET_JS", "FILL_JS", "FILLED_JS", "READ_JS"])

    def test_fill_says_when_the_button_opened_no_field(self):
        cdp = _cdp()
        page = _ScriptedPage(cdp, {
            "FIND_JS": {"found": True, "count": 1, "label": "search", "notField": True, "tag": "span",
                        "x": 305, "y": 27, "url": "u"},
            "TARGET_JS": {"ok": False, "fields": 2, "named": 0}, "READ_JS": WEB_SCREEN})
        with mock.patch.object(cdp.time, "sleep"), self.assertRaises(cdp.NotDone) as cm:
            cdp.fill(page, "Search", "Pixel 7")
        self.assertEqual(str(cm.exception),
                         "'Search' is a span, not a field; tapping it opened 2 text fields, none labelled 'Search'")
        self.assertNotIn("Input.insertText", [c[0] for c in page.calls])

    def test_fill_of_a_field_touches_nothing(self):
        cdp = _cdp()
        page = _ScriptedPage(cdp, {
            "FIND_JS": {"found": True, "count": 1, "label": "email", "tag": "input", "type": "email", "url": "u"},
            "FILL_JS": {"ok": True, "mode": "insert"}, "FILLED_JS": {"ok": True, "value": "a@b.c"},
            "READ_JS": WEB_SCREEN})
        with mock.patch.object(cdp.time, "sleep"):
            r = cdp.fill(page, "Email", "a@b.c")
        self.assertEqual((r["how"], r["value"]), ("fill", "a@b.c"))
        self.assertNotIn("many", [c[0] for c in page.calls])

    def test_type_takes_the_one_field_in_view_when_nothing_has_the_focus(self):
        cdp = _cdp()
        page = _ScriptedPage(cdp, {"SELECT_JS": {"ok": True, "direct": False, "type": "search", "only": True},
                                   "READ_JS": WEB_SCREEN})
        with mock.patch.object(cdp.time, "sleep"):
            r = cdp.type_text(page, "Pixel 7")
        self.assertTrue(r["only"])
        self.assertEqual(page.calls[0], ("many", ["Runtime.evaluate", "Input.insertText"]))
        page = _ScriptedPage(cdp, {"SELECT_JS": {"ok": False, "fields": 2}})
        with self.assertRaises(cdp.NotSent) as cm:
            cdp.type_text(page, "Pixel 7")
        self.assertEqual(str(cm.exception), "no field has the focus on the page "
                         "(2 text fields in view; tap one, or type --field with its label)")

    def test_helper_keeps_the_page_when_a_fill_did_not_take(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = self._fake_cdp(mod, calls)

        def fill(page, label, text, index=None):
            raise fake.NotDone("%r is a span, not a field; tapping it opened no text field" % label)
        fake.fill = fill
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([])
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"set_text": "Pixel 7", "field": "Search"}))
        self.assertEqual(str(cm.exception), "act failed after sending: 'Search' is a span, not a field; "
                         "tapping it opened no text field")
        self.assertIsNotNone(dm._web)  # the page session is fine: no reconnect

    def test_type_field_that_failed_on_the_phone_claims_no_typing(self):
        self.allow("u2_invalidate")
        self.allow("nav_record")

        def u2(cmd, arg="", timeout=30):
            pc._u2_status = ("err act failed after sending: 'Search' is a span, not a field; "
                             "tapping it opened no text field")
            return None
        self.allow("u2sock", side_effect=u2)
        with self.cap() as (out, err):
            rc = pc.cmd_type(self.parse(["type", "--field", "Search", "Pixel 7"]))
        self.assertEqual(rc, 1)
        self.assertNotIn("typed", out.getvalue())
        self.assertIn("the typing failed on the phone ('Search' is a span, not a field", err.getvalue())

    def test_type_into_a_labelled_field_is_one_helper_op(self):
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append((cmd, json.loads(arg)))
            return _cdp().page_xml(WEB_SCREEN, 283, 2400)
        self.allow("u2sock", side_effect=u2)
        self.allow("nav_record")
        with self.cap() as (out, err):
            rc = pc.cmd_type(self.parse(["type", "--field", "Date picker", "2026-10-05"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(calls, [("act", {"set_text": "2026-10-05", "field": "Date picker", "idle": 1200})])
        self.assertIn("typed 10 chars into Date picker", out.getvalue())

    def test_helper_opens_a_link_in_the_page(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = self._fake_cdp(mod, calls)

        def navigate(page, url, idle_ms=1000):
            calls.append(("open", url, idle_ms))
            return {"screen": WEB_SCREEN, "ready": "interactive"}
        fake.navigate = navigate
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([])
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        xml = dm.cmd_act(json.dumps({"open": "https://www.espn.com/nfl/", "idle": 1000})).decode()
        self.assertEqual(calls[-1], ("open", "https://www.espn.com/nfl/", 1000))
        self.assertIn('text="Box Score"', xml)
        dm._last_xml = SAMPLE_XML  # not Chrome: launched by the CLI instead
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"open": "https://example.com"}))
        self.assertEqual(str(cm.exception), "act not sent: not a page")

    def test_scroll_asks_the_page_when_chrome_is_in_front(self):
        self.allow("wake")
        self.allow("u2_invalidate")
        for name in ("_screen_wh", "_screen_pkg", "_screen_kbd"):
            self.addCleanup(setattr, pc, name, getattr(pc, name))
        calls = []
        page = _cdp().page_xml(WEB_SCREEN, 283, 2400)

        def u2(cmd, arg="", timeout=30):
            calls.append((cmd, json.loads(arg) if arg.startswith("{") else arg))
            return "1080 2400 com.android.chrome" if cmd == "screen" else page
        self.allow("u2sock", side_effect=u2)
        sc = self.allow("scrcpy_send", return_value=True)
        self.allow("ui_dump", return_value=ET.fromstring(page))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_scroll(SimpleNamespace(direction="down", times=1, to=None, quiet=False))
        self.assertEqual(rc, 0, err.getvalue())
        sc.assert_not_called()  # no finger swipe on a page
        self.assertEqual([c[0] for c in calls], ["screen", "act"])
        self.assertEqual(calls[1][1]["scroll"], "down")
        self.assertIn("scrolled down x1", out.getvalue())
        self.assertIn("Box Score (click) (796,491)", out.getvalue())


class RepeatedRowsTests(OfflineTestCase):
    def test_rows_read_again_are_printed_once(self):
        block = ('<node text="Flames" class="android.widget.TextView" package="com.android.chrome"'
                 ' bounds="[100,2200][460,2290]" clickable="false" enabled="true" />'
                 '<node text="LIVE" class="android.widget.TextView" package="com.android.chrome"'
                 ' bounds="[470,2190][520,2240]" clickable="false" enabled="true" />')
        xml = ('<hierarchy rotation="0"><node text="" class="android.widget.FrameLayout"'
               ' package="com.android.chrome" bounds="[0,0][1080,2400]" clickable="false" enabled="true">'
               + block * 20 + '</node></hierarchy>')
        lines, _ = pc.screen_lines(pc.walk(ET.fromstring(xml)), 1080, 2400)
        self.assertEqual(lines, ["Flames (280,2245)", "LIVE (495,2215)"])


class EmptyScreenTests(OfflineTestCase):
    def no_sleep(self, mod):
        """The helper's waits are no-ops for this test only (its `_time`
        is the shared time module, so a plain assignment would leak), its
        log lines go nowhere, and its trace spans never touch run/."""
        p = mock.patch.object(mod._time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)
        mod.log = lambda *a: None
        mod.TRACE_FILE = os.path.join(ROOT, "run", "no-such-trace.jsonl")

    def test_helper_real_screen(self):
        mod = _u2mux()
        self.assertTrue(mod.real_screen(SAMPLE_XML))
        self.assertFalse(mod.real_screen(EMPTY_XML))
        self.assertFalse(mod.real_screen(""))
        self.assertFalse(mod.real_screen(None))

    def test_helper_blank_screen(self):
        mod = _u2mux()
        self.assertTrue(mod.blank_screen(EMPTY_XML))
        self.assertTrue(mod.blank_screen(""))
        self.assertTrue(mod.blank_screen(SHADE_XML))
        self.assertTrue(mod.blank_screen(SHADE_XML.replace(SHADE_NODE, SHADE_NODE * 2)))
        self.assertFalse(mod.blank_screen(SAMPLE_XML))
        # an app's bare window is the app, mid-draw: not blank
        self.assertFalse(mod.blank_screen(SHADE_XML.replace("com.android.systemui", "com.example")))
        # a pulled-down shade or a lock screen has many nodes: not blank
        self.assertFalse(mod.blank_screen(SHADE_XML.replace(SHADE_NODE, SHADE_NODE * 5)))

    def test_helper_mute_read(self):
        mod = _u2mux()
        self.assertTrue(mod.mute_read(MUTE_XML))
        self.assertFalse(mod.mute_read(SAMPLE_XML))
        self.assertFalse(mod.mute_read(EMPTY_XML))  # that one is blank, not wordless
        self.assertFalse(mod.mute_read(SHADE_XML))
        # one word anywhere, as text or as a description, and it isn't mute
        self.assertFalse(mod.mute_read(MUTE_XML.replace('content-desc="" bounds="[0,200]',
                                                        'content-desc="Search" bounds="[0,200]')))
        self.assertFalse(mod.mute_read(MUTE_XML.replace('text="" class="android.widget.LinearLayout"',
                                                        'text="Pair new device" class="android.widget.LinearLayout"')))
        # a word is a word: an entity in a text attribute counts
        self.assertFalse(mod.mute_read(MUTE_XML.replace(' text="" class="android.widget.LinearLayout"',
                                                        ' text="&quot;" class="android.widget.LinearLayout"')))
        # a resource-id is never a word
        self.assertTrue(mod.mute_read(MUTE_XML.replace('index="0" text="" class="androidx',
                                                       'index="0" resource-id="android:id/text1" text="" class="androidx')))

    def test_helper_fix_blank_read_replaces_a_wordless_server(self):
        mod = _u2mux()
        self.no_sleep(mod)
        clock = [1000.0]
        p = mock.patch.object(mod._time, "monotonic", lambda: clock[0])
        p.start()
        self.addCleanup(p.stop)
        # a screen loading: wordless for a few seconds, kept as it is, no
        # extra read, no restart
        dm = self._daemon(mod)
        dm.d = _FakeServer([], screen_on=True)
        self.assertEqual(dm._fix_blank_read(MUTE_XML, "wordless read"), (MUTE_XML, False))
        self.assertEqual(dm._mute_since, 1000.0)
        clock[0] += 5
        self.assertEqual(dm._fix_blank_read(MUTE_XML, "wordless read"), (MUTE_XML, False))
        self.assertEqual(dm.d.calls, [])
        # wordless for 8s across reads: the server is replaced, by force (a
        # wordless server passes the usual checks)
        restarts = []

        def connect(force=False):
            restarts.append(force)
            dm._last_restart = clock[0]
            dm.d = _FakeServer([SAMPLE_XML])
        dm.connect = connect
        clock[0] += 4
        self.assertEqual(dm._fix_blank_read(MUTE_XML, "wordless read"), (SAMPLE_XML, False))
        self.assertEqual(restarts, [True])
        self.assertFalse(dm._wordless_seen)  # the fresh server read words
        # a fresh server that reads no words either settles it: the screen
        # has none, and reads are kept, however long, until one has words
        dm = self._daemon(mod)

        def connect_mute(force=False):
            restarts.append(force)
            dm.d = _FakeServer([MUTE_XML])
        dm.connect = connect_mute
        dm._mute_since = clock[0] - 30
        dm.d = _FakeServer([], screen_on=True)
        self.assertEqual(dm._fix_blank_read(MUTE_XML, "wordless read"), (MUTE_XML, False))
        self.assertTrue(dm._wordless_seen)
        self.assertEqual(restarts, [True, True])
        dm._last_restart = -1e9
        clock[0] += 100
        self.assertEqual(dm._fix_blank_read(MUTE_XML, "wordless read"), (MUTE_XML, False))
        self.assertEqual(restarts, [True, True])
        # a replacement that fails keeps the read and the spell's attempt:
        # the cooldown paces the next try
        dm = self._daemon(mod)
        dm._mute_since = clock[0] - 30
        dm.connect = lambda force=False: (_ for _ in ()).throw(RuntimeError("no phone"))
        dm.d = _FakeServer([], screen_on=True)
        self.assertEqual(dm._fix_blank_read(MUTE_XML, "wordless read"), (MUTE_XML, False))
        self.assertFalse(dm._wordless_seen)
        # the screen went off during the replacement: the read after it is
        # blank, and the wake path handles it
        dm = self._daemon(mod)
        dm._mute_since = clock[0] - 30
        woke = []

        def connect_dark(force=False):
            dm._last_restart = clock[0]
            dm.d = _FakeServer([SHADE_XML, SAMPLE_XML], screen_on=False)
            dm.d.jsonrpc.wakeUp = lambda: woke.append(1)
        dm.connect = connect_dark
        dm.d = _FakeServer([], screen_on=True)
        self.assertEqual(dm._fix_blank_read(MUTE_XML, "wordless read"), (SAMPLE_XML, False))
        self.assertEqual(woke, [1])
        # replace=False (a wait near its deadline): kept whatever the spell
        dm = self._daemon(mod)
        dm._mute_since = clock[0] - 30
        dm.d = _FakeServer([], screen_on=True)
        self.assertEqual(dm._fix_blank_read(MUTE_XML, "wordless read", replace=False),
                         (MUTE_XML, False))

    def test_helper_wordless_spell_ends_with_a_read_with_words(self):
        mod = _u2mux()
        self.no_sleep(mod)
        dm = self._daemon(mod)
        dm._mute_since, dm._wordless_seen = 1.0, True
        dm.d = _FakeServer([SAMPLE_XML], screen_on=True)
        self.assertEqual(dm._dump(), SAMPLE_XML)
        self.assertEqual((dm._mute_since, dm._wordless_seen), (None, False))
        # a blank read doesn't end it (an app with no words whose screen went off)
        dm._mute_since, dm._wordless_seen = 1.0, True
        dm._last_restart = mod._time.monotonic()
        dm.d = _FakeServer([SHADE_XML], screen_on=True)
        self.assertEqual(dm._dump(fresh=True), SHADE_XML)
        self.assertEqual((dm._mute_since, dm._wordless_seen), (1.0, True))
        # after an action too
        dm._mute_since, dm._wordless_seen = 1.0, True
        dm._batch = lambda calls, timeout=45.0: [None] * (len(calls) - 1) + [SAMPLE_XML]
        dm.cmd_act(json.dumps({"tap": [1, 2]}))
        self.assertEqual((dm._mute_since, dm._wordless_seen), (None, False))
        # a wordless read after an action comes back as the screen, never as an error
        dm.d = _FakeServer([], screen_on=True)
        dm._batch = lambda calls, timeout=45.0: [None] * (len(calls) - 1) + [MUTE_XML]
        self.assertEqual(dm.cmd_act(json.dumps({"tap": [1, 2]})), MUTE_XML.encode())
        self.assertIsNotNone(dm._mute_since)

    def test_helper_wait_for_on_a_wordless_screen(self):
        mod = _u2mux()
        self.no_sleep(mod)
        clock = [100.0]

        def tick():
            clock[0] += 0.3
            return clock[0]
        p = mock.patch.object(mod._time, "monotonic", tick)
        p.start()
        self.addCleanup(p.stop)
        # the deadline holds: a wordless screen costs no extra reads
        dm = self._daemon(mod)
        dm.d = _FakeServer([MUTE_XML] * 8, screen_on=True)
        with self.assertRaises(mod.U2NotFound):
            dm.cmd_wait_for(json.dumps({"text": "Nope", "timeout": 1}))
        self.assertLessEqual(len(dm.d.calls), 4)
        # "gone" is never read off a wordless screen until a fresh server agreed
        dm = self._daemon(mod)
        dm.d = _FakeServer([MUTE_XML] * 8, screen_on=True)
        with self.assertRaises(mod.U2NotFound):
            dm.cmd_wait_for(json.dumps({"text": "Nope", "absent": True, "timeout": 1}))
        dm._wordless_seen = True
        dm.d = _FakeServer([MUTE_XML], screen_on=True)
        out = json.loads(dm.cmd_wait_for(json.dumps({"text": "Nope", "absent": True, "timeout": 1})))
        self.assertTrue(out["gone"])
        # a long wait on a long spell replaces the server and finds the label
        dm = self._daemon(mod)
        dm._mute_since = clock[0] - 30
        restarts = []

        def connect(force=False):
            restarts.append(force)
            dm._last_restart = clock[0]
            dm.d = _FakeServer([SAMPLE_XML] * 3)
        dm.connect = connect
        dm.d = _FakeServer([MUTE_XML], screen_on=True)
        out = json.loads(dm.cmd_wait_for(json.dumps({"text": "Hello", "timeout": 30})))
        self.assertTrue(out["found"])
        self.assertEqual(restarts, [True])
        # a short wait on the same spell doesn't: not worth a replacement
        dm = self._daemon(mod)
        dm._mute_since = clock[0] - 30
        dm.d = _FakeServer([MUTE_XML] * 8, screen_on=True)
        with self.assertRaises(mod.U2NotFound):
            dm.cmd_wait_for(json.dumps({"text": "Hello", "timeout": 2}))

    def test_helper_ensure_server_replaces_a_wordless_server_by_force(self):
        mod = _u2mux()
        self.no_sleep(mod)
        devs = [_FakeServer([SAMPLE_XML])]
        fake_u2 = SimpleNamespace(connect=lambda target: devs.pop(0))
        adb_dev = _FakeDev(PS_LISTING, "PID ARGS\n1 init\n")
        fake_adbutils = SimpleNamespace(adb=SimpleNamespace(device=lambda target: adb_dev))
        real_works = mod.server_works
        mod.server_works = lambda d, words=False: True  # the running server passes every check
        mod._KEEPALIVE = SimpleNamespace(close=lambda: None)
        mod.apply_fast_config = lambda d: None
        with mock.patch.dict(sys.modules, {"uiautomator2": fake_u2, "adbutils": fake_adbutils}):
            d = mod.ensure_server(force=True)
        self.assertEqual(d.reads, [SAMPLE_XML])
        self.assertEqual(adb_dev.shells, ["ps -A -o PID,ARGS", "kill -9 2843 2840 3200",
                                          "ps -A -o PID,ARGS", "dumpsys power | grep -m1 mWakefulness=", "input keyevent 224"])
        # mid-session a fresh server that reads no words is taken as it reads
        fresh = _FakeServer([MUTE_XML])
        devs = [fresh]
        fake_u2 = SimpleNamespace(connect=lambda target: devs.pop(0))
        adb_dev = _FakeDev(PS_LISTING, "PID ARGS\n1 init\n")
        fake_adbutils = SimpleNamespace(adb=SimpleNamespace(device=lambda target: adb_dev))
        mod.server_works = real_works
        with mock.patch.dict(sys.modules, {"uiautomator2": fake_u2, "adbutils": fake_adbutils}):
            d = mod.ensure_server(force=True)
        self.assertIs(d, fresh)
        self.assertEqual(adb_dev.shells.count("kill -9 2843 2840 3200"), 1)
        # a launch that fails gets one more try
        devs = [None, _FakeServer([SAMPLE_XML])]

        def connect_or_fail(target):
            d = devs.pop(0)
            if d is None:
                raise RuntimeError("server not ready")
            return d
        fake_u2 = SimpleNamespace(connect=connect_or_fail)
        adb_dev = _FakeDev(PS_LISTING, "PID ARGS\n1 init\n", PS_LISTING, "PID ARGS\n1 init\n")
        fake_adbutils = SimpleNamespace(adb=SimpleNamespace(device=lambda target: adb_dev))
        with mock.patch.dict(sys.modules, {"uiautomator2": fake_u2, "adbutils": fake_adbutils}):
            d = mod.ensure_server(force=True)
        self.assertEqual(d.reads, [])
        self.assertEqual(adb_dev.shells.count("kill -9 2843 2840 3200"), 2)

    def test_helper_server_pids(self):
        mod = _u2mux()
        # the server, its shell wrapper and a stray uiautomator command;
        # not the ps itself, an app, or init
        self.assertEqual(mod.server_pids(PS_LISTING), ["2843", "2840", "3200"])
        self.assertEqual(mod.server_pids(""), [])
        self.assertEqual(mod.server_pids(None), [])

    def test_helper_kill_server_on_phone(self):
        mod = _u2mux()
        self.no_sleep(mod)
        dev = _FakeDev(PS_LISTING, "PID ARGS\n1 init\n")
        self.assertEqual(mod.kill_server_on_phone(dev), ["2843", "2840", "3200"])
        self.assertEqual(dev.shells, ["ps -A -o PID,ARGS", "kill -9 2843 2840 3200",
                                      "ps -A -o PID,ARGS"])
        # nothing running: nothing killed
        dev = _FakeDev("PID ARGS\n1 init\n")
        self.assertEqual(mod.kill_server_on_phone(dev), [])
        self.assertEqual(dev.shells, ["ps -A -o PID,ARGS"])
        # the kill didn't take: said so, not "killed"
        dev = _FakeDev(PS_LISTING)
        with self.assertRaises(RuntimeError) as cm:
            mod.kill_server_on_phone(dev)
        self.assertIn("could not kill the UI server on the phone (pid 2843", str(cm.exception))

    def test_helper_err_text(self):
        mod = _u2mux()
        self.assertEqual(mod.err_text(RuntimeError("a  b\nc")), "RuntimeError: a b c")
        long = RuntimeError("x" * 200 + " already registered")
        self.assertTrue(mod.err_text(long).endswith("already registered"))
        self.assertTrue(mod.err_text(long).startswith("RuntimeError: "))

    def test_helper_server_works_reads_twice(self):
        mod = _u2mux()
        self.no_sleep(mod)
        # a fresh server's first read is empty, the second real: it works
        d = _FakeServer([EMPTY_XML, SAMPLE_XML])
        self.assertTrue(mod.server_works(d))
        self.assertEqual(d.calls, ["dumpWindowHierarchy"] * 2)
        # empty twice: it doesn't
        self.assertFalse(mod.server_works(_FakeServer([EMPTY_XML, EMPTY_XML])))
        # an off screen reads blank whatever the server does: woken first,
        # and given a few seconds for the app's words to come back
        woke, slept = [], []
        d = _FakeServer([SAMPLE_XML], screen_on=False)
        d.jsonrpc = SimpleNamespace(wakeUp=lambda: woke.append(1))
        mod._time.sleep = slept.append  # no_sleep's patch is on the same object
        self.assertTrue(mod.server_works(d))
        self.assertEqual((woke, d.calls, slept), ([1], ["dumpWindowHierarchy"], [3.0]))
        # wordless right after the wake: a second read, two seconds on
        d = _FakeServer([MUTE_XML, SAMPLE_XML], screen_on=False)
        d.jsonrpc = SimpleNamespace(wakeUp=lambda: None)
        slept.clear()
        self.assertTrue(mod.server_works(d, words=True))
        self.assertEqual(slept, [3.0, 2.0])
        # with the screen on nothing is woken and nothing waits, except
        # the half second before a second read
        slept, woke = [], []
        mod._time.sleep = slept.append
        d = _FakeServer([SAMPLE_XML])
        d.jsonrpc = SimpleNamespace(wakeUp=lambda: woke.append(1))
        self.assertTrue(mod.server_works(d))
        self.assertEqual((slept, woke), ([], []))
        self.assertTrue(mod.server_works(_FakeServer([EMPTY_XML, SAMPLE_XML])))
        self.assertEqual(slept, [0.5])
        # a read with nodes and no word passes, unless words are wanted:
        # then it gets the same second read an empty one gets
        self.assertTrue(mod.server_works(_FakeServer([MUTE_XML])))
        self.assertFalse(mod.server_works(_FakeServer([MUTE_XML, MUTE_XML]), words=True))
        self.assertTrue(mod.server_works(_FakeServer([MUTE_XML, SAMPLE_XML]), words=True))
        self.assertFalse(mod.server_works(_FakeServer([EMPTY_XML, MUTE_XML]), words=True))
        self.assertTrue(mod.server_works(_FakeServer([SAMPLE_XML]), words=True))
        # the system UI's bare window (a screen waking up) is not wordless
        self.assertTrue(mod.server_works(_FakeServer([SHADE_XML]), words=True))

    def test_helper_restart_allowed(self):
        mod = _u2mux()
        self.assertFalse(mod.restart_allowed(3))
        self.assertTrue(mod.restart_allowed(60))

    def test_helper_act_calls_wake_first(self):
        mod = _u2mux()
        for spec in ({"tap": [1, 2]}, {"key": 4}, {"tap": [250, 450]}):
            calls = mod.act_calls(spec)
            self.assertEqual(calls[0], ("wakeUp", []), spec)
            self.assertIn(calls[1][0], mod.ACTION_METHODS)
            self.assertEqual([m for m, _ in calls[2:]],
                             ["dumpWindowHierarchy", "waitForIdle", "dumpWindowHierarchy"])
        # POWER and SLEEP turn the screen off: no wake before them
        for key in (26, 223, "26"):
            self.assertEqual(mod.act_calls({"key": key})[0], ("pressKeyCode", [int(key)]))
            self.assertTrue(mod.sleeps_the_screen({"key": key}))
        self.assertFalse(mod.sleeps_the_screen({"key": 4}))
        self.assertFalse(mod.sleeps_the_screen({"tap": [1, 2]}))
        # a bare read wakes nothing
        self.assertEqual([m for m, _ in mod.act_calls({})],
                         ["waitForIdle", "dumpWindowHierarchy"])
        # every read asks for a depth: a null one reads as 0 on the phone
        # (two bare window nodes, Oct 4)
        for spec in ({}, {"tap": [1, 2]}):
            for m, params in mod.act_calls(spec):
                if m == "dumpWindowHierarchy":
                    self.assertEqual(params, [False, 50])

    def test_helper_click_text_falls_back_to_coordinates_when_the_selector_click_throws(self):
        mod = _u2mux()
        self.no_sleep(mod)
        dm = self._daemon(mod)
        dm.d = _FakeServer([SAMPLE_XML], screen_on=True)
        clicks = []

        class RPCUnknownError(Exception):  # three args and no __str__, as uiautomator2's
            pass

        def click(sel):
            clicks.append(sel)
            raise RPCUnknownError("Unknown RPC error: -32001 java.lang.NullPointerException",
                                  {"textMatches": "x"}, "\tat a.b.C.d(C.java:29)\n" * 40)
        dm.d.jsonrpc.click = click
        logged = []
        mod.log = lambda *a: logged.append(" ".join(str(x) for x in a))
        import types
        fake = types.ModuleType("uiautomator2._selector")
        fake.Selector = lambda **kw: kw
        exc = types.ModuleType("uiautomator2.exceptions")
        exc.UiObjectNotFoundError = type("UiObjectNotFoundError", (Exception,), {})
        pkg = types.ModuleType("uiautomator2")
        pkg._selector, pkg.exceptions = fake, exc
        with mock.patch.dict(sys.modules, {"uiautomator2": pkg, "uiautomator2._selector": fake,
                                           "uiautomator2.exceptions": exc}):
            out = json.loads(dm.cmd_click_text("OK"))
        self.assertEqual((out["x"], out["y"]), (250, 450))  # tier 3: the node's centre
        self.assertEqual(len(clicks), 1)
        line = [x for x in logged if x.startswith("click_text selector failed")][0]
        self.assertIn("NullPointerException", line)  # the head of the error, not its stack

    def test_helper_read_screen_asks_for_a_depth(self):
        mod = _u2mux()
        seen = []

        class Server:
            def jsonrpc_call(self, method, params, timeout=10):
                seen.append((method, params, timeout))
                return SAMPLE_XML
        self.assertEqual(mod.read_screen(Server()), SAMPLE_XML)
        self.assertEqual(seen, [("dumpWindowHierarchy", [False, 50], mod.DUMP_RPC_TIMEOUT)])

    def test_helper_ensure_server_kills_and_relaunches(self):
        mod = _u2mux()
        self.no_sleep(mod)
        devs = [_FakeServer([]), _FakeServer([SAMPLE_XML])]
        fake_u2 = SimpleNamespace(connect=lambda target: devs.pop(0))
        adb_dev = _FakeDev(PS_LISTING, "PID ARGS\n1 init\n")  # gone after the kill
        fake_adbutils = SimpleNamespace(adb=SimpleNamespace(device=lambda target: adb_dev))
        works = iter([False, True])
        mod.server_works = lambda d, words=False: next(works)
        closed, configured = [], []
        mod._KEEPALIVE = SimpleNamespace(close=lambda: closed.append(1))
        mod.apply_fast_config = lambda d: configured.append(d)
        with mock.patch.dict(sys.modules, {"uiautomator2": fake_u2, "adbutils": fake_adbutils}):
            d = mod.ensure_server()
        self.assertEqual(d.reads, [SAMPLE_XML])  # the relaunched device, returned
        self.assertEqual(adb_dev.shells, ["ps -A -o PID,ARGS", "kill -9 2843 2840 3200",
                                          "ps -A -o PID,ARGS", "dumpsys power | grep -m1 mWakefulness=", "input keyevent 224"])
        self.assertEqual(closed, [1])
        self.assertEqual(len(configured), 2)  # every server it connected to

    def test_helper_ensure_server_replaces_a_server_that_reads_no_words(self):
        mod = _u2mux()
        self.no_sleep(mod)
        # the running server reads two nodes and no word, twice: replaced at once
        fresh = _FakeServer([SAMPLE_XML])
        devs = [_FakeServer([MUTE_XML, MUTE_XML]), fresh]
        fake_u2 = SimpleNamespace(connect=lambda target: devs.pop(0))
        adb_dev = _FakeDev(PS_LISTING, "PID ARGS\n1 init\n")
        fake_adbutils = SimpleNamespace(adb=SimpleNamespace(device=lambda target: adb_dev))
        mod._KEEPALIVE = SimpleNamespace(close=lambda: None)
        mod.apply_fast_config = lambda d: None
        with mock.patch.dict(sys.modules, {"uiautomator2": fake_u2, "adbutils": fake_adbutils}):
            d = mod.ensure_server()
        self.assertIs(d, fresh)
        self.assertEqual(adb_dev.shells.count("kill -9 2843 2840 3200"), 1)
        # a fresh server that reads no words either is taken as it reads
        # (the spell rule replaces it mid-session if it stays that way):
        # one kill, one launch, the screen woken right before it
        fresh = _FakeServer([MUTE_XML])
        devs = [_FakeServer([MUTE_XML, MUTE_XML]), fresh]
        fake_u2 = SimpleNamespace(connect=lambda target: devs.pop(0))
        adb_dev = _FakeDev(PS_LISTING, "PID ARGS\n1 init\n")
        fake_adbutils = SimpleNamespace(adb=SimpleNamespace(device=lambda target: adb_dev))
        with mock.patch.dict(sys.modules, {"uiautomator2": fake_u2, "adbutils": fake_adbutils}):
            d = mod.ensure_server()
        self.assertIs(d, fresh)
        self.assertEqual(adb_dev.shells, ["ps -A -o PID,ARGS", "kill -9 2843 2840 3200",
                                          "ps -A -o PID,ARGS", "dumpsys power | grep -m1 mWakefulness=", "input keyevent 224"])

    def test_helper_restart_steps_in_order_and_a_wake_that_fails(self):
        mod = _u2mux()
        self.no_sleep(mod)
        events = []
        real_kill, real_wake = mod.kill_server_on_phone, mod.wake_phone
        mod.kill_server_on_phone = lambda dev: events.append("kill") or ["1"]
        mod.wake_phone = lambda dev: events.append("wake")
        mod._time.sleep = lambda s: events.append("sleep %g" % s)
        mod.server_works = lambda d, words=False: True
        mod._KEEPALIVE = SimpleNamespace(close=lambda: events.append("close"))
        mod.apply_fast_config = lambda d: None
        fake_u2 = SimpleNamespace(connect=lambda target: events.append("launch") or _FakeServer([SAMPLE_XML]))
        fake_adbutils = SimpleNamespace(adb=SimpleNamespace(device=lambda target: None))
        with mock.patch.dict(sys.modules, {"uiautomator2": fake_u2, "adbutils": fake_adbutils}):
            mod.ensure_server(force=True)
        # kill, drop the old streams, wake, let the app come back (the
        # screen state is unknown here: taken as off), launch
        self.assertEqual(events, ["kill", "close", "wake", "sleep 3", "launch"])
        # a screen that is on gets the short pause only
        events.clear()
        fake_adbutils = SimpleNamespace(adb=SimpleNamespace(device=lambda target: awake_dev))
        awake_dev = _FakeDev(PS_LISTING)
        awake_dev.awake = True
        with mock.patch.dict(sys.modules, {"uiautomator2": fake_u2, "adbutils": fake_adbutils}):
            mod.ensure_server(force=True)
        self.assertEqual(events, ["kill", "close", "wake", "sleep 1", "launch"])
        # a wake over adb that fails is logged and the restart goes on
        mod.wake_phone, mod.kill_server_on_phone = real_wake, real_kill
        logged = []
        mod.log = lambda *a: logged.append(" ".join(str(x) for x in a))

        class DeadInput(_FakeDev):
            def shell(self, cmd, timeout=None):
                if cmd.startswith("input"):
                    raise OSError("adb: device offline")
                return super().shell(cmd, timeout)
        fresh = _FakeServer([SAMPLE_XML])
        fake_u2 = SimpleNamespace(connect=lambda target: fresh)
        adb_dev = DeadInput(PS_LISTING, "PID ARGS\n1 init\n")
        fake_adbutils = SimpleNamespace(adb=SimpleNamespace(device=lambda target: adb_dev))
        with mock.patch.dict(sys.modules, {"uiautomator2": fake_u2, "adbutils": fake_adbutils}):
            d = mod.ensure_server(force=True)
        self.assertIs(d, fresh)
        self.assertTrue(any(line.startswith("wake over adb failed") for line in logged), logged)

    def test_helper_ensure_wants_words_only_in_an_unconfirmed_wordless_spell(self):
        mod = _u2mux()
        self.no_sleep(mod)
        asked = []
        mod.server_works = lambda d, words=False: asked.append(words) or True
        dm = self._daemon(mod)
        dm.d = _FakeServer([])
        self.assertTrue(dm._reconnect())
        dm._mute_since = 1.0
        self.assertTrue(dm._reconnect())
        dm._wordless_seen = True
        self.assertTrue(dm._reconnect())
        self.assertEqual(asked, [False, True, False])
        # a server that fails the check is replaced
        mod.server_works = lambda d, words=False: False
        reconnects = []
        dm.connect = lambda force=False: reconnects.append(force)
        self.assertTrue(dm._reconnect())
        self.assertEqual(reconnects, [False])

    def test_helper_ensure_server_gives_up_after_its_tries(self):
        mod = _u2mux()
        self.no_sleep(mod)
        fake_u2 = SimpleNamespace(connect=lambda target: _FakeServer([]))
        adb_dev = _FakeDev("PID ARGS\n1 init\n")
        fake_adbutils = SimpleNamespace(adb=SimpleNamespace(device=lambda target: adb_dev))
        mod.server_works = lambda d, words=False: False
        mod._KEEPALIVE = SimpleNamespace(close=lambda: None)
        with mock.patch.dict(sys.modules, {"uiautomator2": fake_u2, "adbutils": fake_adbutils}):
            with self.assertRaises(RuntimeError) as cm:
                mod.ensure_server()
        self.assertIn("couldn't be restarted: no server process was running", str(cm.exception))
        self.assertIn("(at first: it reads empty or wordless screens)", str(cm.exception))
        self.assertEqual(adb_dev.shells, ["ps -A -o PID,ARGS", "dumpsys power | grep -m1 mWakefulness=", "input keyevent 224"] * 2)

    def test_helper_connect_stamps_the_restart_even_when_it_fails(self):
        mod = _u2mux()
        self.no_sleep(mod)
        dm = self._daemon(mod)
        dm.d = None
        mod.install_keepalive = lambda: None
        mod._KEEPALIVE = SimpleNamespace(close=lambda: None)
        mod.ensure_server = lambda force=False: (_ for _ in ()).throw(RuntimeError("no phone"))
        with self.assertRaises(RuntimeError):
            mod.U2Daemon.connect(dm)
        self.assertGreater(dm._last_restart, 0)  # the cooldown runs from now
        self.assertFalse(mod.restart_allowed(mod._time.monotonic() - dm._last_restart))
        self.assertEqual(dm._restart_error, "RuntimeError: no phone")
        # a restart that works clears it (connect reads once to warm up)
        mod.ensure_server = lambda force=False: _FakeServer([SAMPLE_XML])
        mod.U2Daemon.connect(dm)
        self.assertIsNone(dm._restart_error)
        # a start on a wordless screen is not taken as "this screen has no
        # words": fresh servers at start can read wordless (19:22, Oct 4)
        # while the spell rule's relaunch mid-session reads every label
        mod.ensure_server = lambda force=False: _FakeServer([MUTE_XML])
        mod.U2Daemon.connect(dm)
        self.assertFalse(dm._wordless_seen)

    def test_helper_act_reports_a_failed_read_after_the_action(self):
        mod = _u2mux()
        self.no_sleep(mod)
        dm = self._daemon(mod)
        dm._last_xml, dm._last_xml_t = SAMPLE_XML, mod._time.monotonic()
        dm.d = _FakeServer([], screen_on=True)
        dm._batch = lambda calls, timeout=45.0: [None] * (len(calls) - 1) + [EMPTY_XML]
        dm._fix_blank_read = lambda xml, what: (_ for _ in ()).throw(RuntimeError("server gone"))
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"tap": [1, 2]}))
        self.assertTrue(str(cm.exception).startswith(
            "act failed after sending: the read after it failed (RuntimeError: server gone)"),
            str(cm.exception))
        # the screen was off when the action landed: probably dropped, say so
        dm._fix_blank_read = lambda xml, what: (SAMPLE_XML, True)
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"tap": [1, 2]}))
        self.assertIn("the screen was off, so it was probably dropped", str(cm.exception))
        # the wake before the action failed: the action may have been dropped
        dm._batch = lambda calls, timeout=45.0: [RuntimeError("no wake")] + \
            [None] * (len(calls) - 2) + [SAMPLE_XML]
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"key": 4}))
        self.assertIn("the wake before it failed (no wake), so it may have been dropped",
                      str(cm.exception))
        # the action's own failure is found by method, behind the wake
        dm._batch = lambda calls, timeout=45.0: [None, RuntimeError("no such element")] + \
            [SAMPLE_XML] * (len(calls) - 2)
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"tap": [250, 450]}))
        self.assertEqual(str(cm.exception), "act failed after sending: no such element")
        # a POWER key's blank read after is expected: no fix, no error, no cache
        dm._batch = lambda calls, timeout=45.0: [None] * (len(calls) - 1) + [SHADE_XML]
        self.assertEqual(dm.cmd_act(json.dumps({"key": 26})), SHADE_XML.encode())
        self.assertIsNone(dm._cache)

    def _daemon(self, mod):
        """A daemon object without a phone: no __init__, and a restart
        fails the test. It has its locks and an empty cache."""
        import threading
        dm = mod.U2Daemon.__new__(mod.U2Daemon)
        dm._lock, dm._cache_lock = threading.RLock(), threading.Lock()
        dm._gen, dm._cache = 0, None
        dm._last_restart = -1e9
        dm._restart_error = None
        dm._mute_since, dm._wordless_seen = None, False
        dm.connect = lambda force=False: self.fail("unexpected server restart")
        return dm

    def test_helper_screen_info_from_the_newest_read(self):
        mod = _u2mux()
        self.no_sleep(mod)
        self.assertEqual(mod.screen_of(SAMPLE_XML), (1080, 2400, "com.example"))
        dialog = ('<hierarchy rotation="0"><node package="com.android.settings"'
                  ' bounds="[84,900][1356,1700]"/></hierarchy>')
        self.assertIsNone(mod.screen_of(dialog))  # no edge node: no size
        self.assertIsNone(mod.screen_of(""))
        dm = self._daemon(mod)
        dm.d = _FakeServer([])  # no deviceInfo: asking the phone fails the test
        dm._last_xml, dm._last_xml_t = SAMPLE_XML, mod._time.monotonic()
        self.assertEqual(dm.cmd_screen(""), b"1080 2400 com.example")
        # an older read: the phone is asked
        dm._last_xml_t = mod._time.monotonic() - 100
        dm.d.jsonrpc.deviceInfo = lambda: {"displayWidth": 1440, "displayHeight": 3120,
                                           "currentPackageName": "com.other"}
        self.assertEqual(dm.cmd_screen(""), b"1440 3120 com.other")
        # a young read with only a dialog on it says nothing about the size
        dm._last_xml, dm._last_xml_t = dialog, mod._time.monotonic()
        self.assertEqual(dm.cmd_screen(""), b"1440 3120 com.other")

    def test_helper_fix_blank_read_wakes_an_off_screen(self):
        mod = _u2mux()
        self.no_sleep(mod)
        for blank in (EMPTY_XML, SHADE_XML):
            dm = self._daemon(mod)
            woke = []
            d = _FakeServer([SAMPLE_XML], screen_on=False)
            d.jsonrpc.wakeUp = lambda: woke.append(1)
            dm.d = d
            self.assertEqual(dm._fix_blank_read(blank, "blank read"), (SAMPLE_XML, True))
            self.assertEqual(woke, [1])
        # the screen stays off: said so, not an empty screen
        dm = self._daemon(mod)
        dm.d = _FakeServer([SHADE_XML], screen_on=False)
        with self.assertRaises(RuntimeError) as cm:
            dm._fix_blank_read(SHADE_XML, "blank read")
        self.assertIn("would not wake", str(cm.exception))
        # wakeUp itself failing is an error too
        dm = self._daemon(mod)
        dm.d = _FakeServer([], screen_on=False)
        dm.d.jsonrpc.wakeUp = lambda: (_ for _ in ()).throw(OSError("gone"))
        with self.assertRaises(RuntimeError) as cm:
            dm._fix_blank_read(SHADE_XML, "blank read")
        self.assertIn("wakeUp failed", str(cm.exception))

    def test_helper_fix_blank_read_keeps_a_bare_window_when_on(self):
        mod = _u2mux()
        self.no_sleep(mod)
        dm = self._daemon(mod)
        dm.d = _FakeServer([], screen_on=True)
        self.assertEqual(dm._fix_blank_read(SHADE_XML, "blank read"), (SHADE_XML, False))
        self.assertEqual(dm.d.calls, [])  # no re-read, no restart

    def test_helper_fix_blank_read_needs_the_server_to_answer(self):
        mod = _u2mux()
        self.no_sleep(mod)
        dm = self._daemon(mod)

        class Dead:
            @property
            def info(self):
                raise OSError("timed out")
        dm.d = Dead()
        with self.assertRaises(OSError):  # a dead server is not "screen on"
            dm._fix_blank_read(EMPTY_XML, "blank read")

    def test_helper_fix_blank_read_restarts_once(self):
        mod = _u2mux()
        self.no_sleep(mod)
        dm = self._daemon(mod)
        restarts = []

        def connect():
            restarts.append(1)
            dm._last_restart = mod._time.monotonic()
            dm.d = _FakeServer([SAMPLE_XML])
        dm.connect = connect
        dm.d = _FakeServer([])
        self.assertEqual(dm._fix_blank_read(EMPTY_XML, "blank read"), (SAMPLE_XML, False))
        self.assertEqual(restarts, [1])
        # right after a restart an empty read is kept, not restarted again
        dm.d = _FakeServer([])
        self.assertEqual(dm._fix_blank_read(EMPTY_XML, "blank read"), (EMPTY_XML, False))
        self.assertEqual((restarts, dm.d.calls), ([1], []))
        # a restart that failed a moment ago is reported, not papered over
        dm._restart_error = "RuntimeError: no phone"
        with self.assertRaises(RuntimeError) as cm:
            dm._fix_blank_read(EMPTY_XML, "blank read")
        self.assertIn("couldn't be restarted a moment ago: RuntimeError: no phone", str(cm.exception))
        self.assertEqual(restarts, [1])

    def test_helper_blank_reads_are_never_cached(self):
        mod = _u2mux()
        self.no_sleep(mod)
        dm = self._daemon(mod)
        dm._last_restart = mod._time.monotonic()  # inside the cooldown: kept
        dm.d = _FakeServer([EMPTY_XML, SAMPLE_XML], screen_on=True)
        self.assertEqual(dm._dump(), EMPTY_XML)
        self.assertIsNone(dm._cache)
        self.assertEqual(dm._dump(), SAMPLE_XML)  # read again, not served the blank
        self.assertEqual(dm._cache[1], SAMPLE_XML)
        # a wordless read (a screen loading) is kept but never cached either
        dm._cache = None
        dm.d = _FakeServer([MUTE_XML, SAMPLE_XML], screen_on=True)
        self.assertEqual(dm._dump(), MUTE_XML)
        self.assertIsNone(dm._cache)
        self.assertEqual(dm._dump(), SAMPLE_XML)
        self.assertEqual(dm._cache[1], SAMPLE_XML)
        # `wait_for ... absent` doesn't take a blank read for "gone"
        dm._cache = None
        dm.d = _FakeServer([EMPTY_XML, SAMPLE_XML], screen_on=True)
        out = json.loads(dm.cmd_wait_for(json.dumps({"text": "Nope", "absent": True, "timeout": 5})))
        self.assertTrue(out["gone"])
        self.assertEqual(out["polls"], 2)

    def test_helper_handle_does_not_reconnect_inside_the_cooldown(self):
        mod = _u2mux()
        self.no_sleep(mod)
        dm = self._daemon(mod)
        dm._last_restart = mod._time.monotonic()  # a restart a moment ago

        class Dead:
            @property
            def info(self):
                raise OSError("dead")
        dm.d = Dead()
        dm.cmd_dump = lambda arg: (_ for _ in ()).throw(OSError("dead"))
        with self.assertRaises(OSError):
            mod.U2Daemon.handle(dm, "dump")
        # outside the cooldown it reconnects once and retries
        dm._last_restart = -1e9
        reconnects = []
        dm.connect = lambda: reconnects.append(1)
        with self.assertRaises(OSError):
            mod.U2Daemon.handle(dm, "dump")
        self.assertEqual(reconnects, [1])


if __name__ == "__main__":
    unittest.main()
