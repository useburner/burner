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
        # No test writes the real run/commands.log, or the helper's log.
        for name in ("log_command", "_helper_log"):
            p = mock.patch.object(pc, name)
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
        self.assertIn('no match for "Nope" on the screen; `burner scroll down --to "Nope"` looks further down',
                      err.getvalue())
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
        # a row under another row on a page: the page's own business (a
        # tap by label asks the page what is under the point), no guess here
        plan = pc.plan_tap(pc.walk(ET.fromstring(COVERED_XML)), 1080, 2400, text="Box Score")
        self.assertEqual(plan["action"], "tap")
        native = COVERED_XML.replace("android.webkit.WebView", "android.widget.FrameLayout")
        plan = pc.plan_tap(pc.walk(ET.fromstring(native)), 1080, 2400, text="Box Score")
        self.assertEqual(plan["action"], "tap")  # later is on top on a native screen
        # a later row holding the point: a cover on a native screen, and
        # over a page too when it is Chrome's own (drawn after the WebView:
        # a banner, a prompt); a later row of the page itself is the
        # page's own business (its rows' order says nothing)
        later = WHOLE_XML.replace("</hierarchy>", "").rstrip()
        later = later[:later.rfind("</node>")] + (
            '<node text="Download today" class="android.view.View" package="com.android.chrome"'
            ' bounds="[0,400][1080,600]" clickable="true" enabled="true"/></node></hierarchy>')
        plan = pc.plan_tap(pc.walk(ET.fromstring(later)), 1080, 2400, text="Box Score")
        self.assertNotEqual((plan["action"], plan.get("moved")), ("tap", False))
        plan = pc.plan_tap(pc.walk(ET.fromstring(later.replace("android.webkit.WebView", "android.widget.FrameLayout"))),
                           1080, 2400, text="Box Score")
        self.assertNotEqual((plan["action"], plan.get("moved")), ("tap", False))
        inside = WHOLE_XML.replace(
            '<node text="" content-desc="Where to watch"',
            '<node text="Download today" class="android.view.View" package="com.android.chrome"'
            ' bounds="[0,400][1080,600]" clickable="true" enabled="true"/>\n      <node text="" content-desc="Where to watch"')
        plan = pc.plan_tap(pc.walk(ET.fromstring(inside)), 1080, 2400, text="Box Score")
        self.assertEqual((plan["action"], plan["xy"]), ("tap", (796, 491)))
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

    def test_tap_on_a_page_row_under_another_is_not_nudged(self):
        # the page's own check decides covers; the rows' guess cost 12s of
        # nudging over a banner (Oct 5): tapped where the row is
        self._tap_mocks(COVERED_XML)
        swipe = self.allow("_scrcpy_swipe", return_value=True)
        tc = self.allow("tap_center")
        args = self.parse(["tap", "Box Score", "--json", "--no-evidence"])
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_tap(args)
        self.assertEqual(rc, 0, "stdout=%r stderr=%r" % (out.getvalue(), err.getvalue()))
        swipe.assert_not_called()
        tc.assert_called_once_with(796, 491)

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
        self.allow("u2sock", return_value=None)  # the helper away: no look between steps
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
        self.allow("u2sock", return_value=None)
        with mock.patch.object(pc, "build_parser", return_value=stub):
            with self.cap():
                rc = pc.cmd_do(SimpleNamespace(
                    flow="step one; fail step; step three"))
        self.assertEqual(rc, 1)
        self.assertEqual(calls, ["step one", "fail step"])

    def test_cmd_do_bad_step(self):
        self.allow("u2sock", return_value=None)
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
            self.allow("u2sock", return_value=None)
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

# a search box holding the typed words beside the suggestion with them
TYPED_XML = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" package="com.android.chrome" bounds="[0,0][1080,2400]" clickable="false" enabled="true" focused="false" checked="false">
    <node text="Pixel 7" class="android.widget.EditText" package="com.android.chrome" bounds="[100,300][1000,420]" clickable="true" enabled="true" focused="true" checked="false"/>
    <node text="Pixel 7" class="android.widget.TextView" package="com.android.chrome" bounds="[100,450][1000,560]" clickable="true" enabled="true" focused="false" checked="false"/>
  </node>
</hierarchy>"""

FUZZY_AMBI_XML = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" clickable="false" enabled="true" focused="false" checked="false">
    <node text="OK, got it" class="android.widget.Button" bounds="[100,400][400,500]" clickable="true" enabled="true" focused="false" checked="false"/>
    <node text="OK, fine" class="android.widget.Button" bounds="[100,600][400,700]" clickable="true" enabled="true" focused="false" checked="false"/>
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

    def test_a_progress_bar_row_says_it_is_one(self):
        # Storage, Oct 7: "25.0 (540,540)" printed as a row with no word of
        # what it was
        bar = SAMPLE_XML.replace("</hierarchy>", '<node text="25.0" class="android.widget.ProgressBar" '
                                 'package="com.example" bounds="[100,500][980,580]"/></hierarchy>')
        lines, _ = pc.screen_lines(pc.walk(ET.fromstring(bar)), 1080, 2400)
        self.assertIn("25.0 [ProgressBar] (540,540)", lines)

    def test_a_loose_match_taps_only_a_row_the_words_name(self):
        # YouTube, Oct 7: `tap Search` on the results page, with no row
        # reading "Search", pressed "Search with your voice", and Android
        # asked to record audio
        for query, label, ok in (("Search", "Search with your voice", False),
                                 ("Delete", "Delete account", False),
                                 ("Sign in", "Sign in with Google", False),
                                 ("Battery", "Battery 79 percent.", False),
                                 ("ok", "Okay", False),  # a word inside a word
                                 ("Inbox", "Inbox, 3 unread", True),
                                 ("Echo Dot", "Echo Dot (5th Gen) | Smart speaker with Alexa | Charcoal", True),
                                 ("lofi hip hop radio", "lofi hip hop radio \U0001f4da beats to relax/study to - Lofi Girl", True),
                                 ("Wi-Fi", "Wi\u2011Fi, connected", True),
                                 ("Turn on", "Turn on now", True),
                                 # the review of Oct 7
                                 ("Turn off", "Don\u2019t turn off", False),
                                 ("turn off notifications", "Don't turn off notifications", False),
                                 ("Add", "Add-ons", False),
                                 ("Next", "Next >", True),
                                 ("Continue", "Continue \u2192", True),
                                 ("Starred", "\u2605 Starred", True),
                                 ("@tropoFarmer", "@tropoFarmer \u00b7 2h", True),
                                 ("#general", "#general, 5 new messages", True),
                                 ("Inbox", "Inbox\u20143 unread", True)):
            self.assertEqual(pc.fuzzy_ok(query, label), ok, (query, label))
            self.assertEqual(_u2mux().fuzzy_ok(query, label), ok, (query, label))
        voice = ('<hierarchy rotation="0"><node text="" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" '
                 'clickable="false" enabled="true">'
                 '<node text="lofi hip hop radio" class="android.widget.EditText" bounds="[150,150][850,270]" clickable="true" enabled="true"/>'
                 '<node text="" content-desc="Search with your voice" class="android.widget.ImageView" '
                 'bounds="[880,150][1000,270]" clickable="true" enabled="true"/></node></hierarchy>')
        plan = pc.plan_tap(pc.walk(ET.fromstring(voice)), 1080, 2400, text="Search")
        self.assertEqual((plan["action"], plan["near"]), ("nomatch", ["Search with your voice"]))
        rc, out, err, tc = self._tap(["tap", "Search"], xml=voice)
        self.assertEqual(rc, 1)
        self.assertIn('no row reads "Search"; rows with those words: "Search with your voice". '
                      'If one of these is the row you mean, tap it by its full words; else '
                      '`burner scroll down` and tap again', err)
        # `scroll --to` stops on any row holding the words ("Delivered" in
        # "Delivered Oct 5"), alternatives split (review of Oct 7)
        nodes = pc.walk(ET.fromstring(voice))
        self.assertEqual([n["desc"] for n in pc.rows_holding(nodes, "Search")], ["Search with your voice"])
        self.assertEqual([n["desc"] for n in pc.rows_holding(nodes, "Nope || Search with your voice")],
                         ["Search with your voice"])
        self.assertEqual([n["text"] for n in pc.rows_holding(nodes, "lofi hip hop radio")], ["lofi hip hop radio"])
        # a text field is taken loosely: a tap only focuses it (`type --field
        # Email` on "Email or phone" failed once taps matched strictly,
        # review of Oct 7)
        mod = _u2mux()
        email = voice.replace('text="lofi hip hop radio"', 'text="Email or phone"')
        plan = pc.plan_tap(pc.walk(ET.fromstring(email)), 1080, 2400, text="Email")
        self.assertEqual((plan["action"], plan["node"]["text"]), ("tap", "Email or phone"))
        self.assertEqual(mod.find_node(email, "Email", names=True)["text"], "Email or phone")
        self.assertEqual(mod.field_node(email, "Email")["text"], "Email or phone")
        self.assertIsNone(mod.field_node(email, "mail"))  # whole words
        self.assertIsNone(mod.find_node(email, "mail", names=True))
        self.assertIsNone(mod.field_node(email, "phone"))  # the head of its words (review of Oct 7)
        # a field's words while it has the focus may be what was typed: a
        # search box holding "password manager" took a password (review, Oct 7)
        typed = email.replace('text="Email or phone"', 'text="password manager" focused="true"')
        self.assertIsNone(mod.field_node(typed, "Password"))
        self.assertIsNone(mod.find_node(typed, "Password", names=True))
        self.assertEqual(pc.plan_tap(pc.walk(ET.fromstring(typed)), 1080, 2400, text="Password")["action"],
                         "nomatch")
        # unfocused, a secret's label names only a password field
        shown = typed.replace(' focused="true"', '')
        self.assertIsNone(mod.field_node(shown, "Password"))
        self.assertEqual(mod.field_node(shown.replace('class="android.widget.EditText"',
                                                      'class="android.widget.EditText" password="true"'),
                                        "Password")["text"], "password manager")
        # a sheet over the field: no loose match (its button took the tap)
        sheet = email.replace('</node></hierarchy>', '<node text="Continue as Zach" class="android.widget.Button" '
                              'bounds="[0,100][1080,400]" clickable="true" enabled="true"/></node></hierarchy>')
        self.assertIsNone(mod.field_node(sheet, "Email"))
        self.assertIsNone(mod.find_node(sheet, "Email", names=True))
        # "Add" is not "Address"; rows the words name come before a field
        addr = email.replace('text="Email or phone"', 'text="Address"')
        self.assertEqual(pc.plan_tap(pc.walk(ET.fromstring(addr)), 1080, 2400, text="Add")["action"], "nomatch")
        box = email.replace('text="Email or phone"', 'text="echo dot 5th gen"').replace(
            "Search with your voice", "Echo Dot (5th Gen) | Smart speaker")
        plan = pc.plan_tap(pc.walk(ET.fromstring(box)), 1080, 2400, text="Echo Dot")
        self.assertEqual((plan["action"], plan["node"]["desc"]), ("tap", "Echo Dot (5th Gen) | Smart speaker"))
        two = email.replace('</node></hierarchy>', '<node text="Email again" class="android.widget.EditText" '
                            'bounds="[150,400][850,520]" clickable="true" enabled="true"/></node></hierarchy>')
        self.assertIsNone(mod.field_node(two, "Email"))  # two fields hold it
        # an empty field reads its hint, focus or not: an auto-focused "Email
        # or phone" is named by `Email` (review of Oct 7) ...
        focused = email.replace('text="Email or phone"', 'text="Email or phone" focused="true"')
        self.assertEqual(mod.field_node(focused, "Email")["text"], "Email or phone")
        self.assertEqual(mod.find_node(focused, "Email", names=True)["text"], "Email or phone")
        plan = pc.plan_tap(pc.walk(ET.fromstring(focused)), 1080, 2400, text="Email")
        self.assertEqual((plan["action"], plan["node"]["text"]), ("tap", "Email or phone"))
        # ... but a field without the focus comes first (the focused one's
        # words may be what was typed)
        both = focused.replace('text="Email or phone" focused="true"', 'text="Email updates" focused="true"').replace(
            '</node></hierarchy>', '<node text="Email address" class="android.widget.EditText" '
            'bounds="[150,400][850,520]" clickable="true" enabled="true"/></node></hierarchy>')
        self.assertEqual(mod.field_node(both, "Email")["text"], "Email address")
        self.assertEqual(mod.find_node(both, "Email", names=True)["text"], "Email address")
        self.assertEqual(pc.plan_tap(pc.walk(ET.fromstring(both)), 1080, 2400, text="Email")["node"]["text"],
                         "Email address")
        # a code isn't a secret's word ("Verification code", "Zip code"); a
        # secret's label is one whatever its marks ("PIN:")
        code = email.replace('text="Email or phone"', 'text="Verification code, 6 digits"')
        self.assertEqual(mod.field_node(code, "Verification code")["text"], "Verification code, 6 digits")
        self.assertEqual(pc.plan_tap(pc.walk(ET.fromstring(code)), 1080, 2400, text="Verification code")["action"],
                         "tap")
        pin = email.replace('text="Email or phone"', 'text="PIN: 4 digits"')
        self.assertIsNone(mod.field_node(pin, "PIN:"))
        self.assertEqual(pc.plan_tap(pc.walk(ET.fromstring(pin)), 1080, 2400, text="PIN:")["action"], "nomatch")
        # rows named once each, in full; the advice quoted for the shell
        twice = voice.replace('</node></hierarchy>', '<node text="Search with your voice" '
                              'class="android.widget.TextView" bounds="[890,160][990,260]" clickable="false" '
                              'enabled="true"/></node></hierarchy>')
        self.assertEqual(pc.plan_tap(pc.walk(ET.fromstring(twice)), 1080, 2400, text="Search")["near"],
                         ["Search with your voice"])
        long_row = "Search " + "word " * 40
        longer = voice.replace("Search with your voice", long_row.strip())
        rc, out, err, tc = self._tap(["tap", "Search"], xml=longer)
        self.assertIn(long_row.strip()[:pc.LABEL_MAX - 1] + "\u2026", err)
        self.assertEqual(pc._shell_words("Search"), '"Search"')
        self.assertEqual(pc._shell_words('Say "hi"'), "'Say \"hi\"'")
        self.assertEqual(pc._shell_words("$4.99"), "'$4.99'")
        tc.assert_not_called()
        # --fuzzy asks for the loose match: taken
        rc, out, err, tc = self._tap(["tap", "--fuzzy", "Search"], xml=voice)
        self.assertEqual(rc, 0, err)
        tc.assert_called_once_with(940, 210)
        # a row the words name: tapped as before
        rc, out, err, tc = self._tap(["tap", "Search with"], xml=voice.replace(
            "Search with your voice", "Search with voice"))
        self.assertEqual(rc, 0, err)
        # the helper's click by text, its last tier: the same rule
        mod = _u2mux()
        self.assertIsNone(mod.find_node(voice, "Search", names=True))
        self.assertEqual(mod.find_node(voice, "Search")["desc"], "Search with your voice")  # a wait: loose
        self.assertEqual(mod.find_node(voice.replace("Search with your voice", "Search, voice"), "Search",
                                       names=True)["desc"], "Search, voice")

    def test_typed_words_in_a_field_are_not_its_label(self):
        rc, out, err, tc = self._tap(["tap", "Pixel 7"], xml=TYPED_XML)
        self.assertEqual(rc, 0, err)
        tc.assert_called_once_with(550, 505)  # the suggestion, not the box holding the words
        mod = _u2mux()
        self.assertEqual(mod.label_target(TYPED_XML, "Pixel 7"), (550, 505))
        node, alt = mod.label_node(TYPED_XML, "pixel 7")
        self.assertEqual((node["field"], alt), (False, "pixel 7"))
        only_box = TYPED_XML.replace('text="Pixel 7" class="android.widget.TextView"', 'text="Other" class="android.widget.TextView"')
        self.assertEqual(mod.label_target(only_box, "Pixel 7"), (550, 360))  # alone, the box is the row

    def test_the_tappable_row_among_same_words_is_the_control(self):
        # a button and plain text with the same words (Play's "Open"
        # button beside the word "Open" in the listing): the button
        headed = AMBI_XML.replace(
            '<node text="OK" class="android.widget.Button" bounds="[100,400][400,500]" clickable="true"',
            '<node text="OK" class="android.widget.TextView" bounds="[100,400][400,500]" clickable="false"')
        rc, out, err, tc = self._tap(["tap", "OK"], xml=headed)
        self.assertEqual(rc, 0, err)
        tc.assert_called_once_with(250, 650)  # the button, not the words above it
        mod = _u2mux()
        self.assertEqual(mod.label_target(headed, "OK"), (250, 650))
        # words inside a clickable row count as tappable: two such stay ambiguous
        carded = headed.replace(
            '<node text="OK" class="android.widget.TextView" bounds="[100,400][400,500]" clickable="false" enabled="true" focused="false" checked="false"/>',
            '<node text="" class="android.widget.LinearLayout" bounds="[50,380][450,520]" clickable="true" enabled="true" focused="false" checked="false">'
            '<node text="OK" class="android.widget.TextView" bounds="[100,400][400,500]" clickable="false" enabled="true" focused="false" checked="false"/></node>')
        rc, out, err, tc = self._tap(["tap", "OK"], xml=carded)
        self.assertEqual(rc, 1)
        self.assertIn("ambiguous tap", err)
        with self.assertRaises(RuntimeError):
            mod.label_target(carded, "OK")
        self.assertTrue(mod.can_tap(list(mod.iter_nodes(carded)), list(mod.iter_nodes(carded))[2]))

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
        calls = []
        self.allow("u2sock", side_effect=lambda cmd, arg="", timeout=30:
                   calls.append((cmd, json.loads(arg) if cmd == "act" else arg)) or ("ok" if cmd == "act" else "100"))
        args = self.parse(["tap", "--quiet", "@e2"])
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_tap(args)
        self.assertEqual(rc, 0)
        # one trip through the helper: the Cancel button's point, no screen back
        self.assertIn(("act", {"tap": [650, 450], "quiet": True, "idle": pc.IDLE_ACT_MS}), calls)
        tc.assert_not_called()
        self.assertIn("@e2", out.getvalue())
        # the helper away: the tap over scrcpy, as before
        self.allow("u2sock", return_value=None)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_tap(self.parse(["tap", "--quiet", "@e2"]))
        self.assertEqual(rc, 0)
        tc.assert_called_once_with(650, 450)  # Cancel button coords

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

    def test_apps_finds_the_apps_that_came_with_the_phone(self):
        # Oct 7: `burner apps calendar` said "not installed" (third-party
        # packages only), and Google Calendar ships with the phone
        launcher = ("48 activities found:\n  Activity #0:\n    priority=0 preferredOrder=0 match=0x108000\n"
                    "    com.android.chrome/com.google.android.apps.chrome.Main\n  Activity #1:\n"
                    "    com.google.android.calendar/com.android.calendar.AllInOneActivity\n"
                    "    com.google.android.apps.messaging/.ui.ConversationListActivity\n"
                    "    com.android.settings/.Settings\n")
        calls = []

        own = ["package:com.tinder", "package:com.vinted"]

        def adb(*args, timeout=30):
            calls.append(args)
            out = "\n".join(own) + "\n"
            if "query-activities" in " ".join(args):
                out += "<<launcher>>\n" + launcher
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        self.allow("adb_or_ensure", side_effect=adb)
        for name, want in (("calendar", "com.google.android.calendar\n"),
                           ("Google Calendar", "com.google.android.calendar\n"),
                           ("messages", "com.google.android.apps.messaging\n"),
                           ("tinder", "com.tinder\n"),
                           ("Google Chrome", "com.android.chrome\n")):
            calls.clear()
            with self.cap() as (out, err):
                rc = pc.cmd_apps(SimpleNamespace(match=name, all=False))
            self.assertEqual((rc, out.getvalue()), (0, want), name)
            self.assertEqual(len(calls), 1)  # both lists in one call
        # review, Oct 7: a third-party calendar took "google calendar" by its
        # last word, and Google Calendar was never looked for
        own.append("package:com.simplemobiletools.calendar.pro")
        own.append("package:com.here.app.maps")
        for name, want in (("Google Calendar", "com.google.android.calendar\n"),
                           ("calendar", "com.google.android.calendar\ncom.simplemobiletools.calendar.pro\n"),
                           ("Google Maps", "com.here.app.maps\n")):
            with self.cap() as (out, err):
                rc = pc.cmd_apps(SimpleNamespace(match=name, all=False))
            self.assertEqual((rc, out.getvalue()), (0, want), name)
        # Google Maps isn't there: another maker's maps app, and that said
        self.assertIn('no app holds every word of "Google Maps"; these hold "maps"', err.getvalue())
        self.assertEqual(pc.app_matches(["com.google.android.apps.maps", "com.here.app.maps"], "google maps"),
                         ["com.google.android.apps.maps"])
        # whole parts of the name first: Google Home's package holds
        # "chromecast", which listed it for "google chrome" (Oct 7)
        home = ["com.android.chrome", "com.google.android.apps.chromecast.app", "com.wispr.flowapp"]
        # both, and no note: which one is meant is the user's to say
        self.assertEqual(pc.app_match(home, "google chrome"),
                         (["com.android.chrome", "com.google.android.apps.chromecast.app"], None))
        # review, Oct 7: Microsoft's authenticator alone, with "no app holds
        # every word", while Google's was installed (authenticator2)
        auth = ["com.azure.authenticator", "com.google.android.apps.authenticator2"]
        self.assertEqual(pc.app_match(auth, "google authenticator"),
                         (["com.azure.authenticator", "com.google.android.apps.authenticator2"], None))
        self.assertEqual(pc.app_match(["com.android.vending", "com.example.store"], "google play store"),
                         (["com.android.vending"], None))  # "play store" as one phrase
        self.assertEqual(pc.app_match(["com.wallet.crypto.trustapp"], "google wallet"),
                         (["com.wallet.crypto.trustapp"], ["wallet"]))  # no Google one: said
        self.assertEqual(pc.app_matches(home, "chrome"), ["com.android.chrome"])
        self.assertEqual(pc.app_matches(home, "chromecast"), ["com.google.android.apps.chromecast.app"])
        self.assertEqual(pc.app_matches(home, "wispr flow"), ["com.wispr.flowapp"])  # inside a part, after
        self.assertEqual(pc.app_matches(["com.google.android.apps.docs", "com.google.android.gm"], "drive"),
                         ["com.google.android.apps.docs"])
        self.assertEqual(pc.app_matches(["com.example.notes"], "my notes"), [])
        self.assertEqual(pc.app_matches(["a.b"], "  "), [])
        with self.cap() as (out, err):
            rc = pc.cmd_apps(SimpleNamespace(match="snapchat", all=False))
        self.assertEqual((rc, out.getvalue()), (1, ""))
        self.assertEqual(pc.launchable_packages(launcher),
                         {"com.android.chrome", "com.google.android.calendar",
                          "com.google.android.apps.messaging", "com.android.settings"})

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

    def test_scroll_to_tells_the_list_s_end_from_a_dropped_swipe(self):
        # Oct 7, right after `burner update`: ten swipes moved nothing and
        # the search said "not found after 10 scrolls" (11.4s)
        top = pc.ET.fromstring('<hierarchy><node text="Apps" package="com.android.settings" '
                               'bounds="[0,100][1080,300]"/></hierarchy>')
        below = pc.ET.fromstring('<hierarchy><node text="Battery" package="com.android.settings" '
                                 'bounds="[0,100][1080,300]"/></hierarchy>')
        self.allow("ui_dump", return_value=top)
        steps = self.allow("_scroll_step", return_value=top)  # the swipe moved nothing
        adb = self.allow("_scroll_swipe")
        # the swipe over adb moves the list: the first was dropped, the search goes on
        self.allow("_settled_dump", return_value=below)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_scroll(SimpleNamespace(direction="down", times=1, to="Battery", quiet=True))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("found: Battery", out.getvalue())
        self.assertEqual((steps.call_count, adb.call_count), (1, 1))
        # it doesn't move either: the list's end, said after one scroll, not ten
        self.allow("_settled_dump", return_value=top)
        steps.reset_mock()
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_scroll(SimpleNamespace(direction="down", times=1, to="Battery", quiet=True))
        self.assertEqual((rc, steps.call_count), (1, 1))
        self.assertIn("not found: Battery (the list doesn't move any further down after 1 scroll)", err.getvalue())
        # the read after that swipe failed: no verdict on the list (review of Oct 7)
        self.allow("_settled_dump", return_value=None)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_scroll(SimpleNamespace(direction="down", times=1, to="Battery", quiet=True))
        self.assertEqual(rc, 1)
        self.assertIn("couldn't read the screen to look for Battery", err.getvalue())
        self.assertNotIn("doesn't move", err.getvalue())
        # the list's end still loading (a feed's spinner): a moment, a swipe
        # again, and what came is searched (review of Oct 7)
        loading = pc.ET.fromstring('<hierarchy><node text="Apps" package="com.android.settings" '
                                   'bounds="[0,100][1080,300]"/><node text="" class="android.widget.ProgressBar" '
                                   'package="com.android.settings" bounds="[500,2000][580,2080]"/></hierarchy>')
        self.allow("ui_dump", return_value=loading)
        steps = self.allow("_scroll_step", return_value=loading)
        reads = [loading, below]
        self.allow("_settled_dump", side_effect=lambda: reads.pop(0))
        settle = self.allow("settle_only")
        adb.reset_mock()
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_scroll(SimpleNamespace(direction="down", times=2, to="Battery", quiet=True))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual((adb.call_count, settle.call_count), (2, 1))
        self.assertTrue(pc.still_loading(pc.walk(loading)))
        for xml in ('<hierarchy><node text="25.0" class="android.widget.ProgressBar" bounds="[0,0][9,9]"/></hierarchy>',
                    '<hierarchy><node text="Loading dock" bounds="[0,0][9,9]"/></hierarchy>'):
            self.assertFalse(pc.still_loading(pc.walk(pc.ET.fromstring(xml))), xml)
        self.assertTrue(pc.still_loading(pc.walk(pc.ET.fromstring(
            '<hierarchy><node text="Loading more\u2026" bounds="[0,0][9,9]"/></hierarchy>'))))

    def test_scroll_to_names_the_alternative_found(self):
        # `--to "Nope || Next"` said `found: Nope || Next` (review of Oct 7)
        xml = '<hierarchy><node text="Next page" bounds="[0,0][10,10]"/></hierarchy>'
        self.allow("ui_dump", return_value=pc.ET.fromstring(xml))
        with self.cap() as (out, err):
            rc = pc.cmd_scroll(SimpleNamespace(direction="down", times=1, to="Nope || Next", quiet=True))
        self.assertEqual((rc, out.getvalue()), (0, 'found: Next (in "Next page")\n'))
        self.assertEqual(pc.holding_which(pc.walk(pc.ET.fromstring(xml)), "Nope"), (None, []))

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

    def test_a_screenshot_right_after_a_printed_screen_says_so(self):
        # through Muse, Oct 6-7: a screenshot after most steps, 1.2-2.7s
        # each, right after the screen's rows were printed
        import datetime
        now = 1_800_000_000.0
        stamp = lambda dt: datetime.datetime.fromtimestamp(now - dt).strftime("%Y-%m-%d %H:%M:%S")
        line = lambda dt, cmd, rc=0: "%s   1523ms exit %d burner %s\n" % (stamp(dt), rc, cmd)
        self.assertIn("`burner tap` printed this screen's rows", pc.shot_hint([line(5, "tap Search")], now))
        self.assertIn("`burner type`", pc.shot_hint([line(3, "type --field Search '\u2026'")], now))
        self.assertIn("`burner open`", pc.shot_hint([line(3, "open https://x.com")], now))
        # quiet or --json: no rows were printed (review of Oct 7)
        for cmd in ("tap Search -q", "tap Search --quiet", "--json tap Search", "press back --json"):
            self.assertEqual(pc.shot_hint([line(3, cmd)], now), "", cmd)
        for lines in ([line(60, "tap Search")],          # long ago
                      [line(5, "tap Search", rc=1)],     # it failed: no screen printed
                      [line(5, "shot --out /tmp/a.png")],  # a screenshot again
                      [line(5, "log --last 20")], []):
            self.assertEqual(pc.shot_hint(lines, now), "", lines)
        # on the command: stderr, the path alone on stdout
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.jpg")
            self.allow("shot_fast", return_value=path)
            with mock.patch.object(pc, "_shot_hint_now", return_value="burner: the hint"), \
                    self.cap() as (out, err):
                self.assertEqual(pc.cmd_shot(self.parse(["shot", "--out", path])), 0)
        self.assertEqual(out.getvalue(), os.path.abspath(path) + "\n")
        self.assertEqual(err.getvalue(), "burner: the hint\n")

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
        installed = SimpleNamespace(stdout="package:/data/app/x/base.apk\n", stderr="", returncode=0)
        adb = self.allow("adb_or_ensure", side_effect=lambda *a, **k: installed if a[1:3] == ("pm", "path") else other)
        self.allow("u2_invalidate")
        with self.cap() as (out, err):
            rc = pc.cmd_start(SimpleNamespace(package="com.example"))
        self.assertEqual(rc, 1)
        self.assertEqual(adb.call_count, 3)  # two launches, then the verdict's `pm path`
        self.assertIn("KEYCODE_HOME", adb.call_args_list[1][0][1])
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
        # between its steps a chain only asks the helper for its newest
        # read (no round trip to the phone), to see a question the phone asks
        seen = []
        looks = self.allow("u2sock", return_value="")

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
        self.assertEqual([c[0] for c in looks.call_args_list], [("dump", "cached")] * 2)

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
        self.assertEqual(pc.focus_field("Date picker"), "settled")
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

    def test_start_fresh_opens_the_first_screen_through_adb(self):
        # --fresh: adb's launch clears the app's task (the scrcpy helper's
        # launch can only resume, or force-stop first, which kills a
        # download or a playback in progress)
        sc = self.allow("scrcpy_send", return_value=True)
        adb = self.allow("adb_or_ensure", return_value=SimpleNamespace(
            stdout="  mFocusedApp=ActivityRecord{1 u0 com.example/.Main t3}\n",
            stderr="", returncode=0))
        self.allow("u2_invalidate")
        self.allow("nav_record")
        self.allow("u2sock", return_value="100")
        with mock.patch.object(pc.time, "sleep"), self.cap():
            pc.cmd_start(SimpleNamespace(package="com.example", quiet=True, fresh=True))
        self.assertIn("-f 0x10008000", adb.call_args[0][1])  # NEW_TASK|CLEAR_TASK
        sc.assert_not_called()
        self.assertTrue(self.parse(["start", "com.example", "--fresh"]).fresh)

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
        self.allow("scrcpy_send", return_value=False)  # Chrome started through the scrcpy helper: not here
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
        self.assertEqual((calls[1][0], calls[1][1].split()[0]), ("dump", "page"))  # the page asked first (an older helper's reply: not a page)
        self.assertEqual(calls[2], ("act", '{"wake": true, "idle": 1000}'))  # the screen woken first
        self.assertEqual(dump.call_count, 1)  # one re-read of a thin screen
        self.assertIn("(may still be loading)", out.getvalue())


# --------------------------------- 15. one round trip per action

def _u2mux():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "u2mux_under_test", os.path.join(ROOT, "lib", "u2", "u2mux.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # its log() writes run/u2mux.log: the tests' lines go elsewhere (they
    # had read as the phone's, review of Oct 7)
    mod.LOG_FILE = os.path.join(tempfile.gettempdir(), "burner-tests-u2mux.log")
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

    def test_a_lost_or_failed_act_claims_nothing_on_stdout(self):
        self.allow("wake_async", return_value=mock.Mock())
        self.allow("ui_dump", return_value=ET.fromstring(TAP_XML))
        self.allow("tap_center")
        self.allow("u2_invalidate")
        self.allow("nav_record")

        def lost(cmd, arg="", timeout=30):
            pc._u2_status = "lost"
            return None
        self.allow("u2sock", side_effect=lost)
        with self.cap() as (out, err):
            rc = pc.cmd_tap(self.parse(["tap", "Not now"]))
        self.assertEqual((rc, out.getvalue().strip()), (1, ""))
        self.assertIn("didn't confirm the tap", err.getvalue())
        with self.cap() as (out, err):
            rc = pc.cmd_press(self.parse(["press", "BACK"]))
        self.assertEqual((rc, out.getvalue().strip()), (1, ""))
        self.assertIn("didn't confirm the key press", err.getvalue())
        with self.cap() as (out, err):
            rc = pc.cmd_type(self.parse(["type", "--field", "Search", "hello"]))
        self.assertEqual((rc, out.getvalue().strip()), (1, ""))
        self.assertIn("didn't confirm the typing", err.getvalue())

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

    def test_enter_goes_only_with_somewhere_to_type(self):
        # a check through Muse, Oct 7: a type that failed, then Enter, on
        # YouTube's account list: "Add account" had the focus, and Google's
        # sign-in opened
        self.allow("u2_invalidate")
        self.allow("nav_record")
        sc = self.allow("scrcpy_send", return_value=True)
        adb = self.allow("adb_or_ensure")
        self.assertTrue(pc.enter_target(pc.walk(ET.fromstring(SAMPLE_XML))))  # a focused field
        self.assertFalse(pc.enter_target(pc.walk(ET.fromstring(TAP_XML))))
        kbd = TAP_XML.replace("</hierarchy>", '<node text="" class="android.widget.FrameLayout" '
                              'package="com.google.android.inputmethod.latin" bounds="[0,1600][1080,2400]"/></hierarchy>')
        self.assertTrue(pc.enter_target(pc.walk(ET.fromstring(kbd))))  # the keyboard up: a page's field
        calls = []
        screen = [TAP_XML]

        def u2(cmd, arg="", timeout=30):
            calls.append((cmd, arg))
            return screen[0]
        self.allow("u2sock", side_effect=u2)
        for key in ("enter", "ENTER", "KEYCODE_ENTER", "66", "DPAD_CENTER", "NUMPAD_ENTER", "SPACE"):
            calls.clear()
            with self.cap() as (out, err):
                rc = pc.cmd_press(self.parse(["press", key]))
            self.assertEqual(rc, 1, key)
            self.assertIn("the screen's newest read shows no text field with the focus and no keyboard",
                          err.getvalue())
            self.assertIn("nothing was pressed", err.getvalue())
            self.assertIn("`--force` sends the key anyway", err.getvalue())
            # the helper's newest read, then the screen reader's now; no key sent
            self.assertEqual(calls, [("dump", "cached"), ("dump", "native")], key)
        sc.assert_not_called()
        adb.assert_not_called()
        # a text field with the focus: the key goes, in one round trip
        screen[0] = SAMPLE_XML
        calls.clear()
        with self.cap() as (out, err):
            rc = pc.cmd_press(self.parse(["press", "enter"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual([(c, json.loads(a)) for c, a in calls if c == "act"], [("act", {"key": 66, "idle": 1200})])
        # other keys never ask
        screen[0] = TAP_XML
        calls.clear()
        with self.cap() as (out, err):
            rc = pc.cmd_press(self.parse(["press", "BACK"]))
        self.assertEqual((rc, [c for c, _ in calls]), (0, ["act"]))
        # --force sends it whatever the read shows
        calls.clear()
        with self.cap() as (out, err):
            rc = pc.cmd_press(self.parse(["press", "enter", "--force"]))
        self.assertEqual((rc, [c for c, _ in calls]), (0, ["act"]))
        # a page's read in Chrome (no keyboard, the address bar never
        # focused there): the screen reader's read decides (review, Oct 7)
        page = '<hierarchy rotation="0" page="1">' + TAP_XML.split(">", 1)[1]
        native = [kbd]

        def u2_page(cmd, arg="", timeout=30):
            calls.append((cmd, arg))
            return native[0] if (cmd, arg) == ("dump", "native") else (page if cmd == "dump" else SAMPLE_XML)
        self.allow("u2sock", side_effect=u2_page)
        calls.clear()
        with self.cap() as (out, err):
            rc = pc.cmd_press(self.parse(["press", "enter"]))
        self.assertEqual((rc, [(c, a) for c, a in calls][:2]), (0, [("dump", "cached"), ("dump", "native")]))
        native[0] = TAP_XML  # the screen reader sees no field and no keyboard either: refused
        calls.clear()
        with self.cap() as (out, err):
            rc = pc.cmd_press(self.parse(["press", "enter"]))
        self.assertEqual((rc, [c for c, _ in calls]), (1, ["dump", "dump"]))
        native[0] = page  # a page's read again (defensive: can't tell), the key goes
        self.allow("ui_dump", return_value=ET.fromstring(page))
        with self.cap() as (out, err):
            self.assertEqual(pc.cmd_press(self.parse(["press", "enter"])), 0)
        self.assertEqual(err.getvalue(), "")
        # the screen can't be read: the key goes, and a line says why
        self.allow("_cached_root", return_value=None)
        self.allow("ui_dump", side_effect=RuntimeError("the phone is unreachable"))
        self.allow("u2sock", side_effect=lambda cmd, arg="", timeout=30: None if cmd == "dump" else SAMPLE_XML)
        with self.cap() as (out, err):
            self.assertEqual(pc.cmd_press(self.parse(["press", "enter"])), 0)
        self.assertIn("couldn't be read to look for a text field (the phone is unreachable); the key goes",
                      err.getvalue())

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
        self.allow("scrcpy_send", return_value=False)  # Chrome started through the scrcpy helper: not here
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
                                 ("act", {"wake": True, "idle": 1000})])
        self.assertIn("screen: com.example", out.getvalue())

    def test_a_thin_launch_read_is_read_again(self):
        # Oct 7: `burner settings home` read Settings as its search bar
        # alone; its list came a moment later
        self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.allow("u2_invalidate")
        thin = ('<hierarchy rotation="0"><node text="Search Settings" class="android.widget.TextView" '
                'package="com.android.settings" bounds="[100,200][900,300]" clickable="true" enabled="true"/>'
                '</hierarchy>')
        full = thin.replace("</hierarchy>", "".join(
            '<node text="%s" class="android.widget.TextView" package="com.android.settings" '
            'bounds="[100,%d][900,%d]" clickable="true" enabled="true"/>' % (w, 400 + i * 150, 500 + i * 150)
            for i, w in enumerate(("Network and internet", "Connected devices", "Apps", "Battery"))) + "</hierarchy>")
        reads, calls = [thin, full], []
        self.allow("u2sock", side_effect=lambda cmd, arg="", timeout=30: calls.append(cmd) or reads.pop(0))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_settings(self.parse(["settings", "home"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("Battery", out.getvalue())
        self.assertEqual(calls, ["act", "act"])
        # a full read: once
        reads[:], calls[:] = [full], []
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            pc.cmd_settings(self.parse(["settings", "home"]))
        self.assertEqual(calls, ["act"])

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
        # a row the words only named: the helper's read says its words
        self.allow("u2sock", side_effect=lambda cmd, arg="", timeout=30: SAMPLE_XML.replace(
            '<hierarchy rotation="0">', '<hierarchy tapped="Search, Tab 2 of 4" rotation="0">'))
        with self.cap() as (out, err):
            self.assertEqual(pc.cmd_tap(self.parse(["tap", "Search"])), 0)
        self.assertIn('tapped "Search, Tab 2 of 4" (label)', out.getvalue())

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
        tapped = SAMPLE_XML.replace('text="Hello"', 'text="Hello, tapped"')  # the screen after each tap

        def batch(calls, timeout=45.0):
            sent.append(calls)
            return [None] * (len(calls) - 1) + [tapped]
        dm._batch = batch
        # the newest read is young and has the row once, whole and clear:
        # the phone finds it by its words at tap time, in the one round
        # trip with the wait and the read (no read first)
        dm.cmd_act(json.dumps({"tap_label": "OK"}))
        self.assertEqual(sent[0][2][0], "click")
        self.assertEqual(sent[0][2][1][0]["text"], "OK")
        self.assertEqual([m for m, _ in sent[0]], ["wakeUp", "count", "click", "waitForIdle", "dumpWindowHierarchy"])
        self.assertEqual(dm.d.calls, [])
        # the phone no longer finds it (the screen changed): nothing was
        # tapped, the row is read afresh and tapped where it is now
        def batch_miss(calls, timeout=45.0):
            sent.append(calls)
            if isinstance(calls[1][1][0], dict):
                return [None, 0, RuntimeError("UiObjectNotFoundException")] + [None] * (len(calls) - 4) + [tapped]
            return [None] * (len(calls) - 1) + [tapped]
        dm._batch = batch_miss
        dm.cmd_act(json.dumps({"tap_label": "OK"}))
        self.assertEqual(sent[-1][1], ("click", [250, 450]))
        self.assertEqual(dm.d.calls, ["dumpWindowHierarchy"])  # one fresh read, which agreed
        # any other error from the click may have come after the touch went
        # in (a NullPointerException on a web node, Oct 4): no second tap
        def batch_npe(calls, timeout=45.0):
            sent.append(calls)
            return [None, 1, RuntimeError("java.lang.NullPointerException")] + [None] * (len(calls) - 4) + [SAMPLE_XML]
        dm._batch = batch_npe
        dm.d = _FakeServer([], screen_on=True)
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"tap_label": "OK"}))
        self.assertTrue(str(cm.exception).startswith("act failed after sending: java.lang.NullPointerException"))
        # rows with the words came since the read: the first was tapped, and it says so
        def batch_two(calls, timeout=45.0):
            sent.append(calls)
            return [None, 2] + [None] * (len(calls) - 3) + [SAMPLE_XML]
        dm._batch = batch_two
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"tap_label": "OK"}))
        self.assertEqual(str(cm.exception), "act failed after sending: 2 rows read 'OK' now; the first was tapped")
        dm._batch = batch
        # an older read: the row is read afresh; a native row is then
        # tapped by its words, the phone finding it where it is at tap time
        # (no second read for its stillness: 2.7-3.1s taps, Oct 6)
        dm._last_xml_t = 0
        dm.d = _FakeServer([SAMPLE_XML], screen_on=True)
        dm.cmd_act(json.dumps({"tap_label": "OK"}))
        self.assertEqual([m for m, _ in sent[-1]], ["wakeUp", "count", "click", "waitForIdle", "dumpWindowHierarchy"])
        self.assertEqual(sent[-1][2][1][0]["text"], "OK")
        self.assertEqual(dm.d.calls, ["dumpWindowHierarchy"])
        # a native row that moved since the assistant's read: the same, one read
        moved = SAMPLE_XML.replace("[100,400][400,500]", "[100,440][400,540]")
        dm.d = _FakeServer([moved], screen_on=True)
        dm._last_xml_t = 0
        dm.cmd_act(json.dumps({"tap_label": "OK"}))
        self.assertEqual(sent[-1][2][0], "click")
        self.assertEqual(dm.d.calls, ["dumpWindowHierarchy"])
        # a web page's rows report their old place for a moment after a
        # scroll, and the phone's own click threw on a web node (Oct 4): a
        # web row is read until two reads agree on its place, three at most,
        # and tapped there
        web = lambda x: x.replace('class="android.widget.FrameLayout" package="com.example"',
                                  'class="android.webkit.WebView" package="com.example"', 1)
        moved, moved2 = web(moved), web(SAMPLE_XML.replace("[100,400][400,500]", "[100,460][400,560]"))
        dm._last_xml, dm._last_xml_t = web(SAMPLE_XML), 0
        dm.d = _FakeServer([moved, moved2, moved2], screen_on=True)
        dm.cmd_act(json.dumps({"tap_label": "OK"}))
        self.assertEqual(sent[-1][1], ("click", [250, 510]))
        self.assertEqual(dm.d.calls, ["dumpWindowHierarchy"] * 3)
        # still moving after three reads: the newest place is tapped
        moved3 = web(SAMPLE_XML.replace("[100,400][400,500]", "[100,480][400,580]"))
        dm._last_xml, dm._last_xml_t = web(SAMPLE_XML), 0
        dm.d = _FakeServer([moved, moved2, moved3], screen_on=True)
        dm.cmd_act(json.dumps({"tap_label": "OK"}))
        self.assertEqual(sent[-1][1], ("click", [250, 530]))
        self.assertEqual(dm.d.calls, ["dumpWindowHierarchy"] * 3)
        # the assistant's read didn't have the row whole (cut off at an
        # edge, then nudged): two reads that agree, then the tap
        dm._last_xml = CLIPPED_XML
        dm.d = _FakeServer([web(SAMPLE_XML), web(SAMPLE_XML)], screen_on=True)
        dm.cmd_act(json.dumps({"tap_label": "OK"}))
        self.assertEqual(sent[-1][1], ("click", [250, 450]))
        self.assertEqual(dm.d.calls, ["dumpWindowHierarchy"] * 2)
        # the read before the tap fails: nothing was sent, and it says so
        dm.d = _FakeServer([], screen_on=True)
        dm._last_xml_t = 0
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"tap_label": "OK"}))
        self.assertTrue(str(cm.exception).startswith("act not sent: the read before it failed"),
                        str(cm.exception))
        # a quiet tap (a step of `burner do`): the tap, the wait for the
        # UI to go quiet and a read in the one trip; no screen sent back,
        # and the read is the newest one known (the next step finds its
        # control by words on the screen after this tap: a calculator's
        # display reads "1" once the 1 key is tapped)
        after = SAMPLE_XML.replace('text="Hello"', 'text="Hello after"')
        dm._last_xml, dm._last_xml_t = SAMPLE_XML, mod._time.monotonic()
        dm.d = _FakeServer([SAMPLE_XML], screen_on=True)
        sent.clear()
        dm._batch = lambda calls, timeout=45.0: sent.append(calls) or [None] * (len(calls) - 1) + [after]
        # by words, from the young read: the phone waits for the tap's
        # acknowledgement itself, so no read stands in for the pause
        self.assertEqual(dm.cmd_act(json.dumps({"tap_label": "OK", "quiet": True, "idle": 1200})), b"ok")
        self.assertEqual([m for m, _ in sent[0]], ["wakeUp", "count", "click", "waitForIdle", "dumpWindowHierarchy"])
        self.assertEqual(sent[0][2][1][0]["text"], "OK")
        self.assertEqual(dm.d.calls, [])
        self.assertEqual(dm._last_xml, after)
        self.assertLess(mod._time.monotonic() - dm._last_xml_t, 5.0)
        # from an older read: the fresh read, then the row tapped by its
        # words (the phone finds it where it is at tap time), the wait and
        # the read; no screen sent back
        dm._last_xml_t = 0
        self.assertEqual(dm.cmd_act(json.dumps({"tap_label": "OK", "quiet": True, "idle": 1200})), b"ok")
        self.assertEqual([m for m, _ in sent[1]],
                         ["wakeUp", "count", "click", "waitForIdle", "dumpWindowHierarchy"])
        self.assertEqual(sent[1][2][1][0]["text"], "OK")
        self.assertEqual(dm.d.calls, ["dumpWindowHierarchy"])
        self.assertEqual(dm._last_xml, after)
        self.assertEqual([m for m, _ in mod.act_calls({"key": 66, "quiet": True, "idle": 500})],
                         ["wakeUp", "pressKeyCode", "dumpWindowHierarchy", "waitForIdle", "dumpWindowHierarchy"])
        # a blank read after a quiet step is not kept: the next step reads afresh
        dm._batch = lambda calls, timeout=45.0: sent.append(calls) or [None] * len(calls)
        dm._last_xml, dm._last_xml_t = SAMPLE_XML, mod._time.monotonic()
        self.assertEqual(dm.cmd_act(json.dumps({"tap_label": "OK", "quiet": True, "idle": 1200})), b"ok")
        self.assertEqual((dm._last_xml, dm._last_xml_t), (SAMPLE_XML, 0.0))


class HelperStalenessTests(OfflineTestCase):
    def test_a_command_waits_for_a_helper_that_is_starting(self):
        attempts = []

        class Sock:
            def settimeout(self, t):
                pass

            def connect(self, path):
                attempts.append(path)
                if len(attempts) < 4:
                    raise OSError("no socket yet")
        with mock.patch.object(pc, "helper_starting", return_value=True), \
                mock.patch.object(pc, "_helper_waited", False), \
                mock.patch.object(pc.time, "sleep") as sl:
            pc.connect_helper(Sock(), 5)
            self.assertEqual(len(attempts), 4)  # three misses, then the socket
            self.assertEqual(sl.call_count, 3)
            # the wait was had once: a start that never comes isn't waited for again
            attempts.clear()
            with self.assertRaises(OSError):
                pc.connect_helper(Sock(), 5)
            self.assertEqual(len(attempts), 1)
        # no start under way: no wait at all
        attempts.clear()
        with mock.patch.object(pc, "helper_starting", return_value=False), \
                mock.patch.object(pc, "_helper_waited", False), \
                mock.patch.object(pc.time, "sleep") as sl, self.assertRaises(OSError):
            pc.connect_helper(Sock(), 5)
        self.assertEqual((len(attempts), sl.call_count), (1, 0))

    def test_a_dead_helper_s_socket_is_cleared_and_the_helper_started_again(self):
        # the helper died and left its socket file (Oct 5): connecting was
        # refused, nothing restarted it, twenty minutes of 12s scrolls
        attempts, removed, started = [], [], []

        class Sock:
            def settimeout(self, t):
                pass

            def connect(self, path):
                attempts.append(path)
                if len(attempts) < 3:
                    raise ConnectionRefusedError("nobody listens")
        # even right after a start (the stamp says one is under way): a
        # helper that listens has its socket, so a refused one is dead
        with mock.patch.object(pc, "helper_starting", return_value=True),                 mock.patch.object(pc, "helper_alive", return_value=False),                 mock.patch.object(pc, "_helper_waited", False),                 mock.patch.object(pc.os.path, "exists", lambda path: path == pc.U2_SOCK),                 mock.patch.object(pc.os, "remove", lambda path: removed.append(path)),                 mock.patch.object(pc, "u2_start_background", lambda force=False: started.append(force)),                 mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            pc.connect_helper(Sock(), 5)
        self.assertEqual((len(attempts), started, sorted(removed)), (3, [True], sorted([pc.U2_SOCK, pc.U2_PID])))
        self.assertIn("the UI helper wasn't running; starting it again", err.getvalue())
        # no socket file at all (a first run): the slow way, no start here
        attempts.clear()
        with mock.patch.object(pc, "helper_starting", return_value=False),                 mock.patch.object(pc, "_helper_waited", False),                 mock.patch.object(pc.os.path, "exists", return_value=False),                 mock.patch.object(pc, "u2_start_background", lambda force=False: started.append(2)),                 self.assertRaises(OSError):
            pc.connect_helper(Sock(), 5)
        self.assertEqual((len(attempts), started), (1, [True]))

    def test_a_pid_that_came_back_as_another_process_is_no_helper(self):
        # after a reboot the pid file's number can name another process: a
        # refused socket then read as a busy helper, never started again
        # (review of Oct 7)
        pid_file = os.path.join(tempfile.mkdtemp(), "u2-mux.pid")
        with open(pid_file, "w") as f:
            f.write("4242")
        with mock.patch.object(pc, "U2_PID", pid_file), mock.patch.object(pc.os, "kill") as kill:
            for command, alive in (("python3 /home/hatch/burner/lib/u2/u2mux.py", True),
                                   ("/usr/bin/vim notes.txt", False),
                                   ("", True)):  # can't be read: taken for the helper
                with mock.patch.object(pc, "_process_command", return_value=command):
                    self.assertEqual(pc.helper_alive(), alive, command)
            kill.side_effect = ProcessLookupError()
            self.assertFalse(pc.helper_alive())  # no such process
        with mock.patch.object(pc, "U2_PID", pid_file + ".none"):
            self.assertFalse(pc.helper_alive())  # no pid file

    def test_a_busy_helper_is_waited_for_not_replaced(self):
        # Oct 7: a helper relinking the phone took no call for a moment; the
        # CLI took it for dead and started a second one (a 20.7s `open`)
        attempts, removed, started, notes = [], [], [], []

        class Sock:
            def settimeout(self, t):
                pass

            def connect(self, path):
                attempts.append(path)
                if len(attempts) < 3:
                    raise BlockingIOError(11, "Resource temporarily unavailable")
        with mock.patch.object(pc, "helper_starting", return_value=False), \
                mock.patch.object(pc, "_helper_waited", False), \
                mock.patch.object(pc.os.path, "exists", lambda path: path == pc.U2_SOCK), \
                mock.patch.object(pc.os, "remove", lambda path: removed.append(path)), \
                mock.patch.object(pc, "u2_start_background", lambda force=False: started.append(force)), \
                mock.patch.object(pc, "_helper_log", notes.append), \
                mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            pc.connect_helper(Sock(), 5)
        self.assertEqual((len(attempts), started, removed), (3, [], []))
        self.assertIn("didn't take a connection (BlockingIOError); waiting for it", notes[0])
        self.assertNotIn("starting it again", err.getvalue())

    def test_helper_restarts_when_any_file_under_lib_u2_is_newer(self):
        # the helper loads cdp.py once: an update that changed only the
        # page scripts must restart it too
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = os.path.join(tmp.name, "u2")
        os.makedirs(src)
        pid = os.path.join(tmp.name, "u2-mux.pid")
        sock = os.path.join(tmp.name, "u2-mux.sock")
        with open(pid, "w") as f:
            f.write("4242")
        now = pc.time.time()
        for name, age in (("u2mux.py", 100), ("cdp.py", 100)):
            with open(os.path.join(src, name), "w") as f:
                f.write("# source")
            os.utime(os.path.join(src, name), (now - age, now - age))
        os.utime(pid, (now - 50, now - 50))
        self.allow("u2_start_background")
        ps = SimpleNamespace(stdout="python3 lib/u2/u2mux.py\n")
        with mock.patch.object(pc, "U2_SRC_DIR", src), mock.patch.object(pc, "U2_PID", pid), \
                mock.patch.object(pc, "U2_SOCK", sock), mock.patch.object(pc.os, "kill") as kill, \
                mock.patch.object(pc.subprocess, "run", return_value=ps):
            pc._u2_checked = False
            pc.u2_restart_if_stale()
            kill.assert_not_called()  # both older than the pid file
            os.utime(os.path.join(src, "cdp.py"), (now - 10, now - 10))
            with open(pid, "w") as f:
                f.write("4242")
            os.utime(pid, (now - 50, now - 50))
            pc._u2_checked = False
            pc.u2_restart_if_stale()
            kill.assert_called_once_with(4242, 15)
            self.assertFalse(os.path.exists(pid))
        pc._u2_checked = True


def _record(key, flags, when, words, public=None, extras_indent=16, when_line=True, marks=True):
    """One record as `dumpsys notification --noredact` prints it (AOSP's
    NotificationRecord.dump): the head, its fields, the notification, its
    lock screen version. `words` are extras lines (key, raw value)."""
    pkg = key.split("|")[1]
    pad = " " * extras_indent
    lines = ["    NotificationRecord(0x0123abcd: pkg=%s user=UserHandle{0} id=1 tag=null importance=3 key=%s: "
             "Notification(channel=c shortcut=null contentView=null vibrate=null sound=null defaults=0x0 "
             "flags=0x10 color=0x00000000 vis=PRIVATE))" % (pkg, key),
             "      uid=10123 userId=0", "      opPkg=" + pkg, "      icon=Icon(typ=RESOURCE pkg=%s)" % pkg,
             "      flags=" + flags, "      originalFlags=" + flags, "      pri=0", "      key=" + key,
             "      seen=false", "      groupKey=0|%s|g:x" % pkg]

    def notification(words, when):
        out = [" " * 12 + "contentIntent=PendingIntent{1: PendingIntentRecord{2 %s startActivity}}" % pkg,
               " " * 12 + "number=0"]
        if when_line:
            out.append(" " * 12 + "when=%s" % when)
        out += [" " * 12 + "tickerText=null", " " * 12 + "vis=0", " " * 12 + "extras={"]
        out += [pad + "%s=%s" % kv for kv in words]
        return out + [" " * 12 + "}"]
    if marks:
        lines.append("      notification=")
    lines += notification(words, when)
    if marks:
        lines.append("      publicNotification=")
        lines += notification(public, when) if public else [" " * 12 + "None"]
    return lines + ["      stats=SingleNotificationStats{posttimeElapsedMs=1}", "      mContactAffinity=0.0"]


NOTIFICATION_KEYS = ["0|com.facebook.aura|1|null|10301", "0|com.google.android.googlequicksearchbox|1|g|10120",
                     "0|com.google.android.googlequicksearchbox|2|wx|10120", "0|com.wispr.flowapp|7|null|10288",
                     "0|com.whatsapp|3|null|10250", "0|com.example.bank|4|null|10260"]
NOTIFICATION_RAW = "\n".join(
    ["Current Notification Manager state:", "  Housekeeping: ok", "  Notification List:"]
    + _record(NOTIFICATION_KEYS[0], "AUTO_CANCEL", "1791343200000/1791343200000",
              [("android.title", "String (Muse)"), ("android.text", "String (Ten o'clock, Zach (time to wind down))")])
    # a group's summary, words and all: its notifications are listed
    + _record(NOTIFICATION_KEYS[1], "GROUP_SUMMARY", "1791343213898/1791343213898",
              [("android.title", "String (Google)"), ("android.text", "String (2 new)")])
    + _record(NOTIFICATION_KEYS[2], "", "1791343300000/1791343300000",
              [("android.title", "SpannableString (Cooling over next 2 days)"),
               ("android.text", "SpannableString (See full forecast for Woodbury)")])
    + _record(NOTIFICATION_KEYS[3], "ONGOING_EVENT|NO_CLEAR|FOREGROUND_SERVICE", "1791340000000/1791340000000",
              [("android.title", "String (Wispr Flow)"), ("android.text", "null"),
               ("android.bigText", "String (Dictation ready)")])
    # words over several lines: the next lines start anywhere (review of Oct 7)
    + _record(NOTIFICATION_KEYS[4], "AUTO_CANCEL", "0/1791343500000",
              [("android.title", "String (Mom)"),
               ("android.text", "String (Call me when you land\nLove you\n  P.S. snacks\n\n)"),
               ("android.bigText", "String (Call me\nwhen you land)")])
    # the lock screen version's words never stand in for the notification's own
    + _record(NOTIFICATION_KEYS[5], "AUTO_CANCEL", "1791343100000/1791343100000",
              [("android.title", "String (Bank)")],
              public=[("android.title", "String (Bank)"), ("android.text", "String (Contents hidden)")])
    + ["  ", "  mArchive=Archive (5 notifications)", "  Snoozed notifications:"]
    + _record("0|com.snoozed.app|9|null|10999", "AUTO_CANCEL", "1791343400000/1791343400000",
              [("android.title", "String (Snoozed)"), ("android.text", "String (Not shown)")])) + "\n"


def _pb_varint(n):
    out = b""
    while True:
        low, n = n & 0x7F, n >> 7
        if not n:
            return out + bytes([low])
        out += bytes([low | 0x80])


def _pb_field(number, value):
    if isinstance(value, int):
        return _pb_varint(number << 3) + _pb_varint(value)
    value = value.encode() if isinstance(value, str) else value
    return _pb_varint((number << 3) | 2) + _pb_varint(len(value)) + value


def _notification_proto(keys, flags=None, state=1):
    """`dumpsys notification --proto` for these keys, base64: each record's
    key = 1, state = 2 (POSTED = 1), flags = 3, package = 11 (the key's),
    then a field the reader skips (listener_hints = 4)."""
    import base64
    flags = flags or {}
    body = b"".join(_pb_field(1, _pb_field(1, k) + (_pb_field(2, state) if state else b"")
                              + _pb_field(3, flags.get(k, 0)) + _pb_field(11, k.split("|")[1]))
                    for k in keys)
    return base64.b64encode(body + _pb_field(4, 0)).decode()


def _on_the_phone(raw, keys=(), flags=None, proto=None):
    """What NOTIF_DUMP prints on a phone whose dump is `raw`: the proto
    dump of the notifications showing (base64, see _notification_proto;
    `proto` stands in for it), the mark, and the dump through its grep
    (the same extended regular expression, run here, nothing added)."""
    pattern = re.search(r"grep -E '(.*)'$", pc.NOTIF_DUMP).group(1)
    kept = [line for line in raw.split("\n") if line and re.search(pattern, line)]
    head = _notification_proto(keys, flags) if proto is None else proto
    return head + "\n<<dump>>\n" + "\n".join(kept) + "\n"


NOTIFICATION_FLAGS = {NOTIFICATION_KEYS[0]: 0x10, NOTIFICATION_KEYS[1]: 0x200, NOTIFICATION_KEYS[3]: 0x62,
                      NOTIFICATION_KEYS[4]: 0x10, NOTIFICATION_KEYS[5]: 0x10}
NOTIFICATION_DUMP = _on_the_phone(NOTIFICATION_RAW, NOTIFICATION_KEYS, NOTIFICATION_FLAGS)


class NotificationsTests(OfflineTestCase):
    def test_notifications_come_from_the_notification_manager(self):
        # Oct 7: the shade's read was slow and, with SystemUI holding a stale
        # open shade, said "1 notification" where the phone held eighteen
        found, left_out = pc.parse_notification_dump(NOTIFICATION_DUMP)
        self.assertEqual(left_out, 0)
        self.assertEqual([(p, t, x, o) for _w, p, t, x, o in found], [
            # a text over several lines: its first line, and nothing after it lost
            ("com.whatsapp", "Mom", "Call me when you land\u2026", False),
            ("com.google.android.googlequicksearchbox", "Cooling over next 2 days",
             "See full forecast for Woodbury", False),
            ("com.facebook.aura", "Muse", "Ten o'clock, Zach (time to wind down)", False),
            ("com.example.bank", "Bank", None, False),  # not its lock screen "Contents hidden"
            ("com.wispr.flowapp", "Wispr Flow", "Dictation ready", True)])  # newest first; no summary, no snoozed
        self.assertEqual(found[0][0], 1791343500000)  # getWhen(): an app's when of 0 isn't the oldest
        adb = self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=0, stdout=NOTIFICATION_DUMP, stderr=""))
        sc = self.allow("scrcpy_send")
        with self.cap() as (out, err):
            rc = pc.cmd_notifications(self.parse(["notifications"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("5 notifications (newest first)", out.getvalue())
        self.assertIn("- Muse: Ten o'clock, Zach (time to wind down) (com.facebook.aura)", out.getvalue())
        self.assertIn("- Wispr Flow: Dictation ready (com.wispr.flowapp, ongoing)", out.getvalue())
        self.assertNotIn("left out", out.getvalue())
        self.assertEqual(adb.call_count, 1)
        self.assertIn("dumpsys notification --noredact", adb.call_args[0][1])
        sc.assert_not_called()  # the shade is never touched
        self.assertEqual(pc._extra_value("null"), None)
        self.assertEqual(pc._extra_value("String (a (b) c)"), "a (b) c")
        self.assertEqual(pc._extra_value("String (cut here"), "cut here\u2026")
        self.assertEqual(pc._flag_bits("0x62"), 0x62)
        self.assertEqual(pc._flag_bits("AUTO_CANCEL|GROUP_SUMMARY"), 0x200)
        # the list's end comes from the phone's grep itself: the snoozed
        # notification after it stays out
        self.assertIn("\n  \n", NOTIFICATION_DUMP)
        self.assertNotIn("Snoozed", "".join(t or "" for _w, _p, t, _x, _o in found))
        # a proto that can't be read vouches for nothing
        self.assertIsNone(pc.posted_notifications("not base64!"))
        self.assertIsNone(pc.posted_notifications(""))
        self.assertEqual(pc.posted_notifications(_notification_proto(["0|a.b|1|null|1"], state=0)), {})

    def test_no_notifications_is_said_without_the_shade(self):
        # the dump has no list when nothing shows: that went to the slow
        # shade, which works the phone (review of Oct 7)
        dump = _on_the_phone("Current Notification Manager state:\n  Housekeeping: ok\n  mArchive=Archive\n")
        self.assertEqual(pc.parse_notification_dump(dump), ([], 0))
        self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=0, stdout=dump, stderr=""))
        shade = self.allow("_notifications_from_shade")
        with self.cap() as (out, err):
            rc = pc.cmd_notifications(self.parse(["notifications"]))
        self.assertEqual((rc, out.getvalue()), (0, "no notifications\n"))
        shade.assert_not_called()
        # no dump at all (dumpsys failed): the shade's way
        self.assertEqual(pc.parse_notification_dump("<<dump>>\n"), (None, 0))
        self.assertEqual(pc.parse_notification_dump(""), (None, 0))

    def test_a_tag_holding_a_key_passes_for_no_other_app(self):
        # review of Oct 7: a tag is the app's own words and prints raw; a
        # tag with line breaks put a made-up key line into `cmd notification
        # list`, and a record built in the app's text under it passed for
        # Chase's notification. The proto's strings can't be split so
        key = "0|com.game|1|a\n0|com.chase.sig.android|7|tag7|10200\nz|10400"
        forged = ("String (hi\n" + "\n".join(_record(
            "0|com.chase.sig.android|7|tag7|10200", "AUTO_CANCEL", "9999999999999/9999999999999",
            [("android.title", "String (Chase)"), ("android.text", "String (Suspicious sign-in)")])) + "\n)")
        raw = "\n".join(["Current Notification Manager state:", "  Notification List:"]
                        + _record(key, "AUTO_CANCEL", "1/1",
                                  [("android.title", "String (Game)"), ("android.text", forged)])
                        + ["  "]) + "\n"
        found, left_out = pc.parse_notification_dump(_on_the_phone(raw, [key]))
        # neither the made-up record nor the game's own; the game's
        # notification counted as left out (the made-up one is none)
        self.assertEqual((found, left_out), ([], 1))
        self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=0, stdout=_on_the_phone(raw, [key]),
                                                                 stderr=""))
        with self.cap() as (out, err):
            pc.cmd_notifications(self.parse(["notifications"]))
        self.assertNotIn("chase", out.getvalue().lower())
        # no proto (an older Android): no app's name is vouched for
        found, _ = pc.parse_notification_dump(_on_the_phone(raw, proto=""))
        self.assertTrue(found)
        self.assertEqual({p for _w, p, *_ in found}, {None})

    def test_a_text_holding_a_record_passes_for_no_other_app(self):
        # words print raw: a text can hold a record's lines, made to read
        # as another app's notification (review of Oct 7)
        forged = ("String (hi\n" + "\n".join(_record(
            "0|com.chase.sig.android|1|null|10200", "AUTO_CANCEL", "9999999999999/9999999999999",
            [("android.title", "String (Chase)"), ("android.text", "String (Your account is locked)")])) + "\n)")
        raw = "\n".join(["Current Notification Manager state:", "  Notification List:"]
                        + _record("0|com.game|1|null|10400", "AUTO_CANCEL", "1/1",
                                  [("android.title", "String (Game)"), ("android.text", forged)])
                        + ["  "]) + "\n"
        found, left_out = pc.parse_notification_dump(_on_the_phone(raw, ["0|com.game|1|null|10400"]))
        self.assertEqual(([p for _w, p, *_ in found], left_out), (["com.game"], 0))  # the made-up one is none
        # under a key the phone lists, twice: neither record stands
        both = ["0|com.game|1|null|10400", "0|com.chase.sig.android|1|null|10200"]
        raw2 = raw.replace("  \n", "\n".join(_record(both[1], "AUTO_CANCEL", "5/5", [
            ("android.title", "String (Chase)"), ("android.text", "String (Statement ready)")])) + "\n  \n")
        found, left_out = pc.parse_notification_dump(_on_the_phone(raw2, both))
        self.assertEqual(([p for _w, p, *_ in found], left_out), (["com.game"], 1))  # Chase's, not for sure
        self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=0, stdout=_on_the_phone(raw2, both),
                                                                 stderr=""))
        with self.cap() as (out, err):
            pc.cmd_notifications(self.parse(["notifications"]))
        self.assertIn("(1 more left out: the phone shows it but its words couldn't be told for sure)",
                      out.getvalue())
        # no proto (an older Android): every record read, none vouched for
        found, left_out = pc.parse_notification_dump(_on_the_phone(raw2, proto=""))
        self.assertEqual((left_out, {p for _w, p, *_ in found}), (0, {None}))
        self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=0, stdout=_on_the_phone(raw2, proto=""),
                                                                 stderr=""))
        with self.cap() as (out, err):
            pc.cmd_notifications(self.parse(["notifications"]))
        self.assertIn("- Chase: Statement ready\n", out.getvalue())  # no "(com.chase...)" on its word
        self.assertIn("(no app names: this phone doesn't say for sure which app sent each)", out.getvalue())

    def test_the_notifications_showing_are_all_accounted_for(self):
        # review, Oct 7: a notification whose record didn't come was left
        # out without a word; an app's words can hold line separators
        title = "Hello" + chr(0x2028) + "World"
        keys = ["0|com.a|1|null|1", "0|com.b|2|null|2", "0|com.c|3|null|3"]
        raw = "\n".join(["Current Notification Manager state:", "  Notification List:"]
                        + _record(keys[0], "AUTO_CANCEL", "2/2", [("android.title", "String (%s)" % title)])
                        + ["  "]) + "\n"
        found, left_out = pc.parse_notification_dump(
            _on_the_phone(raw, keys, {keys[2]: pc._FLAG_BITS["GROUP_SUMMARY"]}))
        self.assertEqual([(p, t) for _w, p, t, *_ in found], [("com.a", title)])  # its whole title
        self.assertEqual(left_out, 1)  # com.b's; com.c's is a group's summary
        # the key where AOSP prints it, not in the tag before it
        tagged = raw.replace("tag=null", "tag=x key=%s: y" % keys[1])
        found, left_out = pc.parse_notification_dump(_on_the_phone(tagged, [keys[1], keys[0]]))
        self.assertEqual(([p for _w, p, *_ in found], left_out), (["com.a"], 1))
        # a varint past ten bytes, a fixed field past the end: no proto
        self.assertRaises(ValueError, pc._proto_fields, bytes([8] + [255] * 11 + [1]))
        self.assertRaises(ValueError, pc._proto_fields, bytes([9, 1, 2]))
        self.assertEqual(pc._proto_fields(bytes([8, 150, 1])), [(1, 0, 150)])

    def test_an_older_androids_dump_reads_too(self):
        # Android 9 and 10: no notification marks, no time, the words at 10
        # spaces, flags in hex (0x62: ongoing, a foreground service)
        raw = "\n".join(["Current Notification Manager state:", "  Notification List:"]
                        + _record("0|com.spotify|1|null|10500", "0x62", "0",
                                  [("android.title", "String (Song)"), ("android.text", "String (Artist)")],
                                  extras_indent=10, when_line=False, marks=False)
                        + ["  "]) + "\n"
        spotify = ["0|com.spotify|1|null|10500"]
        found, _ = pc.parse_notification_dump(_on_the_phone(raw, spotify, {spotify[0]: 0x62}))
        self.assertEqual(found, [(0, "com.spotify", "Song", "Artist", True)])
        # without the proto, the hex flags of the text say it
        found, _ = pc.parse_notification_dump(_on_the_phone(raw, proto=""))
        self.assertEqual(found, [(0, None, "Song", "Artist", True)])
        self.allow("adb_or_ensure", return_value=SimpleNamespace(
            returncode=0, stdout=_on_the_phone(raw, spotify, {spotify[0]: 0x62}), stderr=""))
        with self.cap() as (out, err):
            pc.cmd_notifications(self.parse(["notifications"]))
        self.assertIn("1 notification (in the phone's order)", out.getvalue())

    def test_a_shade_that_draws_nothing_is_reset_once(self):
        # Oct 7: the shade window held the focus and drew nothing; neither
        # the command nor a finger opened it until quick settings opened
        # and closed
        self.allow("_notifications_from_dump", return_value=(None, 0))
        self.allow("scrcpy_send", return_value=True)
        self.allow("u2_invalidate")
        self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        self.allow("screen_dims", return_value=(1080, 2400))
        self.allow("settle_only")
        adb = self.allow("adb_or_ensure")
        pages = [[], [], [("Muse: done", 400)]]
        self.allow("notification_rows", side_effect=lambda root: pages.pop(0))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_notifications(self.parse(["notifications"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("1 notification", out.getvalue())
        self.assertEqual([c[0][1] for c in adb.call_args_list],
                         ["cmd statusbar collapse; sleep 0.4; cmd statusbar expand-notifications"])
        # still nothing after the reset: "no notifications", one reset only
        adb.reset_mock()
        pages[:] = [[], [], [], []]
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_notifications(self.parse(["notifications"]))
        self.assertIn("no notifications", out.getvalue())
        self.assertEqual(adb.call_count, 1)

    def test_the_shade_pages_over_the_scrcpy_helper(self):
        # Oct 7: twelve notifications took 5.5s, two adb swipes and pauses
        self.allow("_notifications_from_dump", return_value=(None, 0))
        sc = self.allow("scrcpy_send", return_value=True)
        self.allow("wake")
        self.allow("u2_invalidate")
        self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        self.allow("screen_dims", return_value=(1080, 2400))
        adb_swipe = self.allow("_scroll_swipe")
        settle = self.allow("settle_only")
        pages = [[("Muse: done", 400), ("Amazon: shipped", 2300)],  # reaches the bottom
                 [("Amazon: shipped", 600), ("Weawow: 71\u00b0", 900)]]
        self.allow("notification_rows", side_effect=lambda root: pages.pop(0))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_notifications(self.parse(["notifications"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("3 notifications", out.getvalue())
        self.assertEqual(sum(1 for c in sc.call_args_list if c[0][0].startswith("swipe")), 1)
        adb_swipe.assert_not_called()
        # the shade drawn first, then the page after the swipe
        self.assertEqual([c.kwargs for c in settle.call_args_list],
                         [{"idle_ms": pc.IDLE_ACT_MS, "delay": 0.2}, {"idle_ms": pc.IDLE_SCROLL_MS, "delay": 0.15}])
        # a scrcpy swipe that brought nothing new: once more over adb
        sc.reset_mock()
        pages[:] = [[("Muse: done", 400), ("Amazon: shipped", 2300)],
                    [("Muse: done", 400), ("Amazon: shipped", 2300)],  # it didn't move
                    [("Weawow: 71\u00b0", 900)]]
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_notifications(self.parse(["notifications"]))
        self.assertIn("3 notifications", out.getvalue())
        adb_swipe.assert_called_once_with("down")

    def test_the_shade_opens_and_closes_through_the_scrcpy_helper(self):
        self.allow("_notifications_from_dump", return_value=(None, 0))  # an older Android: the shade's way
        sc = self.allow("scrcpy_send", return_value=True)
        settle = self.allow("settle_only")  # the shade drawn to its end first (Oct 7)
        adb = self.allow("adb_or_ensure")
        self.allow("u2_invalidate")
        self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        self.allow("notification_rows", return_value=[("Wispr Flow: dictation ready", 500)])
        self.allow("screen_dims", return_value=(1080, 2400))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_notifications(self.parse(["notifications"]))
        self.assertEqual(rc, 0, err.getvalue())
        # closed first (a stale "open" shade ignores an expand, Oct 7), then opened
        self.assertEqual([c[0][0] for c in sc.call_args_list], ["collapse", "wake", "shade", "collapse"])
        adb.assert_not_called()
        self.assertIn("Wispr Flow: dictation ready", out.getvalue())
        self.assertTrue(all(c.kwargs.get("fresh") for c in pc.ui_dump.call_args_list))  # never the cache
        settle.assert_called_once()  # before the first read: a read 0.6s in caught the shade opening
        # the helper is away: adb opens and closes the shade, as before
        sc.side_effect = RuntimeError("no scrcpy")
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_notifications(self.parse(["notifications"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual([c[0] for c in adb.call_args_list],
                         [("shell", "input keyevent 224; cmd statusbar collapse; sleep 0.15; "
                                    "cmd statusbar expand-notifications"),
                          ("shell", "cmd", "statusbar", "collapse")])


class StartAndSettingsTests(OfflineTestCase):
    def test_start_goes_over_scrcpy_and_reads_once(self):
        sc = self.allow("scrcpy_send", return_value=True)
        adb = self.allow("adb_or_ensure")
        self.allow("u2_invalidate")
        self.allow("nav_record")
        calls, specs = [], []
        self.allow("u2sock", side_effect=lambda cmd, arg="", timeout=30:
                   calls.append(cmd) or specs.append(json.loads(arg)) or SAMPLE_XML)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_start(self.parse(["start", "com.example"]))
        self.assertEqual(rc, 0)
        sc.assert_called_once_with("startapp com.example")  # no "+": a force-stop would kill a download
        adb.assert_not_called()
        self.assertEqual(calls, ["act"])
        # the read wakes the screen first, in the same trip (an app started
        # with the screen off read as the bars alone, Oct 7)
        self.assertEqual(specs, [{"wake": True, "idle": pc.IDLE_LAUNCH_MS}])
        self.assertIn("launched com.example", out.getvalue())
        self.assertIn("screen: com.example", out.getvalue())

    def test_start_reports_another_app_in_front(self):
        self.allow("scrcpy_send", return_value=True)
        self.allow("u2_invalidate")
        self.allow("nav_record")
        # the verdict's one adb call: the package is installed
        self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=0, stdout="package:/data/app/x/base.apk\n", stderr=""))
        reads = self.allow("u2sock", return_value=SAMPLE_XML)
        with mock.patch.object(pc.time, "sleep"), mock.patch.object(pc, "LAUNCH_SLOW_S", 0), \
                self.cap() as (out, err):
            rc = pc.cmd_start(self.parse(["start", "com.other"]))
        self.assertEqual(rc, 1)
        self.assertIn("com.other didn't come to the front; com.example is still open",
                      err.getvalue())
        # read again while the window may still be coming, then the verdict
        self.assertEqual(reads.call_count, 1 + pc.LAUNCH_REREADS)
        # a package that isn't installed is said (the old advice, BACK and
        # another try, can't help)
        self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=1, stdout="", stderr=""))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_start(self.parse(["start", "com.espn.scores"]))
        self.assertEqual(rc, 1)
        self.assertIn("com.espn.scores isn't installed on the phone. `burner apps scores`", err.getvalue())
        self.assertNotIn("press BACK", err.getvalue())

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

    def test_a_start_whose_window_is_not_in_the_tree_yet_is_waited_for(self):
        # the Play Store, Oct 6: drawn 2.4s after a cold start, while the
        # reads held only the system UI's bars, no word, for 6-14s
        self.allow("scrcpy_send", return_value=True)
        self.allow("u2_invalidate")
        self.allow("nav_record")
        self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=0, stdout="package:/data/app/x/base.apk\n", stderr=""))
        reads = []

        def u2(cmd, arg="", timeout=30):
            reads.append(cmd)
            return BARS_XML if len(reads) <= 7 else SAMPLE_XML
        self.allow("u2sock", side_effect=u2)
        self.allow("ui_dump", side_effect=lambda **kw: ET.fromstring(BARS_XML if len(reads) <= 7 else SAMPLE_XML))
        with mock.patch.object(pc.time, "sleep"), mock.patch.object(pc, "LAUNCH_SLOW_S", 0), \
                self.cap() as (out, err):
            rc = pc.cmd_start(self.parse(["start", "com.example"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("screen: com.example", out.getvalue())  # read on past the other budget
        self.assertNotIn("no app's window", out.getvalue())
        # never there: said so, with the way to see the screen
        reads.clear()
        self.allow("u2sock", side_effect=lambda cmd, arg="", timeout=30: reads.append(cmd) or BARS_XML)
        self.allow("ui_dump", side_effect=lambda **kw: ET.fromstring(BARS_XML))
        with mock.patch.object(pc.time, "sleep"), mock.patch.object(pc, "LAUNCH_TREE_S", 0), \
                self.cap() as (out, err):
            rc = pc.cmd_start(self.parse(["start", "com.example"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("(no app's window readable yet; `burner shot` shows the screen)", out.getvalue())
        # the helper takes such a read for no app on the screen, not for a
        # server reading no words: never replaced for it, however long
        mod = _u2mux()
        self.assertTrue(mod.blank_screen(BARS_XML))
        self.assertFalse(mod.mute_read(BARS_XML))
        self.assertFalse(mod.blank_screen(BARS_XML.replace('content-desc=""', 'content-desc="Battery 80 percent"', 1)))
        self.assertFalse(mod.blank_screen(MUTE_XML))
        self.assertTrue(mod.mute_read(MUTE_XML))
        EmptyScreenTests.no_sleep(self, mod)
        dm = EmptyScreenTests._daemon(self, mod)
        restarts, woke = [], []
        dm.connect = lambda force=False: restarts.append(force)
        dm._wake = lambda: woke.append(1)
        dm._page_read = lambda **kw: None
        # the screen off (Oct 6): woken and read again, never a replacement
        dm.d = _FakeServer([BARS_XML, SAMPLE_XML], screen_on=False)
        dm._mute_since = mod._time.monotonic() - 30  # a long wordless spell already
        self.assertEqual(dm._dump(fresh=True), SAMPLE_XML)
        self.assertEqual((woke, restarts), ([1], []))
        # the screen on and the bars alone, read after read: the wordless
        # spell decides, and a server that sees no app is replaced
        dm.d = _FakeServer([BARS_XML, SAMPLE_XML], screen_on=True)
        dm._mute_since, dm._wordless_seen = mod._time.monotonic() - 30, False
        self.assertEqual(dm._dump(fresh=True), SAMPLE_XML)
        self.assertEqual(restarts, [True])
        # a moment of them: kept, as a transition
        restarts.clear()
        dm.d = _FakeServer([BARS_XML], screen_on=True)
        dm._mute_since, dm._wordless_seen = None, False
        self.assertEqual(dm._dump(fresh=True), BARS_XML)
        self.assertEqual(restarts, [])

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
        self.assertIn("-f 0x10200000", adb.call_args[0][1])  # resumed, as a tap on its icon does

    def test_settings_page_by_name(self):
        adb = self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.allow("u2_invalidate")
        self.allow("u2sock", return_value=SAMPLE_XML)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_settings(self.parse(["settings", "bluetooth"]))
        self.assertEqual(rc, 0)
        # the page asked for, not the one Settings was left on (Oct 7)
        self.assertIn("am start --activity-clear-top -a 'android.settings.BLUETOOTH_SETTINGS'", adb.call_args[0][1])
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
        # alone: the Settings app, on its main page, the page names after its
        # screen (it opened nothing and the next tap missed, Oct 7)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            self.assertEqual(pc.cmd_settings(self.parse(["settings"])), 0)
        self.assertIn("am start --activity-clear-top -a 'android.settings.SETTINGS'", adb.call_args[0][1])
        self.assertIn("opened settings: home", out.getvalue())
        self.assertIn("screen: com.example", out.getvalue())
        self.assertIn("(pages by name: `burner settings <page>`: about, accessibility", out.getvalue())
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
# the system UI's bars alone, no word: a cold start's window not in the
# screen reader's tree yet (the Play Store, Oct 6)
BARS_XML = ('<?xml version="1.0"?><hierarchy rotation="0">'
            + "".join('<node index="%d" text="" class="android.widget.FrameLayout" package="com.android.systemui"'
                      ' content-desc="" bounds="[0,%d][1080,%d]" />' % (i, i * 10, i * 10 + 8) for i in range(15))
            + '</hierarchy>')

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
        if cmd == "echo ok":
            return "ok"
        if cmd.startswith("dumpsys power"):
            return {True: "mWakefulness=Awake", False: "mWakefulness=Asleep"}.get(self.awake, "")
        if not cmd.startswith("ps"):
            return ""
        return self.listings.pop(0) if len(self.listings) > 1 else self.listings[0]

    def open_transport(self, timeout=None):
        """A stream to the phone's shell (see u2mux.bounded_shell): its
        command runs through `shell` above."""
        assert timeout, "a phone shell needs a bound on the whole of it"
        dev = self

        class Transport:
            def __init__(self):
                self.cmd = None
                self.conn = SimpleNamespace(settimeout=lambda t: None)

            def send_command(self, c):
                assert c.startswith("shell:"), c
                self.cmd = c[len("shell:"):]

            def check_okay(self):
                pass

            def read_until_close(self, encoding="utf-8"):
                return dev.shell(self.cmd, timeout=timeout)

            def close(self):
                pass
        return Transport()


NO_WINDOWS = '<hierarchy rotation="0" />'  # a look at the windows that shows nothing over the page


class _FakeServer:
    """A uiautomator2 device: deviceInfo and a queue of reads. One read
    more than queued is a failed test. A look at the windows (a shallow
    read, see u2mux._Look) is answered with `windows`, counted in
    `looks`, and spends no queued read."""
    def __init__(self, reads, screen_on=True, windows=NO_WINDOWS):
        self.reads = list(reads)
        self.info = {"screenOn": screen_on, "sdkInt": 35}
        self.calls, self.looks, self.windows = [], 0, windows
        self.jsonrpc = SimpleNamespace(wakeUp=lambda: None, getConfigurator=lambda: {},
                                       setConfigurator=lambda cfg: None)

    def jsonrpc_call(self, method, params, timeout=10):
        if method == "dumpWindowHierarchy" and params[1] < 50:
            self.looks += 1
            return self.windows
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

    def wait_loading(self, cap_s):
        return self.loading

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
        for name in ("TARGET_FILL_JS", "FIND_JS", "TARGET_JS", "FILL_JS", "FILLED_JS", "READ_JS", "PLACE_JS",
                     "SELECT_JS", "SETTLE_JS", "SCROLL_TO_JS", "SCROLL_JS", "TEXT_JS", "GUARD_JS", "VERDICT_JS"):
            if ("(" + getattr(self.cdp, name)) in expression:
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

    def call_many(self, cmds, timeout=10.0, raise_errors=True):
        names, out = [], []
        for method, params in cmds:
            if method == "Runtime.evaluate":
                name = self._name(params.get("expression", ""))
                names.append(name)
                out.append({"result": {"value": self._answer(name)}})
            else:
                names.append(method)
                out.append({})
        self.calls.append(("many", names))
        if self.answers.get("navigates") and "Input.dispatchTouchEvent" in names:
            self.loading = True  # the touch started a load: its event came back with the batch
        return out


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

    def test_websocket_first_frames_ride_with_the_handshake(self):
        import base64
        import hashlib
        cdp = _cdp()
        sent = []

        class Sock:
            def __init__(self):
                self.pending = b""

            def settimeout(self, t):
                pass

            def sendall(self, data):
                sent.append(bytes(data))
                if b"Sec-WebSocket-Key: " in data:
                    key = data.split(b"Sec-WebSocket-Key: ", 1)[1].split(b"\r\n", 1)[0].decode()
                    accept = base64.b64encode(hashlib.sha1(
                        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
                    self.pending += (b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: "
                                     + accept + b"\r\n\r\n")
                else:
                    payload = b'{"id":1,"result":{"ok":true}}'
                    self.pending += bytes([0x81, len(payload)]) + payload

            def recv(self, n):
                out, self.pending = self.pending[:n], self.pending[n:]
                return out
        ws = cdp.WebSocket(Sock(), "/devtools/page/X")
        ws.send('{"id":1,"method":"Page.enable"}')  # before any reply was read
        self.assertEqual(len(sent), 2)
        self.assertTrue(sent[0].startswith(b"GET /devtools/page/X HTTP/1.1"))
        self.assertEqual(ws.recv(), '{"id":1,"result":{"ok":true}}')
        self.assertIsNone(ws._handshake_key)

    def test_a_session_chrome_drops_at_its_handshake_is_opened_again(self):
        # Oct 7: Chrome's DevTools server takes no frame before it has
        # accepted the handshake, and the accept waits on Chrome's main
        # thread; just brought to the front, Chrome closed every session
        # whose first commands came with the handshake, for 4-11s
        import base64
        import hashlib
        cdp = _cdp()
        opened = []

        class Chrome:
            """A stream to Chrome's DevTools socket: busy, a frame before
            the handshake's answer closes it; the answer comes once the
            client waits for it."""

            def __init__(self, busy, refuse=b""):
                self.busy, self.refuse, self.out, self.key = busy, refuse, b"", None
                self.accepted = self.closed = False
                self.frames = []
                opened.append(self)

            def settimeout(self, t):
                pass

            def close(self):
                pass

            def sendall(self, data):
                data = bytes(data)
                if data.startswith(b"GET "):
                    self.key = data.split(b"Sec-WebSocket-Key: ", 1)[1].split(b"\r\n", 1)[0]
                    if not self.busy:
                        self._accept()
                    return
                if not self.accepted:
                    self.closed = True  # a frame error: the connection closed
                    return
                n = data[1] & 0x7F
                at = 2 if n < 126 else 4
                n = n if n < 126 else int.from_bytes(data[2:4], "big")
                msg = json.loads(cdp.xor_mask(data[at + 4:at + 4 + n], data[at:at + 4]).decode())
                self.frames.append(msg["method"])
                result = {"Page.getFrameTree": {"frameTree": {"frame": {"id": "F1", "url": "https://a/"}}},
                          "Runtime.evaluate": {"result": {"type": "string", "value": "visible"}}}.get(msg["method"], {})
                payload = json.dumps({"id": msg["id"], "result": result}).encode()
                self.out += bytes([0x81, 126]) + len(payload).to_bytes(2, "big") + payload

            def _accept(self):
                if self.refuse:
                    self.out += self.refuse
                    return
                self.accepted = True
                accept = base64.b64encode(hashlib.sha1(self.key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                self.out += b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"

            def recv(self, n):
                if not self.out and not self.closed and not self.accepted:
                    self._accept()  # Chrome's main thread got to it while the client waited
                got, self.out = self.out[:n], self.out[n:]
                return got  # b"" once closed: what was sent before the close is read first
        streams = []
        with mock.patch.object(cdp, "open_stream", side_effect=lambda dev, timeout=6.0: streams.pop(0)):
            # Chrome idle: one stream, the commands with the handshake
            streams[:] = [Chrome(busy=False)]
            page = cdp.Page(None, "T1")
            self.assertEqual((len(opened), page.handshake_again, page.main_frame), (1, False, "F1"))
            self.assertGreater(page.visible_at, 0)
            # Chrome busy: the first stream dropped, the second's commands
            # sent once the handshake is answered
            opened.clear()
            streams[:] = [Chrome(busy=True), Chrome(busy=True)]
            page = cdp.Page(None, "T1")
            self.assertEqual((len(opened), page.handshake_again, page.main_frame), (2, True, "F1"))
            self.assertTrue(opened[0].closed)
            self.assertEqual(opened[1].frames, ["Page.enable", "Page.getFrameTree", "Runtime.evaluate"])
            self.assertGreater(page.visible_at, 0)
            # right after a launch: the commands wait for the handshake's
            # answer from the start, one stream (Oct 7: every session after a
            # launch took the second handshake)
            opened.clear()
            streams[:] = [Chrome(busy=True)]
            page = cdp.Page(None, "T1", pipeline=False)
            self.assertEqual((len(opened), page.handshake_again, page.main_frame), (1, False, "F1"))
            # any other refusal stands: one stream, no second handshake
            opened.clear()
            streams[:] = [Chrome(busy=False, refuse=b"HTTP/1.1 500 Internal Server Error\r\n\r\n")]
            with self.assertRaises(cdp.NothingSent) as cm:
                cdp.Page(None, "T1")
            self.assertIn("500", str(cm.exception))
            self.assertEqual(len(opened), 1)

    def test_a_direct_stream_is_kept_warm_with_a_ping(self):
        mod = _u2mux()
        ka = mod.KeepAliveHTTP()
        pings = []

        class Resp:
            status, will_close = 200, False

            def read(self):
                return b"pong"

        class Conn:
            class sock:
                @staticmethod
                def settimeout(t):
                    pass

            def request(self, method, path, headers=None):
                pings.append((method, path))

            def getresponse(self):
                return Resp()

            def close(self):
                pings.append("closed")
        dev = SimpleNamespace(serial="s")
        key = (dev.serial, 9008)
        ka._target, ka._last_real, ka.direct = (dev, 9008), mod._time.monotonic(), True
        ka._conns[key] = [Conn(), mod._time.monotonic() - ka.REFRESH_S - 1]
        ka._open = mock.Mock(side_effect=AssertionError("no fresh stream: the ping keeps this one"))
        ka._warm_once()
        self.assertEqual(pings, [("GET", "/ping")])
        self.assertLess(mod._time.monotonic() - ka._conns[key][1], 1.0)  # its clock started over
        ka._warm_once()
        self.assertEqual(pings, [("GET", "/ping")])  # young again: nothing to do
        # the ping fails (the server dropped the stream): a fresh one is swapped in
        ka._conns[key][1] = mod._time.monotonic() - ka.REFRESH_S - 1
        ka._conns[key][0].getresponse = mock.Mock(side_effect=ConnectionError("gone"))
        ka._open = mock.Mock(return_value=Conn())
        ka._warm_once()
        ka._open.assert_called_once()
        self.assertIn("closed", pings)
        # over adb a fresh stream is swapped in, as before
        ka.direct = False
        ka._conns[key][1] = mod._time.monotonic() - ka.REFRESH_S - 1
        ka._open = mock.Mock(return_value=Conn())
        ka._warm_once()
        ka._open.assert_called_once()

    def test_helper_reaches_the_server_straight_over_the_tailnet(self):
        mod = _u2mux()
        ka = mod.KeepAliveHTTP()
        opened = []

        class Conn:
            def __init__(self, how):
                self.how = how

            def connect(self):
                pass
        ka._open_direct = lambda ip, port: opened.append(("direct", ip, port)) or Conn("direct")
        adb = mock.Mock(return_value=Conn("adb"))
        with mock.patch.object(mod, "phone_config", return_value={"PHONE_TAILSCALE_IP": "100.64.0.9"}), \
                mock.patch.object(mod, "open_stream", adb):
            c = ka._open(SimpleNamespace(serial="s"), 9008)
            self.assertEqual((c.how, ka.direct, opened), ("direct", True, [("direct", "100.64.0.9", 9008)]))
            adb.assert_not_called()
            # through a proxy (a hosted assistant): adb's streams, no direct try
            with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://user:pw@proxy.example:3128"}):
                self.assertIsNone(ka._direct_target(9008))
                self.assertEqual(ka._open(SimpleNamespace(serial="s"), 9008).how, "adb")
            adb.reset_mock()
            # the route fails twice in a row: adb's streams, and no new try for a while
            ka._open_direct = mock.Mock(side_effect=ConnectionRefusedError("refused"))
            with mock.patch.object(mod._time, "sleep"):
                c = ka._open(SimpleNamespace(serial="s"), 9008)
            self.assertEqual((c.how, ka.direct), ("adb", False))
            adb.assert_called_once()
            c = ka._open(SimpleNamespace(serial="s"), 9008)
            self.assertEqual(ka._open_direct.call_count, 2)  # the two tries; none since
            # one refusal, then it answers: the route stays
            ka2 = mod.KeepAliveHTTP()
            ka2._open_direct = mock.Mock(side_effect=[ConnectionRefusedError("refused"), Conn("direct")])
            with mock.patch.object(mod._time, "sleep"):
                self.assertEqual(ka2._open(SimpleNamespace(serial="s"), 9008).how, "direct")
            self.assertTrue(ka2.direct)
        # no address known: adb's streams, no attempt
        ka2 = mod.KeepAliveHTTP()
        ka2._open_direct = mock.Mock(side_effect=AssertionError("no address, no direct try"))
        with mock.patch.object(mod, "phone_config", return_value={}), \
                mock.patch.object(mod, "open_stream", adb):
            self.assertEqual(ka2._open(SimpleNamespace(serial="s"), 9008).how, "adb")
        # the proxy of a hosted assistant, as tunnel.sh reads it: the
        # CONNECT handshake tools/direct.py used, on a socket of its own
        sent = []

        class Sock:
            def __init__(self):
                self.pending = b"HTTP/1.1 200 Connection established\r\n\r\n"

            def sendall(self, data):
                sent.append(bytes(data))

            def settimeout(self, t):
                pass

            def recv(self, n):
                out, self.pending = self.pending[:n], self.pending[n:]
                return out

            def close(self):
                pass
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://user:pw@proxy.example:3128"}), \
                mock.patch.object(mod.socket, "create_connection", return_value=Sock()) as cc:
            self.assertEqual(mod.tailnet_proxy(), ("proxy.example", "user:pw"))
            conn = mod.direct_http("100.64.0.9", 9008, 5.0)
            self.assertEqual((conn.host, conn.port), ("100.64.0.9", 9008))
            conn.connect()
            self.assertEqual(cc.call_args[0][0], ("proxy.example", 3130))
            self.assertTrue(sent[0].startswith(b"CONNECT 100.64.0.9:9008 HTTP/1.1\r\nHost: 100.64.0.9:9008\r\n"))
            self.assertIn(b"Proxy-Authorization: Basic dXNlcjpwdw==\r\n", sent[0])
            # a proxy that refuses
            refused = Sock()
            refused.pending = b"HTTP/1.1 403 Forbidden\r\n\r\n"
            cc.return_value = refused
            with self.assertRaises(ConnectionRefusedError):
                mod.connect_direct("100.64.0.9", 9008, 5.0)
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": ""}), \
                mock.patch.object(mod.socket, "create_connection", return_value=Sock()) as cc:
            self.assertIsNone(mod.tailnet_proxy())
            mod.direct_http("100.64.0.9", 9008, 5.0).connect()
            self.assertEqual(cc.call_args[0][0], ("100.64.0.9", 9008))
            self.assertEqual(sent[-1:], sent[-1:])  # no handshake on the tailnet itself

    def test_an_answered_command_proves_the_page_on_screen(self):
        cdp = _cdp()
        answers = []

        class FakeWS:
            class s:
                timeouts = []

                @classmethod
                def settimeout(cls, t):
                    cls.timeouts.append(t)

            def send(self, text):
                m = json.loads(text)
                answers.insert(0, json.dumps({"id": m["id"], "result": {"value": "visible"}}))

            def recv(self):
                return answers.pop()
        page = object.__new__(cdp.Page)
        page.ws, page.n, page.visible_at = FakeWS(), 0, 0.0
        page.loading, page.main_frame, page.url = False, "F1", ""
        # nothing proved it on screen yet: the command waits STALE_CALL_S at most
        page.call("Runtime.evaluate", 10.0, expression="1")
        self.assertEqual(FakeWS.s.timeouts[-1], cdp.STALE_CALL_S)
        self.assertEqual(page.visible_at, 0.0)  # an answer alone proves nothing: a read's vis does
        page.visible_at = cdp.time.monotonic()
        self.assertTrue(cdp.visible(page))  # no question within VISIBLE_FOR_S
        page.call_many([("Runtime.evaluate", {"expression": "1"})], timeout=10.0)
        self.assertEqual(FakeWS.s.timeouts[-1], 10.0)
        self.assertEqual(page._bound(10.0), 10.0)
        page.visible_at = cdp.time.monotonic() - cdp.QUICK_AFTER_S - 1
        self.assertEqual(page._bound(10.0), cdp.STALE_CALL_S)

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

        def down(dev, current=None, **kw):
            raise TimeoutError("timed out")
        fake.front_page = down
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([SAMPLE_XML, SAMPLE_XML])
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        self.assertEqual(dm._dump(fresh=True), SAMPLE_XML)  # the screen reader instead
        self.assertEqual(calls, [])
        self.assertGreater(dm._web_retry_at, mod._time.monotonic())
        dm._last_xml = CHROME_XML
        fake.front_page = lambda dev, current=None, **kw: self.fail("asked again within the cooldown")
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

        def scroll(page, direction="down", times=1, fraction=0.6, idle_ms=500, grow_ms=0):
            calls.append(("scroll", direction, times, idle_ms))
            return {"moved": 1200, "screen": WEB_SCREEN}
        fake = types.SimpleNamespace(
            is_chrome=real.is_chrome, page_xml=real.page_xml, NotSent=real.NotSent, NotDone=real.NotDone,
            Held=real.Held, CHROME_PACKAGES=real.CHROME_PACKAGES, LOAD_PROBE_S=real.LOAD_PROBE_S,
            front_page=lambda dev, current=None, **kw: _FakePage(), visible=lambda page, timeout=1.5: True,
            QUICK_PROBE_S=0.7,
            read=lambda page, cap=160: calls.append(("read",)) or WEB_SCREEN,
            read_loaded=lambda page, screen, **kw: calls.append(("read_loaded",)) or screen,
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
        self.assertEqual(str(cm.exception), "act not sent: 2 rows on the page read 'Twice'")
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
        fake.find_read = lambda page, label, top=0, exact=False: (fake.find(page, label, top), WEB_SCREEN)
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

    def test_a_native_type_into_a_field_goes_in_one_trip_when_it_can(self):
        # YouTube, Oct 6: "Search YouTube" tapped, then typed with --field
        # "Search YouTube": the field tapped again, a pause, the typing: 4.1s;
        # Settings, Oct 6: the read after the tap came before the box had the focus
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([])
        sent, reads = [], []
        dm._act_batch = lambda spec: sent.append(spec) or SAMPLE_XML.encode()
        unfocused = SAMPLE_XML.replace('focused="true"', 'focused="false"')
        moved = unfocused.replace("[100,600][900,700]", "[100,900][900,1000]")

        def act(xml_before, xml_now=None, label="Search", age=0):
            sent.clear()
            reads.clear()
            dm._last_xml, dm._last_xml_t = xml_before, mod._time.monotonic() - age
            dm._dump = lambda fresh=False, **kw: reads.append(fresh) or xml_now
            return dm.cmd_act(json.dumps({"set_text": "pudgy", "field": label, "idle": 1200}))
        # focused on the newest read: at once, into that very field, no read
        self.assertEqual(act(SAMPLE_XML), SAMPLE_XML.encode())
        self.assertEqual((reads, [s.get("tap_first") for s in sent]), ([], [None]))
        self.assertEqual(sent[0]["field_selector"]["resourceId"], "com.example:id/q")
        self.assertNotIn("field", sent[0])
        # unfocused on it, focused on a read now: at once
        act(unfocused, SAMPLE_XML)
        self.assertEqual((reads, sent[0].get("tap_first"), "field_selector" in sent[0]), ([True], None, True))
        # unfocused on both, in the same place: tapped and typed in one
        # batch, the typing pinned to that field
        act(unfocused, unfocused)
        self.assertEqual((reads, sent[0]["tap_first"]), ([True], [500, 650]))
        # moved since the assistant's read, and in its new place on a look
        # 0.4s later: tapped there, and typed, in one batch (Oct 7); the
        # text into the field with the focus after the tap, which may be
        # another box (Maps' bar opens its search screen, Oct 7)
        act(unfocused, moved)
        self.assertEqual((reads, sent[0]["tap_first"]), ([True, True], [500, 950]))
        self.assertEqual(sent[0]["field_selector"], mod.focused_field_selector())
        # a password field: pinned to its id whatever took the focus (review, Oct 7)
        pw_before = unfocused.replace('password="false" selected="false" bounds="[100,600]',
                                      'password="true" selected="false" bounds="[100,600]')
        pw_now = moved.replace('password="false" selected="false" bounds="[100,900]',
                               'password="true" selected="false" bounds="[100,900]')
        self.assertTrue(pw_before != unfocused and pw_now != moved)
        act(pw_before, pw_now)
        self.assertEqual((sent[0]["tap_first"], sent[0]["field_selector"].get("resourceId")),
                         ([500, 950], "com.example:id/q"))
        # another field with the focus before the tap: pinned to this one
        other = moved.replace("</hierarchy>", '<node text="" resource-id="com.example:id/other" '
                              'class="android.widget.EditText" package="com.example" bounds="[100,1500][900,1600]" '
                              'enabled="true" focused="true"/></hierarchy>')
        act(other.replace("[100,900][900,1000]", "[100,600][900,700]"), other)
        self.assertEqual(sent[0]["field_selector"]["resourceId"], "com.example:id/q")
        # focused, but with no resource id to pin it: a read now first
        # (the focus may have moved since the assistant's read)
        norid = SAMPLE_XML.replace('resource-id="com.example:id/q"', 'resource-id=""')
        act(norid, norid)
        self.assertEqual((reads, sent[0]["field_selector"]), ([True], mod.focused_field_selector()))
        # the newest read shows the words on a row that isn't a field (Settings'
        # search bar, read as the screen slid, Oct 6); the read now, the box
        # with the focus: typed at once
        bar = SAMPLE_XML.replace('class="android.widget.EditText"', 'class="android.widget.TextView"')
        act(bar, SAMPLE_XML)
        self.assertEqual((reads, sent[0].get("tap_first"), "field_selector" in sent[0]), ([True], None, True))
        # gone, not a field, an old read: the CLI's way (it taps first),
        # after one more look
        for before, now, label, age in ((unfocused, TAP_XML, "Search", 0),
                                        (SAMPLE_XML, None, "OK", 0), (SAMPLE_XML, None, "Search", 60),
                                        (SAMPLE_XML, None, "Password", 0)):
            with self.assertRaises(RuntimeError) as cm:
                act(before, now, label, age)
            self.assertEqual(str(cm.exception), dm.NO_FIELD_TAP)
            self.assertEqual((sent, reads), ([], [True, True]))
        # the box a tap opened, on the second look only: typed at once (Oct
        # 7: Settings' search box came after the reads, 6.2s through the CLI)
        sent.clear()
        reads.clear()
        later = iter([TAP_XML, SAMPLE_XML])
        dm._last_xml, dm._last_xml_t = TAP_XML, mod._time.monotonic()
        dm._dump = lambda fresh=False, **kw: reads.append(fresh) or next(later)
        self.assertEqual(dm.cmd_act(json.dumps({"set_text": "battery", "field": "Search", "idle": 1200})),
                         SAMPLE_XML.encode())
        self.assertEqual((reads, sent[0].get("tap_first")), ([True, True], None))
        self.assertEqual(sent[0]["field_selector"]["resourceId"], "com.example:id/q")
        # the field lost the focus before the typing (the phone found no
        # such field): nothing typed, so "not sent", and the CLI taps it
        dm._act_batch = mock.Mock(side_effect=RuntimeError(
            "act failed after sending: {'code': -32002, 'message': 'UiObjectNotFoundException'}"))
        with self.assertRaises(RuntimeError) as cm:
            act(SAMPLE_XML)
        self.assertEqual(str(cm.exception), dm.NO_FIELD_TAP)

    def test_an_action_that_left_the_screen_as_it_was_is_read_again(self):
        # Settings, Oct 6: the search bar opens another app's window, which
        # draws nothing for most of a second; the read after the tap showed
        # the main page, and the tap printed the screen from before it
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        self.assertEqual(mod.screen_sig(SAMPLE_XML), mod.screen_sig(SAMPLE_XML))
        self.assertNotEqual(mod.screen_sig(SAMPLE_XML), mod.screen_sig(SAMPLE_XML.replace('focused="true"', 'focused="false"')))
        self.assertNotEqual(mod.screen_sig(SAMPLE_XML), mod.screen_sig(TAP_XML))
        clock = SAMPLE_XML.replace("</hierarchy>", '<node text="12:01" package="com.android.systemui" class="android.widget.TextView" bounds="[0,0][90,40]"/></hierarchy>')
        self.assertEqual(mod.screen_sig(clock), mod.screen_sig(clock.replace("12:01", "12:02")))  # the status bar's clock aside
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([])
        mod._cdp = lambda: SimpleNamespace(CHROME_PACKAGES=("com.android.chrome",))
        dm._page_read = lambda: None
        batches = []
        results = SAMPLE_XML.replace('text="Hello"', 'text="Search results"')  # the screen the tap opened

        def run(spec, after, again=results, before=SAMPLE_XML):
            batches.clear()
            dm._last_xml, dm._last_xml_t = before, mod._time.monotonic()
            reads = iter([after, again])

            def batch(calls, timeout=45.0):
                batches.append([m for m, _ in calls])
                return [True] * (len(calls) - 1) + [next(reads)]
            dm._batch = batch
            return dm._act_batch(spec).decode()
        # a tap whose read shows the screen from before: read again, the new screen kept
        self.assertEqual(run({"tap": [250, 450], "idle": 1200}, SAMPLE_XML), results)
        self.assertEqual(batches[1], ["waitForIdle", "dumpWindowHierarchy"])
        self.assertEqual(dm._last_xml, results)
        # a key too; still the same after: kept as it is
        self.assertEqual(run({"key": 4, "idle": 1200}, SAMPLE_XML, again=SAMPLE_XML), mod.marked_unchanged(SAMPLE_XML))
        self.assertEqual(len(batches), 2)
        # the same words in other places, or the focus moved: the tapped bar
        # slid before the screen it opened was drawn (Settings' search, Oct 7)
        moved = SAMPLE_XML.replace("[100,600][900,700]", "[100,640][900,740]").replace('focused="true"', 'focused="false"')
        self.assertNotEqual(mod.screen_sig(moved), mod.screen_sig(SAMPLE_XML))
        self.assertEqual(run({"tap": [250, 450], "idle": 1200}, moved), results)
        self.assertEqual(len(batches), 2)
        self.assertEqual(mod.words_changed(SAMPLE_XML, results), [("-", "Hello"), ("+", "Search results")])
        self.assertEqual(mod.words_changed(SAMPLE_XML, moved), [])
        # typed text never goes into the log with a failed act's arguments
        # (review, Oct 7)
        self.assertEqual(mod.log_arg('{"set_text": "hunter2-secret", "field": "Password"}'),
                         '{"set_text": "<14 characters>", "field": "Password"}')
        self.assertEqual(mod.log_arg("cached"), "cached")
        self.assertEqual(mod.log_arg("hunter2-raw", "set_text"), "<11 characters>")  # not JSON: still out
        # text that reads as JSON: still out (review of Oct 7: "482913" was logged)
        for raw in ("482913", "4111111111111111", '"hunter2"', "[1, 2]", "true"):
            self.assertEqual(mod.log_arg(raw, "set_text"), "<%d characters>" % len(raw))
        self.assertEqual(mod.log_arg('{"set_text": 482913, "field": "Code"}', "act"),
                         '{"set_text": "<6 characters>", "field": "Code"}')
        lines = []
        failing = mock.Mock(side_effect=RuntimeError("act failed after sending: the field was tapped"))
        with mock.patch.object(mod, "log", lambda *a: lines.append(" ".join(str(x) for x in a))),                 mock.patch.object(dm, "cmd_act", failing), self.assertRaises(RuntimeError):
            dm.handle('act {"set_text": "hunter2-secret", "field": "Password", "idle": 1200}')
        self.assertIn("<14 characters>", " ".join(lines))
        self.assertNotIn("hunter2", " ".join(lines))
        # a field's words never: a password its eye button shows (review, Oct 7)
        pw = SAMPLE_XML.replace('content-desc="Search"', 'content-desc="Password"')
        shown = pw.replace('text="" resource-id="com.example:id/q"', 'text="hunter2" resource-id="com.example:id/q"')
        self.assertIn("hunter2", shown)
        self.assertEqual(mod.words_changed(pw, shown), [])
        # a tap that put the focus in a text field: its doing, no second look
        unfocused = SAMPLE_XML.replace('focused="true"', 'focused="false"')
        self.assertEqual(mod.focused_field(SAMPLE_XML), ("com.example:id/q", (500, 650)))
        self.assertIsNone(mod.focused_field(unfocused))
        self.assertEqual(run({"tap": [500, 650], "tapped_label": "Search", "idle": 1200}, SAMPLE_XML,
                             before=unfocused), SAMPLE_XML)
        self.assertEqual(len(batches), 1)
        # the same read as before still gets it
        run({"tap": [500, 650], "idle": 1200}, SAMPLE_XML, before=SAMPLE_XML)
        self.assertEqual(len(batches), 2)
        # a tap by words, its row still there and nothing new, rows gone (the
        # page fading out for the screen it opened): a second look
        fading = SAMPLE_XML.replace('text="Hello"', 'text=""')
        self.assertTrue(mod.nothing_new(fading, SAMPLE_XML))
        self.assertFalse(mod.nothing_new(results, SAMPLE_XML))
        self.assertEqual(run({"tap": [500, 650], "tapped_label": "OK", "idle": 1200}, fading), results)
        self.assertEqual(len(batches), 2)
        # its row gone (a dialog's button that closed it): one trip
        no_ok = fading.replace('text="OK"', 'text=""')
        run({"tap": [500, 650], "tapped_label": "OK", "idle": 1200}, no_ok)
        self.assertEqual(len(batches), 1)
        # a tap by coordinates with rows gone: one trip, as before
        run({"tap": [500, 650], "idle": 1200}, fading)
        self.assertEqual(len(batches), 1)
        # a switch's state is no place: a toggle's read is kept, one trip
        toggled = SAMPLE_XML.replace("</hierarchy>", '<node text="Wi-Fi" checked="true" package="com.example" '
                                     'class="android.widget.Switch" bounds="[0,1500][1080,1600]"/></hierarchy>')
        before_toggle = toggled.replace('checked="true"', 'checked="false"')
        self.assertNotEqual(mod.screen_words(toggled), mod.screen_words(before_toggle))
        run({"tap": [540, 1550], "idle": 1200}, toggled, before=before_toggle)
        self.assertEqual(len(batches), 1)
        # the same after the second look: said with the read, not on the
        # read kept (Oct 7: an agent read the screen twice to be sure)
        out = run({"key": 4, "idle": 1200}, SAMPLE_XML, again=SAMPLE_XML)
        self.assertIn('<hierarchy unchanged="1"', out)
        self.assertNotIn("unchanged", dm._last_xml)
        out = run({"tap": [100, 250], "inert": True, "idle": 1200}, SAMPLE_XML, again=SAMPLE_XML)
        self.assertIn('<hierarchy unchanged="inert"', out)
        self.assertNotIn("unchanged", run({"tap": [250, 450], "idle": 1200}, SAMPLE_XML))  # changed
        # the volume panel, the system UI's own: a change (review, Oct 7); the
        # status bar's clock isn't
        panel = SAMPLE_XML.replace("</hierarchy>", '<node text="Media volume" class="android.widget.TextView" '
                                   'package="com.android.systemui" bounds="[900,800][1060,1400]"/></hierarchy>')
        self.assertNotIn("unchanged", run({"key": 24, "idle": 1200}, panel, again=panel))
        clock = SAMPLE_XML.replace("</hierarchy>", '<node text="12:01" class="android.widget.TextView" '
                                   'package="com.android.systemui" bounds="[0,0][120,60]"/></hierarchy>')
        self.assertEqual(mod.system_rows(clock), mod.system_rows(SAMPLE_XML))
        # the CLI says "(unchanged)" only with the read the helper marked: its
        # own re-read can find the new screen (review, Oct 7)
        self.allow("u2sock", return_value=mod.marked_unchanged(SAMPLE_XML))
        self.allow("ui_dump", return_value=ET.fromstring(results))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (o, e):
            status, root, note = pc.act_and_read({"tap": [250, 450]}, prev_root=ET.fromstring(SAMPLE_XML))
        self.assertEqual(note, "")
        self.assertIn("Search results", ET.tostring(root, encoding="unicode"))
        # a heading, and nothing around it that takes a click; a row in a
        # clickable box
        heading = ('<hierarchy rotation="0"><node text="" class="android.widget.FrameLayout" package="com.x" '
                   'bounds="[0,0][1080,2400]" clickable="false"><node text="Device details" '
                   'class="android.widget.TextView" package="com.x" bounds="[50,300][600,360]" clickable="false"/>'
                   '<node text="" class="android.widget.LinearLayout" package="com.x" bounds="[0,400][1080,560]" '
                   'clickable="true"><node text="Model" class="android.widget.TextView" package="com.x" '
                   'bounds="[50,420][600,480]" clickable="false"/></node></node></hierarchy>')
        self.assertTrue(mod.inert_row(heading, mod.label_node(heading, "Device details")[0]))
        self.assertFalse(mod.inert_row(heading, mod.label_node(heading, "Model")[0]))
        # the CLI says it
        self.allow("u2sock", return_value=mod.marked_unchanged(SAMPLE_XML, inert=True))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (o, e):
            status, root, note = pc.act_and_read({"tap_label": "Hello"})
        self.assertEqual((status, note), ("ok", "(unchanged: the row tapped is a heading or a label, "
                                                "which opens nothing)"))
        self.allow("u2sock", return_value=mod.marked_unchanged(SAMPLE_XML))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (o, e):
            self.assertEqual(pc.act_and_read({"key": 4})[2], "(unchanged)")
        # nothing readable on the read again: that read, not the one from
        # before the action (the CLI reads a thin screen again)
        self.assertEqual(run({"tap": [250, 450], "idle": 1200}, SAMPLE_XML, again=BARS_XML), BARS_XML)
        # a row or two where the read before had a dozen: taken mid-transition,
        # read again (Settings' "About phone" read as its search bar alone, Oct 6)
        dozen = SAMPLE_XML.replace("</hierarchy>", "".join(
            '<node text="Row %d" package="com.example" class="android.widget.TextView" bounds="[0,%d][1080,%d]"/>'
            % (i, 800 + i * 100, 880 + i * 100) for i in range(12)) + "</hierarchy>")
        thin = SAMPLE_XML.replace('text="Hello"', 'text=""').replace('text="OK"', 'text=""')
        self.assertTrue(mod.half_drawn_after(thin, dozen))
        self.assertFalse(mod.half_drawn_after(thin, SAMPLE_XML))  # few rows before: a small screen
        self.assertFalse(mod.half_drawn_after(dozen, dozen))
        self.assertEqual(run({"tap": [250, 450], "idle": 1200}, thin, before=dozen), results)
        self.assertEqual(len(batches), 2)
        # the screen changed: one trip; typing, a chained step, Chrome, no read before: one trip
        for spec, after, before in (({"tap": [250, 450]}, results, SAMPLE_XML),
                                    ({"set_text": "x"}, SAMPLE_XML, SAMPLE_XML),
                                    ({"tap": [250, 450], "quiet": True}, SAMPLE_XML, SAMPLE_XML),
                                    ({"tap": [250, 450]}, SAMPLE_XML.replace("com.example", "com.android.chrome"),
                                     SAMPLE_XML.replace("com.example", "com.android.chrome")),
                                    ({"tap": [250, 450]}, SAMPLE_XML, "")):
            run(spec, after, before=before)
            self.assertEqual(len(batches), 1, spec)

    def test_a_tap_by_words_takes_a_row_they_name_in_one_trip(self):
        # Spotify, Oct 7: `tap Search`, with the tab described "Search, Tab
        # 2 of 4", was "not on the last read" in the helper after a fresh
        # read; the CLI planned it on that read and tapped by its place
        mod = _u2mux()
        tabs = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" package="com.spotify.music" bounds="[0,0][1080,2400]" clickable="false" enabled="true">
    <node text="Good evening" class="android.widget.TextView" package="com.spotify.music" bounds="[40,200][700,300]" clickable="false" enabled="true"/>
    <node text="Search with your voice" class="android.widget.Button" package="com.spotify.music" bounds="[700,200][1040,300]" clickable="true" enabled="true"/>
    <node text="" content-desc="Home, Tab 1 of 4" class="android.widget.FrameLayout" package="com.spotify.music" bounds="[0,2150][270,2300]" clickable="true" enabled="true" selected="true"/>
    <node text="" content-desc="Search, Tab 2 of 4" class="android.widget.FrameLayout" package="com.spotify.music" bounds="[270,2150][540,2300]" clickable="true" enabled="true"/>
    <node text="" content-desc="Your Library, Tab 3 of 4" class="android.widget.FrameLayout" package="com.spotify.music" bounds="[540,2150][810,2300]" clickable="true" enabled="true"/>
  </node>
</hierarchy>"""
        with self.assertRaises(RuntimeError) as cm:
            mod.label_node(tabs, "Search")  # exact words only, as a field's label or a bar's
        self.assertEqual(str(cm.exception), "not on the last read")
        node, alt = mod.label_node(tabs, "Search", loose=True)
        self.assertEqual((node["desc"], alt, mod.loose_words(node, alt)),
                         ("Search, Tab 2 of 4", "Search", "Search, Tab 2 of 4"))
        self.assertEqual(mod.selector_for(node, alt)["description"], "Search, Tab 2 of 4")
        self.assertEqual(mod.label_node(tabs, "Find || search", loose=True)[1], "search")
        # the CLI's own plan names the same row
        plan = pc.plan_tap(pc.walk(ET.fromstring(tabs)), 1080, 2400, text="Search")
        self.assertEqual((plan["action"], plan["node"]["desc"]), ("tap", "Search, Tab 2 of 4"))
        # an exact row first; a row the words only name it is not
        exact = tabs.replace('text="Good evening"', 'text="Search"')
        self.assertEqual(mod.label_node(exact, "Search", loose=True)[0]["text"], "Search")
        self.assertIsNone(mod.loose_words(*mod.label_node(exact, "Search", loose=True)))
        # anything less is the CLI's to plan: two rows named, no row named
        # ("Search with your voice" is not), a field holding the words
        for screen in (tabs.replace("Your Library, Tab 3 of 4", "Search, Tab 3 of 4"),
                       tabs.replace("Search, Tab 2 of 4", "Browse, Tab 2 of 4"),
                       tabs.replace('text="" content-desc="Search, Tab 2 of 4" class="android.widget.FrameLayout"',
                                    'text="Search, Tab 2 of 4" class="android.widget.EditText"').replace(
                           "Search with your voice", "Voice")):
            with self.assertRaises(RuntimeError) as cm:
                mod.label_node(screen, "Search", loose=True)
            self.assertEqual(str(cm.exception), "not on the last read")
        # a row with its words as text: its text is the selector
        inbox = tabs.replace('text="" content-desc="Home, Tab 1 of 4"', 'text="Inbox, 3 unread"')
        node, alt = mod.label_node(inbox, "Inbox", loose=True)
        sel = mod.selector_for(node, alt)
        self.assertEqual((sel["text"], sel["mask"]), ("Inbox, 3 unread", 1))
        # in a tap: by its words at tap time, in the one trip, and the read
        # says which row it was; the CLI says it
        EmptyScreenTests.no_sleep(self, mod)
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([tabs], screen_on=True)
        sent = []
        after = tabs.replace("Good evening", "Browse all")
        dm._batch = lambda calls, timeout=45.0: sent.append(calls) or [None, 1] + [None] * (len(calls) - 3) + [after]
        dm._last_xml, dm._last_xml_t = tabs, mod._time.monotonic()
        out = dm.cmd_act(json.dumps({"tap_label": "Search"}))
        self.assertEqual([m for m, _ in sent[-1]], ["wakeUp", "count", "click", "waitForIdle", "dumpWindowHierarchy"])
        self.assertEqual(sent[-1][2][1][0]["description"], "Search, Tab 2 of 4")
        self.assertEqual(dm.d.calls, [])  # no read first
        self.assertEqual(ET.fromstring(out).get("tapped"), "Search, Tab 2 of 4")
        # a `type --field Search` next is not about a box this tab opened
        self.assertEqual(dm._last_tap[0], "Search, Tab 2 of 4")
        self.assertIsNone(ET.fromstring(dm.cmd_act(json.dumps({"tap_label": "Home, Tab 1 of 4"}))).get("tapped"))

    def test_a_tap_by_words_needs_one_row_with_those_exact_words(self):
        # the review of Oct 6: label_node picks a row by rules the phone's
        # selector can't express, and the phone clicks the first match
        mod = _u2mux()
        box = SAMPLE_XML.replace("</hierarchy>", """  <node text="lofi" resource-id="q" class="android.widget.EditText" package="com.example" bounds="[100,800][980,900]" clickable="true" enabled="true" focused="true"/>
  <node text="lofi" class="android.widget.TextView" package="com.example" bounds="[100,1000][980,1100]" clickable="true" enabled="true" focused="false"/>
</hierarchy>""")
        node, alt = mod.label_node(box, "lofi")
        self.assertEqual(node["center"], [540, 1050])  # the suggestion, not the box holding the words
        self.assertEqual(mod.selector_matches(box, mod.selector_for(node, alt)), 2)
        self.assertEqual(mod.selector_matches(SAMPLE_XML, mod.selector_for(*mod.label_node(SAMPLE_XML, "OK"))), 1)
        self.assertEqual(mod.selector_matches("", {"text": "OK"}), 0)
        # in a tap: by coordinates, on the row label_node chose
        EmptyScreenTests.no_sleep(self, mod)
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([box], screen_on=True)
        sent = []
        tapped = box.replace('text="Hello"', 'text="Hello, tapped"')
        dm._batch = lambda calls, timeout=45.0: sent.append(calls) or [None] * (len(calls) - 1) + [tapped]
        dm._last_xml, dm._last_xml_t = box, mod._time.monotonic()
        dm.cmd_act(json.dumps({"tap_label": "lofi"}))
        self.assertEqual(sent[-1][1], ("click", [540, 1050]))
        self.assertNotIn("count", [m for m, _ in sent[-1]])
        # an empty read says it can't be parsed (a RuntimeError, as its callers catch)
        with self.assertRaises(RuntimeError) as cm:
            mod.label_node("", "OK")
        self.assertIn("can't be parsed", str(cm.exception))

    def test_chrome_s_devtools_stream_opens_within_a_bound(self):
        # the review of Oct 6: the open waited adbutils' 600s on a link
        # that passes nothing (a read hung with Chrome in front)
        cdp = _cdp()
        import socket
        opened, closed = [], []

        class T:
            conn = "the socket"

            def __init__(self, answer):
                self.answer = answer

            def send_command(self, c):
                opened.append(c)

            def check_okay(self):
                if self.answer is not None:
                    raise self.answer

            def close(self):
                closed.append(1)
        dev = SimpleNamespace(open_transport=lambda timeout=None: opened.append(timeout) or T(None))
        self.assertEqual(cdp.open_stream(dev), "the socket")
        self.assertEqual(opened, [6.0, "localabstract:chrome_devtools_remote"])
        dev = SimpleNamespace(open_transport=lambda timeout=None: T(socket.timeout("timed out")))
        with self.assertRaises(ConnectionError) as cm:
            cdp.open_stream(dev)
        self.assertIn("no answer from the phone's adb in 6s", str(cm.exception))
        self.assertEqual(closed, [1])

    def test_a_field_named_by_the_button_that_opened_it_takes_the_text(self):
        # Play Store, Oct 6: "Search or ask Play" (a button) opened a box
        # reading "Search apps & games"; `type --field 'Search or ask Play'`
        # failed, and a screenshot and a second type followed
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        dm = EmptyScreenTests._daemon(self, mod)
        sent = []
        dm._act_batch = lambda spec: sent.append(spec) or SAMPLE_XML.encode()
        box = SAMPLE_XML.replace('content-desc="Search"', 'content-desc="Search apps &amp; games"')
        dm._last_xml, dm._last_xml_t = box, mod._time.monotonic()
        # never without that tap: a password named by a field that isn't
        # on the screen must not go into a search box
        dm.d = _FakeServer([box, box])
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"set_text": "duolingo", "field": "Search or ask Play", "idle": 1200}))
        self.assertEqual((str(cm.exception), sent), (dm.NO_FIELD_TAP, []))
        # the button with those words tapped a moment ago
        dm._last_tap = ("Search or ask Play", mod._time.monotonic())
        dm.d = _FakeServer([box, box])
        self.assertEqual(dm.cmd_act(json.dumps({"set_text": "duolingo", "field": "Search or ask Play", "idle": 1200})),
                         SAMPLE_XML.encode())
        self.assertEqual(sent[0]["field_selector"]["resourceId"], "com.example:id/q")
        # a tap by words is remembered once it is done
        dm2 = EmptyScreenTests._daemon(self, mod)
        dm2.d = _FakeServer([])
        dm2._last_xml, dm2._last_xml_t = SAMPLE_XML, mod._time.monotonic()
        dm2._batch = lambda calls, timeout=45.0: [None] * (len(calls) - 1) + [box]
        dm2.cmd_act(json.dumps({"tap_label": "OK"}))
        self.assertEqual(dm2._last_tap[0], "OK")
        # the words still on the screen (a button that hasn't opened a box
        # yet), two fields, or the one field without the focus, a moment
        # after the tap: the box looked for again, the words not tapped
        # again (review of Oct 7), and said so
        cases = ((box.replace('text="OK"', 'text="Search or ask Play"'), "Search or ask Play"),
                 (box.replace("</hierarchy>", '<node text="" class="android.widget.EditText" '
                              'package="com.example" bounds="[100,1600][900,1700]" focused="false"/></hierarchy>'),
                  "Search or ask Play"),
                 (box.replace('focused="true"', 'focused="false"'), "Search or ask Play"))
        for xml, label in cases:
            sent.clear()
            dm._last_tap = ("Search or ask Play", mod._time.monotonic())
            dm.d = _FakeServer([xml] * 8)
            with self.assertRaises(RuntimeError) as cm:
                dm.cmd_act(json.dumps({"set_text": "duolingo", "field": label, "idle": 1200}))
            self.assertEqual((str(cm.exception), sent), (dm.NO_BOX_YET, []))
            self.assertEqual(dm.d.calls.count("dumpWindowHierarchy"), 2 + mod.BOX_LOOKS)
        # the box comes while it is looked for: the text goes into it
        sent.clear()
        dm._last_tap = ("Search or ask Play", mod._time.monotonic())
        dm.d = _FakeServer([cases[0][0], cases[0][0], cases[0][0], box])
        self.assertEqual(dm.cmd_act(json.dumps({"set_text": "duolingo", "field": "Search or ask Play", "idle": 1200})),
                         SAMPLE_XML.encode())
        self.assertEqual(sent[0]["field_selector"]["resourceId"], "com.example:id/q")
        # long after the tap, it opened no box: the CLI's way; the button
        # still there is tapped and typed into, in one trip
        for xml, label in cases[1:]:
            sent.clear()
            dm._last_tap = ("Search or ask Play", mod._time.monotonic() - 10)
            dm.d = _FakeServer([xml, xml])
            with self.assertRaises(RuntimeError) as cm:
                dm.cmd_act(json.dumps({"set_text": "duolingo", "field": label, "idle": 1200}))
            self.assertEqual((str(cm.exception), sent), (dm.NO_FIELD_TAP, []))
        sent.clear()
        dm._last_tap = ("Search or ask Play", mod._time.monotonic() - 10)
        dm.d = _FakeServer([cases[0][0].replace('focused="true"', 'focused="false"')] * 2)
        dm.cmd_act(json.dumps({"set_text": "duolingo", "field": "Search or ask Play", "idle": 1200}))
        self.assertEqual(sent[0]["opens_box"], "Search or ask Play")

    def test_a_tap_s_box_is_forgotten_after_another_action(self):
        # the review of Oct 7: the control tapped by words was taken for
        # the opener of the one focused field for 30s, whatever came next
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        dm = EmptyScreenTests._daemon(self, mod)
        box = SAMPLE_XML.replace('content-desc="Search"', 'content-desc="Search apps &amp; games"')
        dm._last_tap = ("Search or ask Play", mod._time.monotonic())
        dm.cmd_invalidate("")  # a read's invalidate (`dump --fresh`): kept (review of Oct 7)
        self.assertEqual(dm._last_tap[0], "Search or ask Play")
        dm.cmd_invalidate("acted")  # the CLI acted its own way (scrcpy, adb)
        self.assertIsNone(dm._last_tap)
        dm._last_tap = ("Search or ask Play", mod._time.monotonic())
        dm._batch = lambda calls, timeout=45.0: [None] * (len(calls) - 1) + [box]
        dm._last_xml, dm._last_xml_t = box, mod._time.monotonic()
        dm.d = _FakeServer([box], screen_on=True)
        dm.cmd_act(json.dumps({"key": 4, "idle": 1200}))  # BACK
        self.assertIsNone(dm._last_tap)
        # a type naming that control is then the CLI's way, not the lone field
        dm.d = _FakeServer([box, box])
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"set_text": "duolingo", "field": "Search or ask Play", "idle": 1200}))
        self.assertEqual(str(cm.exception), dm.NO_FIELD_TAP)
        # a page's open or scroll ends it too
        dm._last_tap = ("Search or ask Play", mod._time.monotonic())
        dm._page = lambda **kw: None
        with self.assertRaises(RuntimeError):
            dm.cmd_act(json.dumps({"open": "https://example.com"}))
        self.assertIsNone(dm._last_tap)

    def test_a_field_named_by_its_own_hint_takes_the_text(self):
        # Play Store, Oct 6: the search box is an empty field under the
        # words "Search apps & games", a TextView of their own; a type by
        # them took a tap and two more trips (4.7s)
        mod = _u2mux()
        field = SAMPLE_XML.replace('content-desc="Search"', 'content-desc=""')
        edit = 'bounds="[100,600][900,700]"/>'

        def hint(clickable="false", bounds="[150,620][560,680]"):
            return ('<node text="Search apps &amp; games" class="android.widget.TextView" package="com.example" '
                    'bounds="%s" clickable="%s" enabled="true" focused="false"/>' % (bounds, clickable))
        box = field.replace(edit, edit + hint())
        self.assertEqual(mod.field_node(box, "Search apps & games")["rid"], "com.example:id/q")
        # the hint drawn before the field, which has words of its own: the field still
        before = SAMPLE_XML.replace("<node index=\"2\"", hint() + "<node index=\"2\"")
        self.assertEqual(mod.field_node(before, "Search apps & games")["rid"], "com.example:id/q")
        # words that can be tapped (a button inside the box), words outside
        # every field, two fields around them, a row over the field: none
        second = ('<node text="" resource-id="com.example:id/q2" class="android.widget.EditText" '
                  'package="com.example" bounds="[120,610][880,690]" clickable="true" enabled="true" focused="false"/>')
        over = ('<node text="Sale!" class="android.widget.TextView" package="com.example" '
                'bounds="[400,620][600,680]" clickable="false" enabled="true" focused="false"/>')
        for xml in (field.replace(edit, edit + hint(clickable="true")),
                    field.replace(edit, edit + hint(bounds="[150,500][560,560]")),
                    field.replace(edit, edit + second + hint()),
                    field.replace(edit, edit + hint() + over)):
            self.assertIsNone(mod.field_node(xml, "Search apps & games"))
        # in a type: at once, into that very field
        EmptyScreenTests.no_sleep(self, mod)
        dm = EmptyScreenTests._daemon(self, mod)
        sent = []
        dm._act_batch = lambda spec: sent.append(spec) or SAMPLE_XML.encode()
        dm._last_xml, dm._last_xml_t = box, mod._time.monotonic()
        dm._dump = mock.Mock(side_effect=AssertionError("no read: the field has the focus"))
        self.assertEqual(dm.cmd_act(json.dumps({"set_text": "duolingo", "field": "Search apps & games", "idle": 1200})),
                         SAMPLE_XML.encode())
        self.assertEqual(sent[0]["field_selector"]["resourceId"], "com.example:id/q")
        self.assertNotIn("tap_first", sent[0])

    def test_a_tap_then_type_batch(self):
        mod = _u2mux()
        calls = mod.act_calls({"set_text": "pudgy", "tap_first": [500, 650], "idle": 1200})
        self.assertEqual([m for m, _ in calls],
                         ["wakeUp", "click", "dumpWindowHierarchy", "waitForIdle", "setText",
                          "dumpWindowHierarchy", "waitForIdle", "dumpWindowHierarchy"])
        self.assertEqual(calls[1][1], [500, 650])
        self.assertEqual(calls[4][1][0], mod.focused_field_selector())  # whichever field took the focus
        # the selectors are uiautomator2's own shape
        self.assertEqual(mod.focused_field_selector(),
                         {"mask": 131088, "childOrSibling": [], "childOrSiblingSelector": [],
                          "focused": True, "className": "android.widget.EditText"})
        self.assertEqual(mod.focused_field_selector("a:id/b"),
                         {"mask": 2228224, "childOrSibling": [], "childOrSiblingSelector": [],
                          "focused": True, "resourceId": "a:id/b"})
        calls = mod.act_calls({"set_text": "x", "field_selector": mod.focused_field_selector("a:id/b")})
        self.assertEqual(calls[1], ("setText", [mod.focused_field_selector("a:id/b"), "x"]))
        # the typing failed after the tap: said so, never "not sent"
        EmptyScreenTests.no_sleep(self, mod)
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([])
        dm._batch = lambda calls, timeout=45.0: [None, True, SAMPLE_XML, True,
                                                 RuntimeError("UiObjectNotFoundException"), SAMPLE_XML, True, SAMPLE_XML]
        with self.assertRaises(RuntimeError) as cm:
            dm._act_batch({"set_text": "pudgy", "tap_first": [500, 650], "idle": 1200})
        # no field had the focus: said so in the words the CLI types its other ways on
        self.assertIn("act failed after sending: the field was tapped; no editable field had the focus",
                      str(cm.exception))

    def test_typographic_punctuation_is_plain_when_words_are_matched(self):
        # YouTube, Oct 6: the permission dialog's "Don\u2019t allow" (a curly
        # apostrophe); the assistant typed "Don't allow"; the tap failed twice
        self.assertEqual(pc.plain_words("Don\u2019t allow"), "Don't allow")
        self.assertEqual(pc.plain_words("Wi\u2011Fi \u2013 \u201cHome\u201d\u2026"), 'Wi-Fi - "Home"...')
        dialog = SAMPLE_XML.replace("</hierarchy>", """  <node text="Don\u2019t allow" class="android.widget.Button" package="com.google.android.permissioncontroller" bounds="[100,1800][980,1950]" clickable="true" enabled="true" focused="false"/>
  <node text="Wi\u2011Fi" class="android.widget.TextView" package="com.android.settings" bounds="[100,2000][980,2100]" clickable="true" enabled="true" focused="false"/>
</hierarchy>""")
        nodes = pc.walk(ET.fromstring(dialog))
        self.assertEqual([n["text"] for n in pc.find_nodes(nodes, "Don't allow")], ["Don\u2019t allow"])
        self.assertEqual([n["text"] for n in pc.find_nodes(nodes, "wi-fi", exact=False)], ["Wi\u2011Fi"])
        mod = _u2mux()
        node, alt = mod.label_node(dialog, "Don't allow")
        self.assertEqual((node["text"], alt), ("Don\u2019t allow", "Don't allow"))
        self.assertEqual(mod.find_node(dialog, "Wi-Fi", fuzzy=False)["text"], "Wi\u2011Fi")
        # the phone's own selector keeps the row's own words (it matches them exactly)
        sel = mod.selector_for(node, "Don't allow")
        self.assertEqual((sel.get("text"), sel["mask"]), ("Don\u2019t allow", 1))
        # the page scripts that find a control or a field fold the same way; the page's read doesn't
        cdp = _cdp()
        for name in ("FIND_JS", "TARGET_JS", "FILL_JS"):
            self.assertIn("\\u2019", getattr(cdp, name), name)
        self.assertNotIn("\\u2019", cdp.READ_JS)

    def test_a_plain_type_takes_the_one_text_field_in_one_trip(self):
        # Translate, Oct 7: nothing had the focus, the focused selector found
        # nothing, and the slow way took four more trips (5.2s)
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([])
        sent = []
        dm._act_batch = lambda spec: sent.append(spec) or SAMPLE_XML.encode()
        unfocused = SAMPLE_XML.replace('focused="true"', 'focused="false"')
        dm._last_xml, dm._last_xml_t = unfocused, mod._time.monotonic()
        dm.cmd_act(json.dumps({"set_text": "hola", "idle": 1200}))
        self.assertEqual(sent[-1]["field_selector"], mod.field_selector("com.example:id/q"))
        self.assertNotIn("focused", sent[-1]["field_selector"])
        # focused already, two fields, or an old read: the focused selector, as before
        two = unfocused.replace("</hierarchy>", '<node text="" class="android.widget.EditText" package="com.example" '
                                'bounds="[100,900][900,1000]" enabled="true" focused="false"/></hierarchy>')
        for xml, age in ((SAMPLE_XML, 0), (two, 0), (unfocused, 60)):
            sent.clear()
            dm._last_xml, dm._last_xml_t = xml, mod._time.monotonic() - age
            dm.cmd_act(json.dumps({"set_text": "hola", "idle": 1200}))
            self.assertNotIn("field_selector", sent[-1])
        self.assertEqual(mod.field_selector()["mask"], 0x10)
        # a tap naming the field by its kind: the one text field
        box = ('<hierarchy rotation="0"><node text="Enter text" class="android.widget.EditText" package="com.example" '
               'bounds="[100,600][900,700]" clickable="true" enabled="true"/><node text="Spanish" '
               'class="android.widget.Button" package="com.example" bounds="[100,200][500,300]" clickable="true" '
               'enabled="true"/></hierarchy>')
        plan = pc.plan_tap(pc.walk(ET.fromstring(box)), 1080, 2400, text="text field")
        self.assertEqual((plan["action"], plan["node"]["text"]), ("tap", "Enter text"))
        two_boxes = box.replace("</hierarchy>", '<node text="" class="android.widget.EditText" package="com.example" '
                                'bounds="[100,900][900,1000]" clickable="true" enabled="true"/></hierarchy>')
        self.assertEqual(pc.plan_tap(pc.walk(ET.fromstring(two_boxes)), 1080, 2400, text="text field")["action"],
                         "nomatch")
        self.assertEqual(pc.plan_tap(pc.walk(ET.fromstring(box)), 1080, 2400, text="Spanish")["node"]["text"],
                         "Spanish")

    def test_a_query_typed_into_a_box_is_no_label_when_rows_hold_the_words(self):
        # Maps, Oct 7: `tap Target --index 0` on the results tapped the search
        # box, which held the query "Target Woodbury MN"
        results = ('<hierarchy rotation="0"><node text="Target Woodbury MN" class="android.widget.EditText" '
                   'package="com.google.android.apps.maps" bounds="[150,170][900,270]" clickable="true" '
                   'focused="false"/><node text="Target Woodbury MN" class="android.widget.TextView" '
                   'package="com.google.android.apps.maps" bounds="[100,720][700,790]"/></hierarchy>')
        for index in (None, 0):
            plan = pc.plan_tap(pc.walk(ET.fromstring(results)), 1080, 2400, text="Target", index=index)
            self.assertEqual(plan["action"], "nomatch", index)
            self.assertIn("Target Woodbury MN", plan["near"])
        # a box alone with the words: still named by them ("Email or phone")
        email = ('<hierarchy rotation="0"><node text="Email or phone" class="android.widget.EditText" '
                 'package="com.example" bounds="[100,600][900,700]" clickable="true" focused="false"/></hierarchy>')
        plan = pc.plan_tap(pc.walk(ET.fromstring(email)), 1080, 2400, text="Email")
        self.assertEqual((plan["action"], plan["node"]["text"]), ("tap", "Email or phone"))
        # a row that only holds the words, not starting with them, leaves
        # the box named (review of Oct 7: these found nothing)
        for row, label, box_words in (("Forgot email?", "Email", "Email or phone"),
                                      ("We will never share your email address", "Email", "Email address"),
                                      ("Recent searches", "Search", "Search settings")):
            screen = ('<hierarchy rotation="0"><node text="%s" class="android.widget.EditText" package="com.example" '
                      'bounds="[100,600][900,700]" clickable="true" focused="false"/><node text="%s" '
                      'class="android.widget.TextView" package="com.example" bounds="[100,760][900,820]"/></hierarchy>'
                      % (box_words, row))
            plan = pc.plan_tap(pc.walk(ET.fromstring(screen)), 1080, 2400, text=label)
            self.assertEqual((plan["action"], plan["node"]["text"]), ("tap", box_words), row)

    def test_a_type_with_nowhere_to_go_says_so(self):
        # Play Store, Oct 7: `type` right after the Search tab (its bar takes
        # a tap first) went out as key events into nothing, exit 0
        tab = ('<hierarchy rotation="0"><node text="" class="android.widget.FrameLayout" package="com.android.vending" '
               'bounds="[0,0][1080,2400]"><node text="Search" class="android.widget.TextView" package="com.android.vending" '
               'bounds="[0,100][1080,200]"/><node text="What are you looking for?" class="android.widget.TextView" '
               'package="com.android.vending" bounds="[50,300][1030,400]" clickable="false"/>'
               '<node text="Search or ask Play" class="android.view.View" package="com.android.vending" '
               'bounds="[50,450][1030,560]" clickable="true"/></node></hierarchy>')
        self.allow("screen_dims", return_value=(1080, 2400))
        self.assertEqual(pc.nowhere_to_type(ET.fromstring(tab)),
                         'no text field has the focus and the keyboard is closed: tap the box first '
                         '(`burner tap "Search or ask Play"`), or both at once: '
                         '`burner type --field "Search or ask Play" TEXT`')
        focused = tab.replace('<node text="Search or ask Play" class="android.view.View"',
                              '<node text="" focused="true" class="android.widget.EditText"')
        self.assertEqual(pc.nowhere_to_type(ET.fromstring(focused)), "")
        # the bar's words in a box that takes the tap (the Play Store's, Oct 7)
        boxed = tab.replace('<node text="Search or ask Play" class="android.view.View" package="com.android.vending" '
                            'bounds="[50,450][1030,560]" clickable="true"/>',
                            '<node text="" class="android.view.View" package="com.android.vending" '
                            'bounds="[50,450][1030,560]" clickable="true"><node text="Search or ask Play" '
                            'class="android.widget.TextView" package="com.android.vending" '
                            'bounds="[150,470][900,540]"/></node>')
        self.assertIn('`burner tap "Search or ask Play"`', pc.nowhere_to_type(ET.fromstring(boxed)))
        # no box to name (the Calculator takes key events): key events, as
        # before (review, Oct 7)
        calc = ('<hierarchy rotation="0"><node text="" class="android.widget.FrameLayout" package="com.google.android.'
                'calculator" bounds="[0,0][1080,2400]"><node text="7" class="android.widget.Button" package="com.google.'
                'android.calculator" bounds="[0,1500][270,1700]" clickable="true"/></node></hierarchy>')
        self.assertEqual(pc.nowhere_to_type(ET.fromstring(calc)), "")
        # an empty box with no words of its own (its label a line apart):
        # named by its place (key events went into nothing, review of Oct 7)
        empty = ('<hierarchy rotation="0"><node text="" class="android.widget.FrameLayout" package="com.example" '
                 'bounds="[0,0][1080,2400]"><node text="Email" class="android.widget.TextView" package="com.example" '
                 'bounds="[100,500][400,560]"/><node text="" class="android.widget.EditText" package="com.example" '
                 'bounds="[100,580][980,700]" clickable="true" focused="false"/></node></hierarchy>')
        root = ET.fromstring(empty)
        pc._update_screen_from_dump(root)
        self.assertIn("tap the empty text box first (`burner tap --xy 0.50,0.27`)", pc.nowhere_to_type(root))
        # a focused AutoCompleteTextView is a field with the focus
        auto = focused.replace('class="android.widget.EditText"', 'class="android.widget.AutoCompleteTextView"')
        self.assertEqual(pc.nowhere_to_type(ET.fromstring(auto)), "")
        keyboard = tab.replace("</node></hierarchy>", '</node><node text="q" class="android.widget.Button" '
                               'package="com.google.android.inputmethod.latin" bounds="[0,1800][100,1900]"/></hierarchy>')
        self.assertEqual(pc.nowhere_to_type(ET.fromstring(keyboard)), "")
        # the command: nothing typed, exit 1
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append(cmd)
            if cmd == "dump":
                return tab
            pc._u2_status = "err no editable field has the focus" if cmd == "set_text" else "err act not sent: x"
            return None
        self.allow("u2sock", side_effect=u2)
        keys = self.allow("type_keys")
        with self.cap() as (out, err):
            rc = pc.cmd_type(self.parse(["type", "lofi"]))
        self.assertEqual(rc, 1)
        self.assertIn('burner: nothing was typed: no text field has the focus', err.getvalue())
        keys.assert_not_called()
        self.assertNotIn("typed", out.getvalue())

    def test_type_field_on_a_bar_taps_it_and_types_in_one_trip(self):
        # Play Store, Oct 7: `type --field 'Search or ask Play'` took 5.9s,
        # the CLI's tap, read and typing
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([])
        bar = ('<hierarchy rotation="0"><node text="" class="android.widget.FrameLayout" package="com.android.vending" '
               'bounds="[0,0][1080,2400]"><node text="" class="android.view.View" package="com.android.vending" '
               'bounds="[50,450][1030,560]" clickable="true"><node text="Search or ask Play" '
               'class="android.widget.TextView" package="com.android.vending" bounds="[150,470][900,540]"/></node>'
               '<node text="Sign in" class="android.widget.Button" package="com.android.vending" '
               'bounds="[50,700][500,800]" clickable="true"/></node></hierarchy>')
        sent = []
        dm._act_batch = lambda spec: sent.append(spec) or bar.encode()
        dm._last_xml, dm._last_xml_t = bar, mod._time.monotonic()
        dm._dump = lambda fresh=False, **kw: bar
        reads = []
        dm._dump = lambda fresh=False, **kw: reads.append(fresh) or bar
        dm.cmd_act(json.dumps({"set_text": "espn", "field": "Search or ask Play", "idle": 1200}))
        self.assertEqual((sent[0]["tap_first"], sent[0]["field_selector"], sent[0]["opens_box"]),
                         ([525, 505], mod.focused_field_selector(), "Search or ask Play"))
        self.assertEqual(reads, [True])  # in its place on both reads: no second look
        # moved since the assistant's read: the second look first
        sent.clear()
        reads.clear()
        dm._last_xml, dm._last_xml_t = bar.replace("[150,470][900,540]", "[150,670][900,740]"), mod._time.monotonic()
        dm.cmd_act(json.dumps({"set_text": "espn", "field": "Search or ask Play", "idle": 1200}))
        self.assertEqual((reads, sent[0]["tap_first"]), ([True, True], [525, 505]))
        # review, Oct 7: still moving between the two reads just taken: not tapped
        sent.clear()
        moving = iter([bar.replace("[150,470][900,540]", "[150,270][900,340]"), bar])
        dm._dump = lambda fresh=False, **kw: next(moving)
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"set_text": "espn", "field": "Search or ask Play", "idle": 1200}))
        self.assertEqual((str(cm.exception), sent), (dm.NO_FIELD_TAP, []))
        # the agent just tapped it (its box still coming): not tapped again
        dm._dump = lambda fresh=False, **kw: bar
        dm._last_xml, dm._last_xml_t = bar, mod._time.monotonic()
        dm._last_tap = ("Search or ask Play", mod._time.monotonic())
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"set_text": "espn", "field": "Search or ask Play", "idle": 1200}))
        self.assertEqual((str(cm.exception), sent), (dm.NO_BOX_YET, []))
        dm._last_tap = None
        # a SearchView's AutoCompleteTextView is a field, not a bar
        auto = bar.replace('<node text="Search or ask Play" class="android.widget.TextView"',
                           '<node text="Search or ask Play" class="android.widget.AutoCompleteTextView"')
        self.assertIsNone(dm._bar_named(auto, "Search or ask Play"))
        # a secret's label, a row that takes no tap, a field with the focus: the CLI's way
        self.assertIsNone(dm._bar_named(bar.replace("Search or ask Play", "Password"), "Password"))
        self.assertIsNone(dm._bar_named(bar.replace('clickable="true"><node', 'clickable="false"><node'),
                                        "Search or ask Play"))
        self.assertIsNone(dm._bar_named(bar.replace("</node></hierarchy>", '<node text="" class="android.widget.'
                                                    'EditText" package="com.android.vending" bounds="[50,900][900,980]" '
                                                    'focused="true"/></node></hierarchy>'), "Search or ask Play"))
        # no box after the tap: said in words that don't send the CLI to tap again
        dm2 = EmptyScreenTests._daemon(self, mod)
        dm2.d = _FakeServer([])
        dm2._last_xml, dm2._last_xml_t = bar, mod._time.monotonic()
        dm2._batch = lambda calls, timeout=45.0: [
            RuntimeError("UiObjectNotFoundException") if m == "setText" else bar if m == "dumpWindowHierarchy" else True
            for m, _p in calls]
        with self.assertRaises(RuntimeError) as cm:
            dm2._act_batch({"set_text": "espn", "tap_first": [525, 505], "field_selector": mod.focused_field_selector(),
                            "opens_box": "Search or ask Play", "idle": 1200})
        self.assertIn("'Search or ask Play' was tapped, and no text box had the focus after it", str(cm.exception))
        self.assertNotIn("no editable field", str(cm.exception))

    def test_a_field_is_named_by_its_resource_id_last(self):
        # Google Maps, Oct 7: the search box read the query before, so
        # `type --field Search` found no field and took the slow way (9.2s)
        mod = _u2mux()
        maps = ('<hierarchy rotation="0"><node text="Tulsa, OK" resource-id="com.google.android.apps.maps:id/'
                'search_omnibox_edit_text" class="android.widget.EditText" package="com.google.android.apps.maps" '
                'bounds="[150,170][900,270]" clickable="true" enabled="true" focused="false"/>'
                '<node text="Search" class="android.widget.Button" package="com.google.android.apps.maps" '
                'bounds="[100,2200][400,2300]" clickable="true" enabled="true"/></hierarchy>')
        node = mod.field_node(maps, "Search")
        self.assertEqual(node["rid"], "com.google.android.apps.maps:id/search_omnibox_edit_text")
        self.assertEqual(mod.field_node(maps, "search box")["text"], "Tulsa, OK")
        self.assertIsNone(mod.field_node(maps, "box"))  # a field's kind alone names none
        self.assertIsNone(mod.field_node(maps, "Directions"))
        two = maps.replace("</hierarchy>", '<node text="" resource-id="com.example:id/searchField" '
                           'class="android.widget.EditText" package="com.google.android.apps.maps" '
                           'bounds="[150,400][900,500]" clickable="true" enabled="true"/></hierarchy>')
        self.assertIsNone(mod.id_field(two, "Search"))  # two: neither
        self.assertEqual(mod.id_field(two.replace("search_omnibox", "query_omnibox"), "Search")["rid"],
                         "com.example:id/searchField")  # camelCase parts
        pw = maps.replace("search_omnibox_edit_text", "password_edit")
        self.assertIsNone(mod.id_field(pw, "Password"))  # a secret's label: password fields only
        self.assertIsNotNone(mod.id_field(pw.replace('focused="false"', 'focused="false" password="true"'),
                                          "Password"))

    def test_a_native_type_with_a_field_label_is_not_sent_by_the_helper(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        dm = EmptyScreenTests._daemon(self, mod)
        # the read now and a look again: no "Password" field on them either
        dm.d = _FakeServer([SAMPLE_XML, SAMPLE_XML])
        dm._batch = mock.Mock(side_effect=AssertionError("setText must not go to the focused field"))
        dm._last_xml, dm._last_xml_t = SAMPLE_XML, mod._time.monotonic()  # com.example, not Chrome
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"set_text": "hunter2", "field": "Password", "idle": 1200}))
        self.assertEqual(str(cm.exception),
                         "act not sent: --field on a native screen needs a tap on the field first")

    def test_type_with_a_field_label_taps_the_field_first_on_a_native_screen(self):
        self.allow("u2_invalidate")
        self.allow("nav_record")
        calls = []

        def u2(cmd, arg="", timeout=30):
            spec = json.loads(arg) if cmd == "act" else {}
            calls.append((cmd, spec))
            if cmd == "act" and spec.get("field"):
                pc._u2_status = "err act not sent: --field on a native screen needs a tap on the field first"
                return None
            return SAMPLE_XML
        self.allow("u2sock", side_effect=u2)
        with mock.patch.object(pc.time, "sleep") as slept, self.cap() as (out, err):
            rc = pc.cmd_type(self.parse(["type", "--field", "Password", "hunter2"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual([(c, s.get("set_text"), s.get("field"), s.get("tap_label")) for c, s in calls],
                         [("act", "hunter2", "Password", None),   # the one-op fill: not on a native screen
                          ("act", None, None, "Password"),        # the field tapped by its label
                          ("act", "hunter2", None, None)])        # then the text into the focused field
        # the helper's tap waited for the screen to settle: no pause of the CLI's own
        self.assertNotIn(mock.call(0.5), slept.call_args_list)
        self.assertIn("typed 7 chars", out.getvalue())
        self.assertNotIn("into Password", out.getvalue())

    def test_type_with_a_field_tapped_a_moment_ago_does_not_tap_it_again(self):
        # the review of Oct 7: the helper had no box yet for the bar just
        # tapped, and the CLI's fallback tapped the bar a second time
        self.allow("u2_invalidate")
        self.allow("nav_record")
        calls = []

        def u2(cmd, arg="", timeout=30):
            spec = json.loads(arg) if cmd == "act" else {}
            calls.append((cmd, spec))
            if cmd == "act" and spec.get("field"):
                pc._u2_status = ("err act not sent: these words were tapped a moment ago, and no text box "
                                 "has the focus yet")
                return None
            return SAMPLE_XML
        self.allow("u2sock", side_effect=u2)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_type(self.parse(["type", "--field", "Search or ask Play", "espn"]))
        self.assertEqual(rc, 1)
        self.assertEqual([(c, s.get("field"), s.get("tap_label")) for c, s in calls],
                         [("act", "Search or ask Play", None)])  # no tap, no typing elsewhere
        self.assertIn("isn't tapped again", err.getvalue())

    def test_an_action_batch_is_never_replayed_on_a_stale_stream(self):
        mod = _u2mux()
        import http.client as hc
        core = mock.Mock(HTTPResponse=lambda content: SimpleNamespace(content=content),
                         HTTPError=Exception)

        class Resp:
            status, reason, will_close = 200, "OK", False

            def read(self):
                return b"[]"

            def getheader(self, name):
                return ""

        def conn(stale):
            c = SimpleNamespace(requests=[], sock=SimpleNamespace(settimeout=lambda t: None))
            c.request = lambda method, path, body=None, headers=None: c.requests.append(path)
            c.getresponse = (mock.Mock(side_effect=hc.RemoteDisconnected("closed")) if stale
                             else (lambda: Resp()))
            c.close = lambda: None
            return c
        dev = SimpleNamespace(serial="s")
        with mock.patch.dict(sys.modules, {"uiautomator2": mock.Mock(core=core), "uiautomator2.core": core}):
            # a read on a stale reused stream is sent again on a fresh one
            ka = mod.KeepAliveHTTP()
            first, second = conn(stale=True), conn(stale=False)
            ka._conns[(dev.serial, 9008)] = [first, mod._time.monotonic()]
            ka._open = mock.Mock(return_value=second)
            ka.request(dev, 9008, "POST", "/jsonrpc/0", data=[{"method": "dumpWindowHierarchy"}])
            self.assertEqual((first.requests, second.requests), (["/jsonrpc/0"], ["/jsonrpc/0"]))
            # an action batch is not: the server may have acted before it closed
            ka = mod.KeepAliveHTTP()
            first, second = conn(stale=True), conn(stale=False)
            ka._conns[(dev.serial, 9008)] = [first, mod._time.monotonic()]
            ka._open = mock.Mock(return_value=second)
            with self.assertRaises(hc.RemoteDisconnected):
                ka.request(dev, 9008, "POST", "/jsonrpc/0", data=[{"method": "click"}], replay=False)
            self.assertEqual(second.requests, [])
            # a re-open that fails after the request went out once is not "not sent"
            ka = mod.KeepAliveHTTP()
            first = conn(stale=True)
            ka._conns[(dev.serial, 9008)] = [first, mod._time.monotonic()]
            ka._open = mock.Mock(side_effect=OSError("refused"))
            with self.assertRaises(hc.RemoteDisconnected):
                ka.request(dev, 9008, "POST", "/jsonrpc/0", data=[{"method": "dumpWindowHierarchy"}])
            # the batch sender tells the two apart
            dm = EmptyScreenTests._daemon(self, mod)
            dm.d = SimpleNamespace(_dev=dev, _device_server_port=9008)
            seen = []
            with mock.patch.object(mod._KEEPALIVE, "request",
                                   side_effect=lambda *a, **k: seen.append(k.get("replay")) or SimpleNamespace(json=lambda: [])):
                dm._batch([("wakeUp", []), ("click", [1, 2])])
                dm._batch([("dumpWindowHierarchy", [False, 50])])
            self.assertEqual(seen, [False, True])

    def test_an_act_whose_stream_would_not_open_is_not_sent(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([])
        dm._last_xml, dm._last_xml_t = SAMPLE_XML, mod._time.monotonic()
        dm._batch = mock.Mock(side_effect=mod.StreamUnavailable("Unable to connect to uiautomator2 server: closed"))
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"tap": [540, 505], "idle": 1200}))
        self.assertEqual(str(cm.exception), "act not sent: the UI server couldn't be reached "
                         "(Unable to connect to uiautomator2 server: closed)")
        # the stream opener's failure is that error
        ka = mod.KeepAliveHTTP()
        ka._open = mock.Mock(side_effect=OSError("closed"))
        core = mock.Mock(HTTPResponse=object, HTTPError=Exception)
        with mock.patch.dict(sys.modules, {"uiautomator2": mock.Mock(core=core), "uiautomator2.core": core}),                 self.assertRaises(mod.StreamUnavailable):
            ka.request(SimpleNamespace(serial="s"), 9008, "GET", "/ping")

    def test_a_read_that_says_hidden_ends_the_proof(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        self._fake_cdp(mod, [])
        dm = EmptyScreenTests._daemon(self, mod)
        dm._web = _FakePage()
        dm._web.visible_at = 1e9
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        dm._page_xml(dict(WEB_SCREEN, vis="visible"))
        self.assertLess(abs(dm._web.visible_at - mod._time.monotonic()), 5.0)  # the proof, stamped
        dm._page_xml(dict(WEB_SCREEN, vis="hidden"))  # a tap opened another tab
        self.assertEqual(dm._web.visible_at, 0.0)

    def test_helper_looks_at_the_screen_when_the_page_left_the_front(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = self._fake_cdp(mod, calls)
        fake.visible = lambda page, timeout=1.5: False  # the page in hand is hidden now
        fake.front_page = lambda dev, current=None, **kw: self.fail("no tab scan with another app in front")

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

    def test_a_label_not_on_the_page_is_tapped_on_chromes_own_prompt(self):
        # a permission ask or "Save password?" is Chrome's, not the page's:
        # the screen reader has it, and the row is tapped where it is
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = self._fake_cdp(mod, calls)
        fake.tap = lambda page, label, index=None, idle_ms=1200: calls.append(("tap", label)) or {"found": False, "screen": WEB_SCREEN}
        prompt = CHROME_XML.replace(
            '<node text="" class="android.webkit.WebView"',
            '<node text="Allow" class="android.widget.Button" package="com.android.chrome"'
            ' bounds="[600,1500][900,1600]" clickable="true" enabled="true"/>'
            '<node text="" class="android.webkit.WebView"')
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([prompt])
        sent = []
        dm._batch = lambda cs, timeout=45.0: sent.append(cs) or [None] * (len(cs) - 1) + [prompt]
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        dm.cmd_act(json.dumps({"tap_label": "Allow", "idle": 900}))
        self.assertEqual(calls[0], ("tap", "Allow"))
        self.assertEqual(sent[0][1], ("click", [750, 1550]))
        # no such row on the screen reader's read either: not sent
        dm.d = _FakeServer([CHROME_XML])
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"tap_label": "Nope"}))
        self.assertEqual(str(cm.exception), "act not sent: not on the page")

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
            "FIND_JS": on_button, "FILL_JS": {"ok": False, "why": "not a field"},
            "TARGET_FILL_JS": {"target": {"ok": True, "how": "focus", "label": "search wikipedia"},
                               "filled": {"ok": True, "mode": "insert"}},
            "FILLED_JS": {"ok": True, "value": "Pixel 7"}, "READ_JS": WEB_SCREEN})
        with mock.patch.object(cdp.time, "sleep"):
            r = cdp.fill(page, "Search", "Pixel 7")
        self.assertEqual((r["found"], r["how"], r["value"]), (True, "tap+focus", "Pixel 7"))
        self.assertEqual(page.calls, [
            ("many", ["FIND_JS", "FILL_JS", "GUARD_JS"]),  # the fill rides with the find: a field takes it there
            ("many", ["Input.dispatchTouchEvent", "Input.dispatchTouchEvent", "VERDICT_JS"]),  # not a field: touched
            ("eval", "TARGET_FILL_JS"),  # the field that opened, waited for and filled in one script
            ("many", ["Input.insertText", "FILLED_JS", "READ_JS"])])
        # the touch led to a page with the box (no focus): the field with the label, on the second look
        page = _ScriptedPage(cdp, {
            "FIND_JS": on_button, "FILL_JS": {"ok": False, "why": "not a field"},
            "TARGET_FILL_JS": [{"target": {"ok": False, "fields": 0}, "filled": None},
                               {"target": {"ok": True, "how": "label", "label": "search wikipedia"},
                                "filled": {"ok": True, "mode": "insert"}}],
            "FILLED_JS": {"ok": True, "value": "Pixel 7"}, "READ_JS": WEB_SCREEN})
        with mock.patch.object(cdp.time, "sleep"):
            r = cdp.fill(page, "Search", "Pixel 7")
        self.assertEqual(r["how"], "tap+label")
        self.assertEqual([c for c in page.calls if c[0] == "eval"], [("eval", "TARGET_FILL_JS")] * 2)

    def test_fill_says_when_the_button_opened_no_field(self):
        cdp = _cdp()
        page = _ScriptedPage(cdp, {
            "FIND_JS": {"found": True, "count": 1, "label": "search", "notField": True, "tag": "span",
                        "x": 305, "y": 27, "url": "u"}, "FILL_JS": {"ok": False, "why": "not a field"},
            "TARGET_FILL_JS": {"target": {"ok": False, "fields": 2, "named": 0}, "filled": None},
            "READ_JS": WEB_SCREEN})
        with mock.patch.object(cdp.time, "sleep"), self.assertRaises(cdp.NotDone) as cm:
            cdp.fill(page, "Search", "Pixel 7")
        self.assertEqual(str(cm.exception),
                         "'Search' is a span, not a field; tapping it opened 2 text fields, none labelled 'Search'")
        self.assertNotIn("Input.insertText", [n for c in page.calls if c[0] == "many" for n in c[1]])

    def test_fill_of_a_field_touches_nothing(self):
        cdp = _cdp()
        page = _ScriptedPage(cdp, {
            "FIND_JS": {"found": True, "count": 1, "label": "email", "tag": "input", "type": "email", "url": "u"},
            "FILL_JS": {"ok": True, "mode": "insert"}, "FILLED_JS": {"ok": True, "value": "a@b.c"},
            "READ_JS": WEB_SCREEN})
        with mock.patch.object(cdp.time, "sleep"):
            r = cdp.fill(page, "Email", "a@b.c")
        self.assertEqual((r["how"], r["value"], r["screen"]), ("fill", "a@b.c", WEB_SCREEN))
        # two round trips: find + fill, then the text, the check and the read
        self.assertEqual(page.calls, [("many", ["FIND_JS", "FILL_JS", "GUARD_JS"]),
                                      ("many", ["Input.insertText", "FILLED_JS", "READ_JS"])])

    def test_a_read_that_fails_after_an_untouched_answer_is_not_sent(self):
        cdp = _cdp()
        page = _ScriptedPage(cdp, {"FIND_JS": {"found": False}})
        page.eval = mock.Mock(side_effect=[OSError("stream closed")])  # the read after the two finds
        with mock.patch.object(cdp.time, "sleep"), self.assertRaises(cdp.NotSent):
            cdp.tap(page, "Nope")
        page = _ScriptedPage(cdp, {"FIND_JS": {"found": True, "count": 2, "labels": ["a", "b"]}})
        page.eval = mock.Mock(side_effect=[OSError("stream closed")])
        with self.assertRaises(cdp.NotSent):
            cdp.tap(page, "Twice")

    def test_page_text_is_cleaned_of_what_xml_cannot_carry(self):
        cdp = _cdp()
        screen = dict(WEB_SCREEN, title="t\x00itle", rows=[
            {"text": "a\x01b\ud800c", "desc": "d\x1fe", "kind": "text", "click": False, "l": 0, "t": 0, "w": 10, "h": 10}])
        xml = cdp.page_xml(screen, 283, 2400)
        root = ET.fromstring(xml)
        xml.encode("utf-8")
        texts = [n.get("text") for n in root.iter("node")]
        self.assertIn("abc", texts)
        self.assertIn("de", [n.get("content-desc") for n in root.iter("node")])
        self.assertIn("title", xml)
        self.assertNotIn("\x00", xml)

    def test_a_tap_on_a_link_waits_for_its_load_to_start(self):
        # airbnb.com, Oct 5: a listing's link touched, the read 250ms later
        # still the list, the listing up half a second after that
        cdp = _cdp()
        here, there = "https://www.airbnb.com/s/homes", "https://www.airbnb.com/rooms/1"
        found = {"found": True, "count": 1, "label": "guest suite", "x": 164, "y": 650, "url": here, "href": there}
        old, new = dict(WEB_SCREEN, url=here), dict(WEB_SCREEN, url=there, title="the listing")
        waits = []

        class Late(_ScriptedPage):
            def wait_loading(self, cap_s):
                waits.append(cap_s)
                self.loading = True  # the load started within the wait
                return True

            def wait_parsed(self, cap_s):
                self.loading = False
                return True
        page = Late(cdp, {"FIND_JS": found, "READ_JS": [old, new]})
        r = cdp.tap(page, "guest suite")
        self.assertEqual((r["screen"]["title"], r["ready"], waits), ("the listing", "complete", [cdp.LINK_LOAD_S]))
        # the page already elsewhere a moment after the touch, a link to
        # the page itself, or no link: no wait
        for hit, screen in ((found, new), (dict(found, href=here + "#top"), old), (dict(found, href=""), old)):
            self.assertFalse(cdp.leaves_for(hit, screen))
        self.assertTrue(cdp.leaves_for(found, old))
        self.assertTrue(cdp.leaves_for(found, {}))  # a read without an address: not gone yet
        waits.clear()
        page = Late(cdp, {"FIND_JS": dict(found, href=""), "READ_JS": old})
        self.assertEqual((cdp.tap(page, "guest suite")["screen"], waits), (old, []))

    def test_a_read_waits_for_rows_that_say_loading(self):
        # Coinbase's Bitcoin page (Oct 7): the open's read had the price
        # still "Loading", and the assistant took a screenshot to see it
        cdp = _cdp()
        row = lambda text, desc="": {"text": text, "desc": desc, "kind": "text", "l": 0, "t": 100, "w": 50, "h": 20}
        price = dict(WEB_SCREEN, rows=WEB_SCREEN["rows"] + [row("$84,047.70")])
        loading = dict(WEB_SCREEN, rows=WEB_SCREEN["rows"] + [row("Loading"), row("Loading\u2026"),
                                                                row("", "Loading price chart")])
        self.assertEqual(cdp.placeholders(loading), 3)
        for words in ("Loading", "loading...", "Please wait", "Loading \u2026"):
            self.assertEqual(cdp.placeholders({"rows": [row(words)]}), 1, words)
        # words about loading are content, and a label beside words is not a skeleton's
        for text, desc in (("Loading dock for sale", ""), ("Bitcoin", "Loading"), ("", "Download"),
                           ("Loadings", ""), ("", "Loadings")):
            self.assertEqual(cdp.placeholders({"rows": [row(text, desc)]}), 0, (text, desc))
        self.assertEqual(cdp.placeholders({}), 0)
        clock = [0.0]

        def sleep(s):
            clock[0] += s
        with mock.patch.object(cdp.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(cdp.time, "sleep", side_effect=sleep):
            # the open: read again until the rows are filled; a read
            # between documents (no rows) doesn't stand
            page = _ScriptedPage(cdp, {"READ_JS": [loading, dict(loading, rows=[]), loading, price]})
            r = cdp.navigate(page, "https://www.coinbase.com/price/bitcoin")
            self.assertEqual(r["screen"]["rows"], price["rows"])
            self.assertEqual(r["screen"]["busy"][:2], [3, 0])
            self.assertEqual([c for c in page.calls if c == ("eval", "READ_JS")], [("eval", "READ_JS")] * 4)
            # filled at once: one read, nothing added
            page = _ScriptedPage(cdp, {"READ_JS": price})
            self.assertEqual(cdp.navigate(page, "https://example.com")["screen"], price)
            # never filled: BUSY_CAP_S at most, the last read printed
            clock[0] = 0.0
            page = _ScriptedPage(cdp, {"READ_JS": loading})
            t0 = clock[0]
            r = cdp.navigate(page, "https://example.com")
            self.assertEqual(r["screen"]["busy"][:2], [3, 3])
            self.assertLessEqual(clock[0] - t0, cdp.BUSY_CAP_S + 0.2 + cdp.BUSY_POLL_S + 0.01)
            # a tap's read waits the same way
            found = {"found": True, "count": 1, "label": "price", "x": 300, "y": 400, "url": "u"}
            page = _ScriptedPage(cdp, {"FIND_JS": found, "READ_JS": [loading, price]})
            r = cdp.tap(page, "Price")
            self.assertEqual(r["screen"]["rows"], price["rows"])
            # a read again that fails leaves the last one standing
            page = _ScriptedPage(cdp, {"READ_JS": loading})
            page.eval = mock.Mock(side_effect=RuntimeError("the document went away"))
            self.assertEqual(cdp.read_loaded(page, loading)["rows"], loading["rows"])

    def test_tap_reads_the_page_in_the_touch_round_trip(self):
        cdp = _cdp()
        found = {"found": True, "count": 1, "label": "box score", "x": 300, "y": 400, "url": "u"}
        page = _ScriptedPage(cdp, {"FIND_JS": found, "READ_JS": WEB_SCREEN})
        r = cdp.tap(page, "Box Score")
        self.assertEqual((r["how"], r["ready"], r["screen"]), ("touch", "complete", WEB_SCREEN))
        # the guard installed in the find's trip, its verdict in the touch's
        self.assertEqual(page.calls, [("many", ["FIND_JS", "GUARD_JS"]),
                                      ("many", ["Input.dispatchTouchEvent", "Input.dispatchTouchEvent",
                                                "VERDICT_JS", "READ_JS"])])
        # the touch started a load: the read that rode along is not the
        # new page; it is waited out and read afresh
        page = _ScriptedPage(cdp, {"FIND_JS": found, "READ_JS": WEB_SCREEN, "navigates": True})
        r = cdp.tap(page, "Box Score")
        self.assertEqual(page.calls[-1], ("eval", "READ_JS"))
        self.assertEqual(r["screen"], WEB_SCREEN)

    def test_a_wait_poll_that_lands_reads_in_its_round_trip(self):
        cdp = _cdp()
        hit = {"found": True, "label": "box score", "l": 10, "t": 20, "w": 100, "h": 30, "inview": True,
               "dpr": 2, "enabled": True, "count": 1}
        page = _ScriptedPage(cdp, {"FIND_JS": hit, "READ_JS": WEB_SCREEN})
        n, screen = cdp.find_read(page, "Box Score", top=283)
        self.assertEqual((n["found"], n["bounds"], screen), (True, "[20,323][220,383]", WEB_SCREEN))
        self.assertEqual(page.calls, [("many", ["FIND_JS", "READ_JS"])])

    def test_scroll_is_one_round_trip(self):
        cdp = _cdp()
        page = _ScriptedPage(cdp, {"SCROLL_JS": {"moved": 1200, "scroller": "page"}, "READ_JS": WEB_SCREEN})
        r = cdp.scroll(page, "down", times=2)
        # the read, with how far the page moved (see page_stuck)
        self.assertEqual((r["moved"], r["screen"]), (2400, dict(WEB_SCREEN, moved=2400, held=False)))
        self.assertEqual(page.calls, [("many", ["SCROLL_JS", "SCROLL_JS", "READ_JS"])])

    def test_type_takes_the_one_field_in_view_when_nothing_has_the_focus(self):
        cdp = _cdp()
        page = _ScriptedPage(cdp, {"SELECT_JS": {"ok": True, "direct": False, "type": "search", "only": True},
                                   "READ_JS": WEB_SCREEN})
        with mock.patch.object(cdp.time, "sleep"):
            r = cdp.type_text(page, "Pixel 7")
        self.assertTrue(r["only"])
        self.assertEqual(r["screen"], WEB_SCREEN)
        self.assertEqual(page.calls, [("many", ["SELECT_JS", "Input.insertText", "READ_JS"])])
        page = _ScriptedPage(cdp, {"SELECT_JS": {"ok": False, "fields": 2}})
        with self.assertRaises(cdp.NotSent) as cm:
            cdp.type_text(page, "Pixel 7")
        self.assertEqual(str(cm.exception), "no field has the focus on the page "
                         "(2 text fields in view; tap one, or type --field with its label)")

    def test_helper_says_nothing_was_tapped_when_every_touch_was_held(self):
        # the review of Oct 7: a target the page keeps moving is never
        # tapped where it was; the tap says nothing was tapped, and the
        # page's session stays
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = self._fake_cdp(mod, calls)
        msg = "nothing was tapped: the page moved 'More' from under the touch 3 times (moved, div under the point)"

        def tap(page, label, index=None, idle_ms=1200):
            raise fake.Held(msg, "moved")
        fake.tap = tap
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([])
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"tap_label": "More"}))
        self.assertEqual(str(cm.exception), "act failed after sending: " + msg)
        self.assertIsNotNone(dm._web)

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
        woke = []
        dm.d.jsonrpc_call = lambda method, params, timeout=10: woke.append((method, timeout))
        dm._last_xml = SAMPLE_XML  # not Chrome: launched by the CLI instead
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"open": "https://example.com"}))
        self.assertEqual(str(cm.exception), "act not sent: not a page")
        # the screen woken for the launch that follows (Chrome came up
        # behind an off screen: an 11s open, Oct 7)
        self.assertEqual(woke, [("wakeUp", mod.WAKE_RPC_S)])  # bounded: it holds the lock
        # a wake that fails says so in the log, and the answer stands
        dm.d.jsonrpc_call = mock.Mock(side_effect=OSError("link down"))
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
        # the page moved nothing: said, with why (allrecipes.com, Oct 7:
        # "scrolled down" again and again over a page an ad held)
        page = _cdp().page_xml(dict(WEB_SCREEN, moved=0, held=True), 283, 2400)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_scroll(SimpleNamespace(direction="down", times=1, to=None, quiet=False))
        self.assertIn("the page didn't move down: something over it holds it (a pop-up or a menu); "
                      "close that first", out.getvalue())
        self.assertNotIn("scrolled down", out.getvalue())
        # a search on such a page stops at the first scroll, not the tenth
        calls.clear()
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_scroll(SimpleNamespace(direction="down", times=1, to="Preheat", quiet=True))
        self.assertEqual(rc, 1)
        # the page asked for the words first (not on it here), then one scroll
        self.assertEqual([c[1].get("scroll_to") or c[1].get("scroll") for c in calls if c[0] == "act"],
                         ["Preheat", "down"])
        self.assertIn("not found: Preheat (the page didn't move down at scroll 1: something over it holds it",
                      err.getvalue())
        # moved, but the rows stayed as they were: the same stop
        page = _cdp().page_xml(dict(WEB_SCREEN, moved=900), 283, 2400)
        self.allow("ui_dump", return_value=ET.fromstring(page))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_scroll(SimpleNamespace(direction="down", times=1, to="Preheat", quiet=True))
        self.assertEqual(rc, 1)
        self.assertIn("its rows stayed as they were", err.getvalue())
        self.assertEqual(pc.page_stuck(ET.fromstring(_cdp().page_xml(dict(WEB_SCREEN, moved=0), 283, 2400))),
                         "it is at its end, or a box over it takes the scroll")
        self.assertEqual(pc.page_stuck(ET.fromstring(_cdp().page_xml(WEB_SCREEN, 283, 2400))), "")
        # moved, but held by a pop-up (a finger couldn't have): said
        page = _cdp().page_xml(dict(WEB_SCREEN, moved=900, held=True), 283, 2400)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            pc.cmd_scroll(SimpleNamespace(direction="down", times=1, to=None, quiet=True))
        self.assertIn("scrolled down x1\n  something over the page holds it (a pop-up or a menu); "
                      "close that to tap the rows under it", out.getvalue())

    def test_text_prints_the_words_uncut(self):
        # Reddit, Oct 7: rows cut the JSON answer at 160 characters, and the
        # assistant went around burner (Chrome's DevTools port over adb)
        cdp = _cdp()
        body = '{"kind": "Listing", "data": {"children": [' + ", ".join('{"title": "post %d"}' % i for i in range(400)) + "]}}"
        page = _ScriptedPage(cdp, {"TEXT_JS": {"title": "", "url": "https://www.reddit.com/r/androiddev/top.json",
                                               "total": len(body), "text": body[:20000]}})
        self.assertEqual(cdp.page_text(page)["total"], len(body))
        self.assertEqual(page.calls, [("eval", "TEXT_JS")])
        sent = []

        def u2(cmd, arg="", timeout=30):
            sent.append((cmd, json.loads(arg)))
            p = json.loads(arg)
            return json.dumps({"title": "", "url": "https://www.reddit.com/r/androiddev/top.json",
                               "total": len(body), "text": body[p["from"]:p["from"] + p["max"]]})
        self.allow("u2sock", side_effect=u2)
        with self.cap() as (out, err):
            rc = pc.cmd_text(self.parse(["text"]))
        self.assertEqual(rc, 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "page: (no title) (https://www.reddit.com/r/androiddev/top.json), {} characters"
                         .format(len(body)))
        self.assertIn('{"title": "post 399"}', out.getvalue())  # the whole answer, uncut
        self.assertEqual(sent, [("text", {"from": 0, "max": pc.TEXT_MAX})])
        # a long one, a part at a time; the cut said on stderr, where a
        # `| grep` keeps it in sight
        with self.cap() as (out, err):
            pc.cmd_text(self.parse(["text", "--max", "100"]))
        self.assertIn("(cut at 100 of {}: `burner text --from 100` for the rest)".format(len(body)), err.getvalue())
        self.assertNotIn("cut at", out.getvalue())
        with self.cap() as (out, err):
            pc.cmd_text(self.parse(["text", "--from", "100", "--max", "50"]))
        self.assertIn(", from 100\n" + body[100:150] + "\n", out.getvalue())
        # not a page: every row's words, uncut, the status bar and the keyboard aside
        long_words = ("A long message " + "word " * 60).strip()
        xml = ('<hierarchy rotation="0"><node text="" class="android.widget.FrameLayout" package="com.example" '
               'bounds="[0,0][1080,2400]"><node text="12:01" package="com.android.systemui" class="android.widget.TextView" '
               'bounds="[0,0][90,40]"/><node text="%s" package="com.example" class="android.widget.TextView" '
               'bounds="[0,300][1080,900]"/><node text="Send" package="com.example" class="android.widget.Button" '
               'bounds="[800,1000][1000,1100]" clickable="true"/><node text="q" package="com.google.android.inputmethod.latin" '
               'class="android.widget.TextView" bounds="[0,1800][100,1900]"/></node></hierarchy>') % long_words
        self.allow("u2sock", return_value=None)
        self.allow("ui_dump", return_value=ET.fromstring(xml))
        self.allow("screen_dims", return_value=(1080, 2400))
        with self.cap() as (out, err):
            rc = pc.cmd_text(self.parse(["text"]))
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().splitlines()[1:], [long_words, "Send"])
        self.assertTrue(out.getvalue().startswith("screen: com.example, "))
        # the helper: the page's words as JSON; elsewhere "not a page"
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = self._fake_cdp(mod, calls)
        fake.page_text = lambda page, start=0, count=20000: {"title": "t", "url": "u", "total": 5,
                                                            "text": "hello"[start:start + count]}
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([])
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        self.assertEqual(json.loads(dm.cmd_text(json.dumps({"from": 1, "max": 3})))["text"], "ell")
        dm._last_xml = SAMPLE_XML
        with self.assertRaises(mod.U2NotFound):
            dm.cmd_text("")
        # a plain miss: no retry (one more round trip for the same answer)
        dm.d = mock.Mock()
        self.assertEqual(dm.handle("text {}"), b"__NOT_FOUND__")
        dm.d.info.assert_not_called()

    def test_text_counts_places_as_the_page_does(self):
        # review, Oct 7: places counted here (code points) against the page's
        # total (UTF-16 units) said "cut" on a page with emoji, and a cut
        # inside an emoji sent half of it, which crashed the print
        emoji = chr(0x1F600)
        whole = "Bake " + emoji * 3 + " at 350"
        units = len(whole.encode("utf-16-le")) // 2
        answers = []

        def u2(cmd, arg="", timeout=30):
            return answers.pop(0)
        self.allow("u2sock", side_effect=u2)
        answers.append(json.dumps({"title": "Bread", "url": "u", "total": units, "start": 0, "next": units,
                                   "text": whole}))
        with self.cap() as (out, err):
            self.assertEqual(pc.cmd_text(self.parse(["text"])), 0)
        self.assertEqual(out.getvalue().splitlines()[1], whole)
        self.assertNotIn("cut at", out.getvalue() + err.getvalue())
        # a lone half and control characters: printable, nothing lost but them
        broken = json.dumps({"title": "T" + chr(7), "url": "u", "total": 40, "start": 0, "next": 9,
                             "text": "ab" + chr(0xD83D) + "c" + chr(27) + "[2Jd\r\ne"})
        answers.append(broken)
        buf = io.BytesIO()
        out = io.TextIOWrapper(buf, encoding="utf-8")  # errors="strict", as a real stdout
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", io.StringIO()) as err:
            self.assertEqual(pc.cmd_text(self.parse(["text"])), 0)
        out.flush()
        lines = buf.getvalue().decode("utf-8").splitlines()
        self.assertEqual(lines[0], "page: T (u), 40 characters")
        self.assertEqual(lines[1:], ["ab" + chr(0xFFFD) + "c[2Jd", "e"])
        self.assertIn("(cut at 9 of 40: `burner text --from 9` for the rest)", err.getvalue())
        # Chrome in front, its page out of reach a moment ago ("not a
        # page"): its rows, and why (review, Oct 7)
        answers.append(pc.U2_NOT_FOUND)
        self.allow("ui_dump", return_value=ET.fromstring(CHROME_XML))
        self.allow("screen_dims", return_value=(1080, 2400))
        with self.cap() as (out, err):
            self.assertEqual(pc.cmd_text(self.parse(["text"])), 0)
        self.assertIn("the page's own text couldn't be read; the screen's rows instead", err.getvalue())
        # C1 controls and direction marks: out
        self.assertEqual(pc._readable("a" + chr(0x9B) + "2Jb" + chr(0x202E) + "c" + chr(0x2066) + "d"), "a2Jbcd")
        # the page couldn't be read in Chrome: its rows, and why
        answers.append(None)
        self.allow("ui_dump", return_value=ET.fromstring(CHROME_XML))
        self.allow("screen_dims", return_value=(1080, 2400))
        with self.cap() as (out, err):
            self.assertEqual(pc.cmd_text(self.parse(["text"])), 0)
        self.assertIn("the page's own text couldn't be read; the screen's rows instead", err.getvalue())
        self.assertTrue(out.getvalue().startswith("screen: com.android.chrome, "))
        # read-only: never recorded into a flow
        with mock.patch.object(sys, "argv", ["burner", "text", "--from", "5"]):
            self.assertIsNone(pc.record_command_line(self.parse(["text", "--from", "5"])))

    def test_scroll_to_on_a_page_asks_the_page(self):
        # allrecipes.com, Oct 7: ten scrolls (469px each) never reached
        # "Preheat" (7.5s); the page holds its words below the screen too
        cdp = _cdp()
        directions = dict(WEB_SCREEN, rows=WEB_SCREEN["rows"] + [
            {"text": "Preheat oven to 350 degrees F", "desc": "", "kind": "text", "l": 0, "t": 400, "w": 500, "h": 40}])
        page = _ScriptedPage(cdp, {"SCROLL_TO_JS": {"found": True, "moved": 5191, "used": "preheat"},
                                   "READ_JS": directions})
        r = cdp.scroll_to(page, "Preheat", "down")
        self.assertEqual((r["found"], r["moved"], r["screen"]["found"]), ("1", 5191, "1"))
        self.assertEqual(page.calls, [("many", ["SCROLL_TO_JS", "READ_JS"])])
        self.assertIn('page="1" found="1">', cdp.page_xml(r["screen"], 283, 2400))
        page = _ScriptedPage(cdp, {"SCROLL_TO_JS": {"found": False, "other": True}, "READ_JS": WEB_SCREEN})
        self.assertEqual(cdp.scroll_to(page, "Preheat", "down")["found"], "other")
        # the CLI: one trip, the row found, no scroll
        self.allow("wake")
        self.allow("u2_invalidate")
        calls = []
        replies = {"1": cdp.page_xml(dict(directions, found="1"), 283, 2400),
                   "other": cdp.page_xml(dict(WEB_SCREEN, found="other"), 283, 2400)}
        answer = ["1"]

        def u2(cmd, arg="", timeout=30):
            calls.append(json.loads(arg) if arg.startswith("{") else arg)
            return replies[answer[0]]
        self.allow("u2sock", side_effect=u2)
        self.allow("ui_dump", return_value=ET.fromstring(cdp.page_xml(WEB_SCREEN, 283, 2400)))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_scroll(SimpleNamespace(direction="down", times=1, to="Preheat", quiet=True))
        self.assertEqual((rc, out.getvalue()), (0, 'found: Preheat (in "Preheat oven to 350 degrees F")\n'))
        self.assertEqual([c.get("scroll_to") for c in calls], ["Preheat"])
        # only above: said, with the way there, and no scroll
        answer[0], calls[:] = "other", []
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_scroll(SimpleNamespace(direction="down", times=1, to="Preheat", quiet=True))
        self.assertEqual(rc, 1)
        self.assertIn("not found below: Preheat (the page has it above: `burner scroll up --to \"Preheat\"`)",
                      err.getvalue())
        self.assertEqual(len(calls), 1)
        # the helper: the page's answer, logged, as a screen read
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        seen = []
        fake = self._fake_cdp(mod, seen)
        fake.scroll_to = lambda page, label, direction="down": seen.append(("scroll_to", label, direction)) or {
            "found": "1", "used": "preheat", "moved": 5191, "screen": dict(directions, found="1")}
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([])
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        xml = dm.cmd_act(json.dumps({"scroll_to": "Preheat", "direction": "down", "idle": 500})).decode()
        self.assertIn('found="1"', xml)
        self.assertIn("Preheat oven to 350 degrees F", xml)
        self.assertEqual(seen[-1], ("scroll_to", "Preheat", "down"))

    def test_scroll_to_on_a_page_trusts_where_the_words_are_drawn(self):
        # review, Oct 7: "found" for words off the screen (a list on it held
        # them below), and "found" with no row to show for it
        cdp = _cdp()
        page = _ScriptedPage(cdp, {"SCROLL_TO_JS": {"found": True, "moved": 0, "used": "target phrase",
                                                    "text": "Filler words go here. Filler\u2026"},
                                   "READ_JS": WEB_SCREEN})
        r = cdp.scroll_to(page, "target phrase")
        self.assertEqual((r["found"], r["near"], r["screen"]["found_in"]), ("1", False, "Filler words go here. Filler\u2026"))
        self.assertIn('found="1" found_in="Filler words go here. Filler\u2026">', cdp.page_xml(r["screen"], 283, 2400))
        for answer, found, near in (({"found": False, "other": True, "near": True}, "other", True),
                                    ({"found": False, "other": True, "near": False}, "other", False),
                                    ({"found": False, "hidden": True}, "hidden", False),
                                    ({"found": False}, "0", False), (None, "0", False)):
            page = _ScriptedPage(cdp, {"SCROLL_TO_JS": answer, "READ_JS": WEB_SCREEN})
            r = cdp.scroll_to(page, "Preheat")
            self.assertEqual((r["found"], r["near"]), (found, near))
            xml = cdp.page_xml(r["screen"], 283, 2400)
            self.assertIn(' found="%s"' % found + (' near="1"' if near else "") + ">", xml)
            self.assertNotIn("found_in", xml)
        # the CLI
        self.allow("wake")
        self.allow("u2_invalidate")
        calls = []
        replies = []

        def u2(cmd, arg="", timeout=30):
            calls.append(json.loads(arg) if arg.startswith("{") else arg)
            return replies.pop(0) if len(replies) > 1 else replies[0]
        self.allow("u2sock", side_effect=u2)
        self.allow("ui_dump", return_value=ET.fromstring(cdp.page_xml(WEB_SCREEN, 283, 2400)))
        # in view, in a row too long to read the words: that row named
        replies[:] = [cdp.page_xml(dict(WEB_SCREEN, found="1", found_in="Filler words go here. Filler\u2026"),
                                   283, 2400)]
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_scroll(SimpleNamespace(direction="down", times=1, to="target phrase", quiet=True))
        self.assertEqual((rc, out.getvalue()), (0, 'found: target phrase (in "Filler words go here. Filler\u2026")\n'))
        # only above, and the page ends close by: the scrolls go on (a feed
        # may load more), each giving the page a moment to grow, and the
        # last word says where the page has it
        stuck = cdp.page_xml(dict(WEB_SCREEN, moved=0), 283, 2400)
        replies[:] = [cdp.page_xml(dict(WEB_SCREEN, found="other", near=True), 283, 2400), stuck]
        calls.clear()
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_scroll(SimpleNamespace(direction="down", times=1, to="Preheat", quiet=True))
        self.assertEqual(rc, 1)
        acts = [c for c in calls if isinstance(c, dict)]
        self.assertEqual([(a.get("scroll_to"), a.get("scroll"), a.get("grow")) for a in acts],
                         [("Preheat", None, None), (None, "down", pc.GROW_MS)])
        self.assertIn("not found: Preheat (the page didn't move down at scroll 1: it is at its end, or a box over "
                      "it takes the scroll; the page has it above: `burner scroll up --to \"Preheat\"`)", err.getvalue())
        # only where it isn't shown: the scrolls look on, and say so at the end
        moving = [cdp.page_xml(dict(WEB_SCREEN, moved=600, rows=[dict(WEB_SCREEN["rows"][0], text="row %d" % k)]),
                               283, 2400) for k in range(3)]
        replies[:] = [cdp.page_xml(dict(WEB_SCREEN, found="hidden"), 283, 2400)] + moving
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_scroll(SimpleNamespace(direction="down", times=2, to="Shipping details", quiet=True))
        self.assertEqual(rc, 1)
        self.assertIn("not found after 2 scrolls: Shipping details (the page has it only where it isn't shown, "
                      "such as a closed section or a slide aside)", err.getvalue())

    def test_a_search_scroll_at_the_end_waits_for_the_page_to_grow(self):
        # review, Oct 7: a search stopped at a feed's end while it loaded more.
        # The scrolls and the read wait each for the one before, in one trip.
        cdp = _cdp()
        page = _ScriptedPage(cdp, {"SCROLL_JS": {"moved": 600, "scroller": "page", "grew": True}, "READ_JS": WEB_SCREEN})
        sent = []
        real = page.call_many
        page.call_many = lambda cmds, timeout=10.0, raise_errors=True: sent.append((cmds, timeout)) or real(cmds, timeout, raise_errors)
        r = cdp.scroll(page, "down", times=2, grow_ms=1000)
        self.assertEqual((r["moved"], r["grew"]), (1200, True))
        cmds, timeout = sent[-1]
        exprs = [p["expression"] for _m, p in cmds]
        self.assertTrue(all(e.startswith("window.__burnerScroll = Promise.resolve(window.__burnerScroll)")
                            for e in exprs[:2]))
        self.assertTrue(exprs[2].startswith("Promise.resolve(window.__burnerScroll)"))
        self.assertIn(", null, 1000)", exprs[0])  # the scroll's wait, at most
        self.assertEqual(timeout, 12.0)
        # a plain scroll: no wait, no chain
        sent.clear()
        cdp.scroll(page, "down")
        self.assertIn(", null, 0)", sent[-1][0][0][1]["expression"])
        self.assertNotIn("__burnerScroll", sent[-1][0][0][1]["expression"])
        # no scroll answered: nothing said about moving (not "at its end")
        page = _ScriptedPage(cdp, {"READ_JS": WEB_SCREEN})
        real2 = page.call_many

        def failing(cmds, timeout=10.0, raise_errors=True):
            out = real2(cmds, timeout, raise_errors)
            return [RuntimeError("stream closed")] * (len(out) - 1) + out[-1:]
        page.call_many = failing
        r = cdp.scroll(page, "down")
        self.assertIsNone(r["moved"])
        self.assertNotIn("moved=", cdp.page_xml(r["screen"], 283, 2400))
        self.assertEqual(pc.page_stuck(ET.fromstring(cdp.page_xml(r["screen"], 283, 2400))), "")

    def test_a_target_scrolled_into_view_is_placed_in_the_finds_trip(self):
        # Hacker News, Oct 7: `tap More` paid a trip for the link's place
        # after the find had scrolled it into view
        cdp = _cdp()
        page = _ScriptedPage(cdp, {"PLACE_JS": {"x": 1, "y": 2}})
        touched = []
        with mock.patch.object(cdp, "touch", lambda pg, x, y, then=(): touched.append((x, y)) or ("touch", [])):
            cdp.touch_hit(page, {"x": 30, "y": 400, "moved": True, "settled": True})
            self.assertEqual((touched[-1], page.calls), ((30, 400), []))  # no trip for the place
            cdp.touch_hit(page, {"x": 30, "y": 400, "moved": True})  # an older find: placed afresh
            self.assertEqual((touched[-1], page.calls), ((1, 2), [("many", ["PLACE_JS", "GUARD_JS"])]))

    def test_a_touch_that_misses_its_target_is_held_and_aimed_again(self):
        # review of Oct 7: an image above "More" took its size after the
        # link's place was taken, and the touch opened the image; the page
        # judges each touch where it lands (GUARD_JS) and holds a miss back
        cdp = _cdp()
        page = _ScriptedPage(cdp, {"PLACE_JS": [{"x": 5, "y": 600}, {"x": 5, "y": 700}, {"x": 5, "y": 800}]})
        verdicts, touched = [], []

        def touch(pg, x, y, then=()):
            touched.append((x, y))
            return "touch", [{"result": {"value": verdicts.pop(0)}}, "read"]
        hit = {"x": 30, "y": 400, "moved": True, "settled": True, "used": "More"}
        with mock.patch.object(cdp, "touch", touch):
            verdicts[:] = [{"verdict": "moved", "hit": "img"}, {"verdict": "ok"}]
            self.assertEqual(cdp.touch_hit(page, hit, then=["READ"]), ("touch", ["read"]))
            self.assertEqual((touched, hit["held"]), ([(30, 400), (5, 600)], 1))
            self.assertEqual(page.calls, [("many", ["PLACE_JS", "GUARD_JS"])])  # placed again, armed again
            # held every time: nothing was tapped, and it says so
            verdicts[:] = [{"verdict": "moved", "hit": "div"}] * 3
            with self.assertRaises(cdp.Held) as cm:
                cdp.touch_hit(page, dict(hit, held=0), then=["READ"])
            self.assertTrue(str(cm.exception).startswith(
                "nothing was tapped: the page moved 'More' from under the touch 3 times (moved, div under the point;"))
            self.assertEqual(cm.exception.verdict, "moved")
            # the target gone (the page drew it again): nothing to aim at here
            verdicts[:] = [{"verdict": "gone"}]
            with self.assertRaises(cdp.Held) as cm:
                cdp.touch_hit(page, dict(hit), then=["READ"])
            self.assertEqual(cm.exception.verdict, "gone")
            # no verdict (nothing armed, or a frame took the touch): as before
            for v in (None, {"verdict": None}):
                verdicts[:] = [v]
                self.assertEqual(cdp.touch_hit(page, dict(hit), then=["READ"]), ("touch", ["read"]))
        # a verdict that can't be read (the touch started a load): it went in
        with mock.patch.object(cdp, "touch", lambda pg, x, y, then=(): ("touch", [RuntimeError("context destroyed"), "read"])):
            self.assertEqual(cdp.touch_hit(page, dict(hit), then=["READ"]), ("touch", ["read"]))
        # in a tap: a target the page replaced is found again by its words, once
        found = {"found": True, "count": 1, "label": "more", "used": "More", "x": 300, "y": 400, "url": "u"}
        page = _ScriptedPage(cdp, {"FIND_JS": found, "READ_JS": WEB_SCREEN})
        tries = []

        def touch_hit(pg, hit, then=()):
            tries.append(hit)
            if len(tries) == 1:
                raise cdp.Held("nothing was tapped: gone", "gone")
            return "touch", [{"result": {"value": WEB_SCREEN}}]
        with mock.patch.object(cdp, "touch_hit", touch_hit):
            self.assertEqual(cdp.tap(page, "More")["screen"], WEB_SCREEN)
        self.assertEqual((len(tries), page.calls.count(("many", ["FIND_JS", "GUARD_JS"]))), (2, 2))
        # held for moving: said as it is (the helper reports nothing was tapped)
        with mock.patch.object(cdp, "touch_hit", mock.Mock(side_effect=cdp.Held("nothing was tapped: x", "moved"))):
            with self.assertRaises(cdp.Held):
                cdp.tap(page, "More")

    def test_a_tap_that_opens_a_new_tab_follows_it(self):
        # weather.gov, Oct 7: 'Get Detailed info' opened a new tab; the tap
        # waited 1s for the old page, printed it, and `text` read it again
        cdp = _cdp()
        page = _ScriptedPage(cdp, {"FIND_JS": {"found": True, "count": 1, "label": "Get Detailed info", "x": 100,
                                               "y": 400, "tag": "a", "url": "https://www.weather.gov/",
                                               "href": "https://forecast.weather.gov/MapClick.php?x=1"},
                                   "READ_JS": WEB_SCREEN})
        page.loading = False

        # the page's session hears it (Page.windowOpen, sent before the touch's read comes back)
        heard = SimpleNamespace(main_frame="F1", loading=False, url="", window_opened=None)
        cdp.Page._event(heard, {"method": "Page.windowOpen", "params": {"url": "https://forecast.weather.gov/x"}})
        self.assertEqual(heard.window_opened, "https://forecast.weather.gov/x")

        def touch_hit(pg, hit, then=()):
            pg.window_opened = "https://forecast.weather.gov/MapClick.php?x=1"
            return "touch", [{"result": {"value": WEB_SCREEN}}]
        waits = []
        page.wait_loading = lambda s: waits.append(s)
        with mock.patch.object(cdp, "touch_hit", touch_hit):
            r = cdp.tap(page, "Get Detailed info")
        self.assertEqual((r["new_tab"], waits), ("https://forecast.weather.gov/MapClick.php?x=1", []))
        # a link in the same tab: no new tab
        page.window_opened = None
        with mock.patch.object(cdp, "touch_hit", lambda pg, hit, then=(): ("touch", [{"result": {"value": WEB_SCREEN}}])):
            self.assertNotIn("new_tab", cdp.tap(page, "Get Detailed info"))
        # the helper: the page in hand dropped, the new tab read as the screen
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([])
        old = _FakePage()
        old.visible_at = 1e9
        dm._web = old
        read_with = []
        dm._page_read = lambda hint=None, **kw: read_with.append(hint) or "<hierarchy rotation=\"0\" page=\"1\"/>"
        xml = dm._new_tab_read({"new_tab": "https://forecast.weather.gov/MapClick.php?x=1"})
        self.assertEqual((xml, read_with, dm._web, old.visible_at),
                         ('<hierarchy rotation="0" page="1"/>', ["https://forecast.weather.gov/MapClick.php?x=1"],
                          None, 0.0))
        # the review of Oct 7: the tab it left, still in front at the read,
        # is no read of the new tab: looked for once more
        old.target = "T-old"
        dm._web = old
        reads = iter([("T-old", "<hierarchy page=\"old\"/>"), ("T-new", "<hierarchy page=\"new\"/>")])

        def page_read(hint=None, **kw):
            target, xml = next(reads)
            dm._web = SimpleNamespace(target=target, visible_at=1e9)
            return xml
        dm._page_read = page_read
        self.assertEqual(dm._new_tab_read({"new_tab": "https://forecast.weather.gov/x"}), '<hierarchy page="new"/>')
        self.assertEqual(dm._web.target, "T-new")

    def test_a_page_scroll_says_how_far_the_page_moved(self):
        cdp = _cdp()
        page = _ScriptedPage(cdp, {"SCROLL_JS": {"moved": 0, "scroller": None, "held": True}, "READ_JS": WEB_SCREEN})
        r = cdp.scroll(page, "down")
        self.assertEqual((r["moved"], r["held"], r["screen"]["moved"], r["screen"]["held"]), (0, True, 0, True))
        self.assertIn('page="1" moved="0" held="1"', cdp.page_xml(r["screen"], 283, 2400))
        page = _ScriptedPage(cdp, {"SCROLL_JS": {"moved": 600, "scroller": "page"}, "READ_JS": WEB_SCREEN})
        r = cdp.scroll(page, "down", times=2)
        self.assertEqual((r["moved"], r["held"]), (1200, False))
        self.assertIn('page="1" moved="1200">', cdp.page_xml(r["screen"], 283, 2400))
        self.assertNotIn("moved=", cdp.page_xml(WEB_SCREEN, 283, 2400))  # a read with no scroll


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
        # a pulled-down shade or a lock screen has words: not blank
        clock = SHADE_NODE.replace('text=""', 'text="12:30"')
        self.assertFalse(mod.blank_screen(SHADE_XML.replace(SHADE_NODE, SHADE_NODE * 4 + clock)))
        # the system UI's bars alone with no word, however many: no app on
        # the screen (a cold start's window not in the tree yet, Oct 6)
        self.assertTrue(mod.blank_screen(SHADE_XML.replace(SHADE_NODE, SHADE_NODE * 5)))
        self.assertTrue(mod.blank_screen(BARS_XML))

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

    def test_a_page_with_no_words_leaves_the_screen_reader_alone(self):
        # Reddit, Oct 7: a page read with no words counted toward a
        # wordless spell, and the phone's UI server was killed and started
        # again (a 10s `state`); a page read doesn't go through that server
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([])
        bare = _cdp().page_xml(dict(WEB_SCREEN, title="", url="", rows=[]), 283, 2400)
        self.assertTrue(mod.mute_read(bare))
        dm._page_read = lambda **kw: bare
        dm._fix_wordless_read = mock.Mock(side_effect=AssertionError("the server must not be replaced"))
        dm._mute_since = mod._time.monotonic() - 30  # a long spell already
        self.assertEqual(dm._dump(fresh=True), bare)
        dm._fix_wordless_read.assert_not_called()
        # the session that took a second handshake is said once, not on every reuse
        lines = []
        mod.log = lambda *a: lines.append(" ".join(str(x) for x in a))
        page = SimpleNamespace(handshake_again=True)
        mod._log_handshake(page)
        mod._log_handshake(page)
        self.assertEqual(len(lines), 1)

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
        # a bare read wakes nothing (it may follow a POWER sent another
        # way); the read after a launch wakes the screen first
        self.assertEqual([m for m, _ in mod.act_calls({})],
                         ["waitForIdle", "dumpWindowHierarchy"])
        self.assertEqual([m for m, _ in mod.act_calls({"wake": True, "idle": 1500})],
                         ["wakeUp", "waitForIdle", "dumpWindowHierarchy"])
        self.assertEqual(mod.act_calls({"wake": True, "tap": [1, 2]})[:2], [("wakeUp", []), ("click", [1, 2])])
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

    def test_helper_reconnects_the_phone_before_restarting_its_server(self):
        # Oct 6: after hours idle the link through the tunnel had died
        # silently; the server was fine, adb had lost the phone, and the
        # helper's two restart tries ran three commands into the 60s watchdog
        mod = _u2mux()
        self.no_sleep(mod)
        self.assertTrue(mod.device_gone("ConnectError: device 127.0.0.1:15555 not online"))
        self.assertTrue(mod.device_gone("AdbError: device '127.0.0.1:15555' not found"))
        self.assertTrue(mod.device_gone("LinkDead: no answer from the phone's adb in 6s (stream open)"))
        self.assertFalse(mod.device_gone("RuntimeError: server can't read the screen"))
        # the words alone don't make it the phone: a transport's error does
        self.assertTrue(mod.phone_gone(ConnectionError("AdbError: device '127.0.0.1:15555' not found")))
        self.assertTrue(mod.phone_gone(mod.LinkDead("no answer from the phone's adb in 6s (stream open)")))
        self.assertFalse(mod.phone_gone(RuntimeError("uiautomator object not found")))
        self.assertFalse(mod.phone_gone(ConnectionError("HTTPError: HTTP request failed: 500")))
        back = _FakeServer([SAMPLE_XML])
        fake_u2 = SimpleNamespace(connect=lambda target: back)
        fake_adbutils = SimpleNamespace(adb=SimpleNamespace(
            device=lambda target: self.fail("no server restart: the phone was the problem")))
        calls = []
        mod.link_alive = lambda: calls.append("probe") or False
        mod.reconnect_device = lambda: calls.append("reconnect") or True
        mod.server_works = lambda d, words=False: True
        mod.apply_fast_config = lambda d: None
        with mock.patch.dict(sys.modules, {"uiautomator2": fake_u2, "adbutils": fake_adbutils}):
            d = mod.ensure_server()
        self.assertIs(d, back)
        self.assertEqual(calls, ["probe", "reconnect"])
        # the phone not coming back: said so at once, no restart tries
        # (each would hang on the dead link)
        calls.clear()
        mod.reconnect_device = lambda: calls.append("reconnect") or False
        with mock.patch.dict(sys.modules, {"uiautomator2": fake_u2, "adbutils": fake_adbutils}):
            with self.assertRaises(RuntimeError) as cm:
                mod.ensure_server()
        self.assertIn("gone from adb", str(cm.exception))
        self.assertEqual(calls, ["probe", "reconnect"])
        # the link alive at the probe, the phone gone as the server is
        # asked: reconnected and asked again, no restart
        calls.clear()
        mod.link_alive = lambda: calls.append("probe") or True
        mod.reconnect_device = lambda: calls.append("reconnect") or True
        answers = iter([ConnectionError("AdbError: device '127.0.0.1:15555' not found"), back])

        def connect(target):
            a = next(answers)
            if isinstance(a, Exception):
                raise a
            return a
        fake_u2 = SimpleNamespace(connect=connect)
        with mock.patch.dict(sys.modules, {"uiautomator2": fake_u2, "adbutils": fake_adbutils}):
            d = mod.ensure_server()
        self.assertIs(d, back)
        self.assertEqual(calls, ["probe", "reconnect"])

    def test_a_stream_open_that_gets_no_answer_is_the_link_dead(self):
        # Oct 6: a link through the tunnel died silently while idle; adb's
        # table still said "device", and a stream opened on it waited 75s
        mod = _u2mux()
        self.no_sleep(mod)
        import socket

        class Sock:
            def __init__(self):
                self.timeout = 6

            def settimeout(self, t):
                self.timeout = t

            def gettimeout(self):
                return self.timeout

            def close(self):
                pass

        class Transport:
            def __init__(self, answer):
                self.answer, self.sent, self.closed = answer, [], False
                self.conn = Sock()

            def send_command(self, cmd):
                self.sent.append(cmd)

            def check_okay(self):
                if isinstance(self.answer, Exception):
                    raise self.answer

            def close(self):
                self.closed = True
        opened = []
        state = {}
        dev = SimpleNamespace(serial="s", open_transport=lambda timeout=None: opened.append(timeout) or state["t"])
        core = mock.Mock(AdbHTTPConnection=lambda dev, port: SimpleNamespace(sock=None))
        state["t"] = t = Transport(socket.timeout("timed out"))
        with mock.patch.dict(sys.modules, {"uiautomator2": mock.Mock(), "uiautomator2.core": core}):
            with self.assertRaises(mod.LinkDead) as cm:
                mod.open_stream(dev, 9008)
        self.assertEqual((opened, t.sent, t.closed), ([6.0], ["tcp:9008"], True))
        self.assertTrue(mod.phone_gone(cm.exception))
        # the link up: the stream is the connection's socket, with no timeout left on it
        state["t"] = t = Transport(None)
        with mock.patch.dict(sys.modules, {"uiautomator2": mock.Mock(), "uiautomator2.core": core}):
            c = mod.open_stream(dev, 9008)
        self.assertIs(c.sock, t.conn)
        self.assertIsNone(t.conn.gettimeout())
        self.assertFalse(t.closed)
        # a phone adb has lost: adb's own error, as before
        state["t"] = t = Transport(RuntimeError("AdbError: device 's' not found"))
        with mock.patch.dict(sys.modules, {"uiautomator2": mock.Mock(), "uiautomator2.core": core}):
            with self.assertRaises(RuntimeError):
                mod.open_stream(dev, 9008)
        self.assertTrue(t.closed)
        # the keep-alive request passes a dead link on as it is (an
        # HTTPError would have uiautomator2 launch a server over it)
        ka = mod.KeepAliveHTTP()
        ka._open = lambda dev, port: (_ for _ in ()).throw(mod.LinkDead("no answer from the phone's adb in 6s (stream open)"))
        with mock.patch.dict(sys.modules, {"uiautomator2": mock.Mock(), "uiautomator2.core": core}):
            with self.assertRaises(mod.LinkDead):
                ka.request(dev, 9008, "GET", "/ping")

    def test_a_frozen_link_is_found_while_an_answer_is_awaited(self):
        # Oct 6, a stopped tunnel leg: a read on a reused stream waited its
        # whole 25s before the link was looked at (34s for one `state`)
        mod = _u2mux()
        self.no_sleep(mod)
        clock = [100.0]
        waits, probes, closed = [], [], []

        def select_never(r, w, x, t):
            waits.append(t)
            clock[0] += t
            return [], [], []
        conn = SimpleNamespace(sock=object(), close=lambda: closed.append("conn"))
        ka = mod.KeepAliveHTTP()

        def dead(dev, port, timeout=None):
            probes.append(timeout)
            raise mod.LinkDead("no answer from the phone's adb in 3s (stream open)")
        with mock.patch.object(mod._time, "monotonic", lambda: clock[0]), \
                mock.patch.object(mod.select, "select", select_never), \
                mock.patch.object(mod, "open_stream", dead):
            with self.assertRaises(mod.LinkDead) as cm:
                ka._wait_answer(conn, None, 9008, 25.0)
        self.assertEqual((waits, probes, closed), ([3.0], [3.0], ["conn"]))  # at 3s, not 25
        self.assertTrue(cm.exception.sent)  # the request had gone out: it may have acted
        # a slow server on a live link: checked, then waited for
        waits.clear()
        probes.clear()
        answers = iter([False, False, True])

        def select_late(r, w, x, t):
            waits.append(t)
            clock[0] += t
            return ([r[0]] if next(answers) else []), [], []

        def alive(dev, port, timeout=None):
            probes.append(timeout)
            return SimpleNamespace(close=lambda: closed.append("probe"))
        with mock.patch.object(mod._time, "monotonic", lambda: clock[0]), \
                mock.patch.object(mod.select, "select", select_late), \
                mock.patch.object(mod, "open_stream", alive):
            ka._wait_answer(conn, None, 9008, 25.0)
        self.assertEqual((waits, probes), ([3.0, 5.0, 5.0], [3.0, 3.0]))
        self.assertEqual(closed, ["conn", "probe", "probe"])
        # an answer at once: no check; a short timeout: no check past it,
        # and the timeout raised at its end (the read after the wait would
        # have waited a second timeout: a hung server's 25s read took 50s)
        waits.clear()
        probes.clear()
        with mock.patch.object(mod._time, "monotonic", lambda: clock[0]), \
                mock.patch.object(mod.select, "select", lambda r, w, x, t: (r, [], [])), \
                mock.patch.object(mod, "open_stream", alive):
            ka._wait_answer(conn, None, 9008, 25.0)
        with mock.patch.object(mod._time, "monotonic", lambda: clock[0]), \
                mock.patch.object(mod.select, "select", select_never), \
                mock.patch.object(mod, "open_stream", alive):
            with self.assertRaises(TimeoutError):
                ka._wait_answer(conn, None, 9008, 2.0)
        self.assertEqual((waits, probes), ([2.0], []))
        # a link check that fails another way: said, and the wait goes on
        # without checks to its end
        waits.clear()
        logged = []
        mod.log = lambda *a: logged.append(" ".join(str(x) for x in a))
        with mock.patch.object(mod._time, "monotonic", lambda: clock[0]), \
                mock.patch.object(mod.select, "select", select_never), \
                mock.patch.object(mod, "open_stream", mock.Mock(side_effect=ValueError("api changed"))):
            with self.assertRaises(TimeoutError):
                ka._wait_answer(conn, None, 9008, 10.0)
        self.assertEqual(waits, [3.0, 7.0])
        self.assertTrue(any("the link check failed" in line for line in logged), logged)
        mod.log = lambda *a: None
        # nothing to watch (not a socket): the read decides
        with mock.patch.object(mod.select, "select", mock.Mock(side_effect=TypeError("not a socket"))):
            ka._wait_answer(conn, None, 9008, 25.0)
        # in a request: a dead link ends it, and its stream is dropped
        core = mock.Mock(HTTPResponse=lambda content: SimpleNamespace(content=content), HTTPError=Exception)
        dev = SimpleNamespace(serial="s")
        sent = []
        stream = SimpleNamespace(via_adb=True, sock=SimpleNamespace(settimeout=lambda t: None),
                                 request=lambda *a, **k: sent.append(a[1]),
                                 getresponse=lambda: self.fail("no read on a dead link"),
                                 close=lambda: None)
        ka._conns[(dev.serial, 9008)] = [stream, mod._time.monotonic()]
        ka._wait_answer = mock.Mock(side_effect=mod.LinkDead("no answer"))
        with mock.patch.dict(sys.modules, {"uiautomator2": mock.Mock(core=core), "uiautomator2.core": core}):
            with self.assertRaises(mod.LinkDead):
                ka.request(dev, 9008, "POST", "/jsonrpc/0", data=[{"method": "dumpWindowHierarchy"}])
        self.assertEqual((sent, ka._conns), (["/jsonrpc/0"], {}))
        # a stream that isn't adb's (the direct route): not watched
        ka._wait_answer.reset_mock()
        direct = SimpleNamespace(sock=SimpleNamespace(settimeout=lambda t: None),
                                 request=lambda *a, **k: None, close=lambda: None,
                                 getresponse=lambda: SimpleNamespace(status=200, reason="OK", will_close=False,
                                                                     read=lambda: b"{}", getheader=lambda n: ""))
        ka._conns[(dev.serial, 9008)] = [direct, mod._time.monotonic()]
        with mock.patch.dict(sys.modules, {"uiautomator2": mock.Mock(core=core), "uiautomator2.core": core}):
            ka.request(dev, 9008, "GET", "/ping")
        ka._wait_answer.assert_not_called()

    def test_reconnect_device_reconnects_and_leaves_adb_s_server_alone(self):
        # Oct 6: adb kept the dead link as a device and said "already
        # connected" to a connect: disconnected first, then connected. adb's
        # server is the CLI's `ensure`'s to restart (with its checks of the
        # phone's port); the scrcpy helper's streams use it too
        mod = _u2mux()
        self.no_sleep(mod)
        events, runs = [], []
        connects = []
        fake_adbutils = SimpleNamespace(adb=SimpleNamespace(
            disconnect=lambda t: events.append("disconnect"),
            connect=lambda t, timeout=None: events.append("connect") or (connects.pop(0) if connects else "connected to " + t),
            wait_for=lambda serial, state="device", timeout=None: events.append("wait %s %g" % (state, timeout)),
            server_kill=lambda: self.fail("adb's server is the CLI's to restart")))

        def run():
            with mock.patch.dict(sys.modules, {"adbutils": fake_adbutils}), \
                    mock.patch.object(mod.subprocess, "run", lambda cmd, **kw: runs.append(cmd[-1])), \
                    mock.patch.object(mod.os.path, "exists", lambda p: True):
                return mod.reconnect_device()
        mod.link_alive = lambda: True
        self.assertTrue(run())
        self.assertEqual((events, runs), (["disconnect", "connect", "wait device 5"], ["start"]))
        # still no answer: False, and why
        events.clear()
        mod.link_alive = lambda: False
        self.assertFalse(run())
        self.assertEqual(events, ["disconnect", "connect", "wait device 5"])
        # a connect adb refused: no wait for a phone that isn't coming
        events.clear()
        connects.append("failed to connect to '127.0.0.1:15555': Connection refused")
        self.assertFalse(run())
        self.assertEqual(events, ["disconnect", "connect"])
        self.assertIn("Connection refused", mod.LINK_WHY[0])

    def test_link_alive_bounds_the_whole_shell(self):
        # the review of Oct 6: adbutils bounds only the reading of a
        # shell's output; the stream's open waited 600s on a link that
        # passes nothing (adb dropped it after 75s)
        mod = _u2mux()
        self.no_sleep(mod)
        import socket

        class Stuck(_FakeDev):
            def open_transport(self, timeout=None):
                opened.append(timeout)
                t = super().open_transport(timeout)

                def no_okay():
                    raise socket.timeout("timed out")
                t.check_okay = no_okay
                return t
        opened = []
        dev = Stuck()
        with self.assertRaises(mod.LinkDead):
            mod.bounded_shell(dev, "echo ok", 5.0)
        self.assertEqual((opened, dev.shells), ([5.0], []))
        fake_adbutils = SimpleNamespace(adb=SimpleNamespace(device=lambda target: dev))
        with mock.patch.dict(sys.modules, {"adbutils": fake_adbutils}):
            self.assertFalse(mod.link_alive())
            self.assertIn("no answer from the phone's adb in 5s (shell)", mod.LINK_WHY[0])
            # a phone that answers: True
            ok = _FakeDev()
            fake_adbutils.adb.device = lambda target: ok
            self.assertTrue(mod.link_alive())
            self.assertEqual(ok.shells, ["echo ok"])
            # a shell that says something else (an unauthorized phone): False
            odd = _FakeDev()
            odd.shell = lambda cmd, timeout=None: "error: device unauthorized"
            fake_adbutils.adb.device = lambda target: odd
            self.assertFalse(mod.link_alive())
            self.assertIn("unauthorized", mod.LINK_WHY[0])

    def test_an_act_on_a_dead_link_is_sent_again_only_when_it_never_went_out(self):
        # the review of Oct 6: an act whose stream couldn't be opened was
        # reported "failed after sending", though nothing went out
        mod = _u2mux()
        self.no_sleep(mod)
        dm = self._daemon(mod)
        dm.d = _FakeServer([])
        dm._last_xml, dm._last_xml_t = SAMPLE_XML, mod._time.monotonic()
        dm._batch = mock.Mock(side_effect=mod.LinkDead("no answer from the phone's adb in 6s (stream open)"))
        with self.assertRaises(mod.ActNotSent) as cm:
            dm._act_batch({"key": 4, "idle": 1200})
        self.assertTrue(str(cm.exception).startswith("act not sent: the UI server couldn't be reached"))
        dm._batch = mock.Mock(side_effect=mod.LinkDead("the link died with the request out", sent=True))
        with self.assertRaises(RuntimeError) as cm:
            dm._act_batch({"key": 4, "idle": 1200})
        self.assertTrue(str(cm.exception).startswith("act failed after sending"))
        # in the command loop: not sent and the phone gone, so the link is
        # remade and the act sent once more; sent, never again
        relinks = []
        dm._relink = lambda why: relinks.append(why)
        answers = iter([mod.ActNotSent("act not sent: the UI server couldn't be reached "
                                       "(LinkDead: no answer from the phone's adb in 6s (stream open))"), b"ok"])

        def act(arg):
            a = next(answers)
            if isinstance(a, Exception):
                raise a
            return a
        dm.cmd_act = act
        self.assertEqual(dm.handle("act {}"), b"ok")
        self.assertEqual(len(relinks), 1)
        answers = iter([RuntimeError("act failed after sending: the link died with the request out")])
        with self.assertRaises(RuntimeError):
            dm.handle("act {}")
        self.assertEqual(len(relinks), 1)
        # not sent for another reason (the server not listening right after
        # an update, "AdbError: closed"): no relink; the server asked (its
        # client relaunches a server that doesn't answer) and the act sent
        # once more
        answers = iter([mod.ActNotSent("act not sent: the UI server couldn't be reached (AdbError: closed)"), b"ok"])
        self.assertEqual(dm.handle("act {}"), b"ok")
        self.assertEqual(len(relinks), 1)

    def test_a_relink_that_failed_is_not_tried_again_at_once(self):
        mod = _u2mux()
        self.no_sleep(mod)
        dm = self._daemon(mod)
        tries = []
        mod.reconnect_device = lambda: tries.append(1) or False
        mod._KEEPALIVE = SimpleNamespace(close=lambda: None)
        with self.assertRaises(RuntimeError) as cm:
            dm._relink("AdbError: device not found")
        self.assertIn("did not come back", str(cm.exception))
        with self.assertRaises(RuntimeError) as cm:
            dm._relink("AdbError: device not found")
        self.assertIn("a reconnect 0s ago failed", str(cm.exception))
        self.assertEqual(tries, [1])
        # later, tried again
        dm._relink_failed_at -= mod.RELINK_RETRY_S + 1
        mod.reconnect_device = lambda: tries.append(1) or True
        dm._relink("AdbError: device not found")
        self.assertEqual((tries, dm._relink_failed_at), ([1, 1], None))

    def test_a_set_text_that_answers_false_is_not_typed(self):
        # the phone's setText answers false when the field didn't take the
        # text: it was reported "typed"
        mod = _u2mux()
        self.no_sleep(mod)
        dm = self._daemon(mod)
        dm.d = _FakeServer([])
        dm._last_xml, dm._last_xml_t = SAMPLE_XML, mod._time.monotonic()

        def batch(calls, timeout=45.0):
            return [False if m == "setText" else True for m, _ in calls[:-1]] + [SAMPLE_XML]
        dm._batch = batch
        with self.assertRaises(RuntimeError) as cm:
            dm._act_batch({"set_text": "hunter2", "idle": 1200})
        self.assertIn("no editable field took the text", str(cm.exception))

    def test_the_helper_relinks_the_phone_whatever_the_restart_cooldown(self):
        # Oct 6, 12:28: three seconds after `burner update` restarted the
        # helper, adb had lost the phone; the restart cooldown raised the
        # read without a word, and the CLI's adb reader sat 60s
        mod = _u2mux()
        self.no_sleep(mod)
        dm = self._daemon(mod)
        dm._last_restart = mod._time.monotonic()  # a restart a moment ago
        dm.d = _FakeServer([])
        answers = iter([ConnectionError("AdbError: device '127.0.0.1:15555' not found"), b"ok"])

        def echo(arg):
            a = next(answers)
            if isinstance(a, Exception):
                raise a
            return a
        dm.cmd_echo = echo
        events = []
        server = dm.d
        mod.reconnect_device = lambda: events.append("reconnect") or True
        mod.apply_fast_config = lambda d: events.append("config")
        mod._KEEPALIVE = SimpleNamespace(close=lambda: events.append("close"))
        fake_u2 = SimpleNamespace(connect=lambda target: events.append("connect"))
        with mock.patch.dict(sys.modules, {"uiautomator2": fake_u2}):
            self.assertEqual(dm.handle("echo"), b"ok")
        self.assertEqual(events, ["close", "reconnect"])  # the server's handle kept: no new one, no settings again
        self.assertIs(dm.d, server)
        # the phone not coming back: the command fails with that, no restart
        answers = iter([ConnectionError("AdbError: device '127.0.0.1:15555' not found")])
        mod.reconnect_device = lambda: False
        with mock.patch.dict(sys.modules, {"uiautomator2": fake_u2}):
            with self.assertRaises(RuntimeError) as cm:
                dm.handle("echo")
        self.assertIn("gone from adb", str(cm.exception))
        # a server that stopped answering within the cooldown: raised as
        # before, no relink (the link is fine)

        class Sick:
            @property
            def info(self):
                raise RuntimeError("HTTPError: HTTP request failed: 500")
        dm.d = Sick()
        answers = iter([RuntimeError("HTTPError: HTTP request failed: 500")])
        mod.reconnect_device = lambda: self.fail("no relink: the server is sick, not the link")
        with self.assertRaises(RuntimeError) as cm:
            dm.handle("echo")
        self.assertIn("500", str(cm.exception))

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
        self.assertEqual(adb_dev.shells, ["echo ok", "ps -A -o PID,ARGS", "kill -9 2843 2840 3200",
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
        self.assertEqual(adb_dev.shells, ["echo ok", "ps -A -o PID,ARGS", "kill -9 2843 2840 3200",
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
        self.assertEqual(adb_dev.shells, ["echo ok"] + ["ps -A -o PID,ARGS", "dumpsys power | grep -m1 mWakefulness=", "input keyevent 224"] * 2)

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


# --------------------------------- 23. pre-merge review fixes (the helper)

DIALOG_XML = SAMPLE_XML.replace("</hierarchy>", """  <node index="0" text="" class="android.widget.FrameLayout" package="com.example" content-desc="" clickable="false" enabled="true" bounds="[50,350][1030,900]">
    <node index="0" text="Delete this?" class="android.widget.TextView" package="com.example" content-desc="" clickable="false" enabled="true" bounds="[100,370][900,420]"/>
    <node index="1" text="Delete" class="android.widget.Button" package="com.example" content-desc="" clickable="true" enabled="true" bounds="[100,430][980,520]"/>
  </node>
</hierarchy>""")  # a dialog, a later window, its Delete button over the OK button's centre (250,450)


class HelperReviewFixTests(OfflineTestCase):
    def test_iter_nodes_knows_parents_and_classes(self):
        mod = _u2mux()
        nodes = list(mod.iter_nodes(SAMPLE_XML))
        self.assertEqual([n["parent"] for n in nodes], [None, 0, 0, 0])
        self.assertEqual(nodes[1]["cls"], "android.widget.textview")
        self.assertTrue(mod.descends(nodes, 2, {id(nodes[0])}))
        self.assertFalse(mod.descends(nodes, 2, {id(nodes[1])}))
        self.assertEqual(mod.screen_size(nodes), (1080, 2400))
        self.assertIsNone(mod.screen_size(nodes[1:]))  # nothing at an edge

    def test_a_tap_by_words_refuses_a_row_under_a_dialog(self):
        mod = _u2mux()
        # a dialog's button over the row (a later window: on top): refused,
        # as the CLI's planned tap refuses it
        with self.assertRaises(RuntimeError) as cm:
            mod.label_target(DIALOG_XML, "OK")
        self.assertEqual(str(cm.exception), "'OK' is under 'Delete'")
        # a dialog by its class, with no words of its own at the point
        sheet = SAMPLE_XML.replace("</hierarchy>", '  <node text="" class="com.google.android.material.bottomsheet.BottomSheetDialog" package="com.example" content-desc="" clickable="false" enabled="true" bounds="[0,300][1080,2400]"/>\n</hierarchy>')
        with self.assertRaises(RuntimeError) as cm:
            mod.label_target(sheet, "OK")
        self.assertIn("'OK' is under", str(cm.exception))
        # an unlabelled clickable scrim over most of the screen (behind a
        # modal): refused; a small unlabelled view drawn after: no cover
        scrim = SAMPLE_XML.replace("</hierarchy>", '  <node text="" class="android.view.View" package="com.example" content-desc="" clickable="true" enabled="true" bounds="[0,0][1080,2400]"/>\n</hierarchy>')
        with self.assertRaises(RuntimeError):
            mod.label_target(scrim, "OK")
        small = SAMPLE_XML.replace("</hierarchy>", '  <node text="" class="android.view.View" package="com.example" content-desc="" clickable="true" enabled="true" bounds="[200,400][300,500]"/>\n</hierarchy>')
        self.assertEqual(mod.label_target(small, "OK"), (250, 450))
        # the same words drawn again over the row (one control drawn twice,
        # centres apart): no cover
        twice = SAMPLE_XML.replace("</hierarchy>", '  <node text="OK" class="android.widget.TextView" package="com.example" content-desc="" clickable="false" enabled="true" bounds="[100,400][900,500]"/>\n</hierarchy>')
        self.assertEqual(mod.label_target(twice, "OK"), (250, 450))
        # the helper's tap by words sends nothing for a row under a dialog
        EmptyScreenTests.no_sleep(self, mod)
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([DIALOG_XML])
        dm._batch = mock.Mock(side_effect=AssertionError("nothing may be tapped under a dialog"))
        dm._last_xml, dm._last_xml_t = DIALOG_XML, mod._time.monotonic()
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"tap_label": "OK", "idle": 1200}))
        self.assertEqual(str(cm.exception), "act not sent: 'OK' is under 'Delete'")

    def test_a_dialog_over_a_link_in_a_webview_is_a_cover(self):
        mod = _u2mux()
        # Chrome's own prompt (a later window) over the Box Score link at (796,491)
        allow = WHOLE_XML.replace("</hierarchy>", '  <node text="" class="android.widget.FrameLayout" package="com.android.chrome" bounds="[60,380][1020,700]" clickable="false" enabled="true">\n    <node text="Allow" class="android.widget.Button" package="com.android.chrome" bounds="[100,440][900,540]" clickable="true" enabled="true"/>\n  </node>\n</hierarchy>')
        with self.assertRaises(RuntimeError) as cm:
            mod.label_target(allow, "Box Score")
        self.assertEqual(str(cm.exception), "'Box Score' is under 'Allow'")
        # a native row before the WebView that overlaps it is not on top
        # (the card case in test_helper_label_target), nor is the WebView's
        # own unlabelled parent drawn after a row
        self.assertEqual(mod.label_target(WHOLE_XML, "Box Score"), (796, 491))

    def test_a_control_s_own_parts_are_no_cover(self):
        mod = _u2mux()
        # a row described "Wi-Fi" whose centre falls on its own "Connected"
        # line: its parts, not covers
        row = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" package="com.example" content-desc="" clickable="false" enabled="true" bounds="[0,0][1080,2400]">
    <node text="" class="android.widget.LinearLayout" package="com.example" content-desc="Wi-Fi" clickable="true" enabled="true" bounds="[0,400][1080,600]">
      <node text="Wi-Fi" class="android.widget.TextView" package="com.example" content-desc="" clickable="false" enabled="true" bounds="[100,410][500,480]"/>
      <node text="Connected" class="android.widget.TextView" package="com.example" content-desc="" clickable="false" enabled="true" bounds="[100,480][500,590]"/>
    </node>
  </node>
</hierarchy>"""
        self.assertEqual(mod.label_target(row, "Wi-Fi"), (540, 500))

    def test_a_hidden_page_read_is_no_screen(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = WebPathTests._fake_cdp(self, mod, calls)
        fake.read = lambda page, cap=160: calls.append(("read",)) or dict(WEB_SCREEN, vis="hidden")
        dm = EmptyScreenTests._daemon(self, mod)
        dm._web = _FakePage()
        dm._web.visible_at = 1e9
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        self.assertIsNone(dm._page_xml(dict(WEB_SCREEN, vis="hidden")))
        self.assertEqual(dm._web.visible_at, 0.0)
        # a read with Chrome on the newest read: the page says hidden, so
        # the screen reader's read is the screen (HOME was pressed)
        dm._web.visible_at = 1e9
        dm.d = _FakeServer([SAMPLE_XML])
        self.assertEqual(dm._dump(fresh=True), SAMPLE_XML)
        self.assertEqual(calls, [("read",)])
        self.assertEqual(dm.d.calls, ["dumpWindowHierarchy"])
        # the screen after an action on the page, when it says hidden
        dm.d = _FakeServer([SAMPLE_XML])
        self.assertEqual(dm._after_page(dict(WEB_SCREEN, vis="hidden")), SAMPLE_XML)
        self.assertIn('text="Box Score"', dm._after_page(WEB_SCREEN))

    def test_the_act_that_leaves_chrome_does_not_ask_the_page(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = WebPathTests._fake_cdp(self, mod, calls)
        fake.read = lambda page, cap=160: self.fail("a page in the background must not be asked")
        dm = EmptyScreenTests._daemon(self, mod)
        dm._web = _FakePage()
        dm._web.visible_at = 1e9
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        dm.d = _FakeServer([])
        dm._batch = lambda calls_, timeout=45.0: [None] * (len(calls_) - 1) + [SAMPLE_XML]  # HOME: com.example in front
        self.assertEqual(dm.cmd_act(json.dumps({"key": 3, "idle": 1200})), SAMPLE_XML.encode())
        self.assertEqual(dm._last_xml, SAMPLE_XML)
        # with Chrome still in front on the read after the action, the page is asked
        fake.read = lambda page, cap=160: calls.append(("read",)) or WEB_SCREEN
        dm._batch = lambda calls_, timeout=45.0: [None] * (len(calls_) - 1) + [CHROME_XML]
        xml = dm.cmd_act(json.dumps({"key": 4, "idle": 1200})).decode()
        self.assertEqual(calls, [("read",)])
        self.assertIn('text="Box Score"', xml)

    def test_a_wait_moves_to_the_screen_reader_when_the_page_hides(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = WebPathTests._fake_cdp(self, mod, calls)
        fake.find_read = lambda page, label, top=0, exact=False: (
            calls.append(("find", label, exact)) or {"found": False}, dict(WEB_SCREEN, vis="hidden"))
        fake.visible = lambda page, timeout=1.5: page.visible_at > 0  # a probe: hidden once the proof ended
        fake.front_page = lambda dev, current=None, **kw: current  # the page in hand while it is visible
        dm = EmptyScreenTests._daemon(self, mod)
        page = dm._web = _FakePage()
        page.visible_at = 1e9
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        dm.d = _FakeServer([SAMPLE_XML])  # the app the tap opened, with its OK button
        out = json.loads(dm.cmd_wait_for(json.dumps({"text": "OK", "timeout": 5})))
        self.assertTrue(out["found"])
        self.assertEqual(out["text"], "OK")
        self.assertEqual(calls, [("find", "OK", False)])  # the page once; then the screen reader
        # one look at the screen (the page check's), which is the read
        self.assertEqual(dm.d.calls, ["dumpWindowHierarchy"])
        self.assertEqual(page.visible_at, 0.0)
        self.assertIsNone(dm._web)

    def test_wait_exact_matches_the_whole_label_only(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        dm = EmptyScreenTests._daemon(self, mod)
        openai = SAMPLE_XML.replace('text="Hello"', 'text="OpenAI"')
        dm._last_xml, dm._last_xml_t = openai, mod._time.monotonic()
        dm.d = SimpleNamespace(jsonrpc_call=lambda m, p, timeout=10: openai, info={"screenOn": True})
        with self.assertRaises(mod.U2NotFound):
            dm.cmd_wait_for(json.dumps({"text": "Uninstall || Open", "timeout": 0.3, "exact": True}))
        dm.d = _FakeServer([openai])
        out = json.loads(dm.cmd_wait_for(json.dumps({"text": "Uninstall || Open", "timeout": 5})))
        self.assertEqual(out["text"], "OpenAI")  # as a part of longer words, without exact
        self.assertEqual(mod.find_node(openai, "open", fuzzy=False), None)
        # the page is asked the same way
        calls = []
        fake = WebPathTests._fake_cdp(self, mod, calls)
        fake.find_read = lambda page, label, top=0, exact=False: (
            calls.append(("find", label, exact)) or {"found": True, "text": label, "desc": "", "bounds": "[0,0][1,1]",
                                                     "enabled": True, "inview": True, "count": 1}, WEB_SCREEN)
        dm._web = _FakePage()
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        dm.cmd_wait_for(json.dumps({"text": "Open", "timeout": 5, "exact": True}))
        self.assertEqual(calls, [("find", "Open", True)])


# --------------------------------- 24. pre-merge review fixes (the CLI)

class CliReviewFixTests(OfflineTestCase):
    def test_a_page_row_under_a_native_dialog_is_refused_by_the_plan(self):
        # Chrome's own prompt (a later window), its button over the whole
        # Box Score link (no free point of the link to move the tap to)
        allow = WHOLE_XML.replace("</hierarchy>", '  <node text="" class="android.widget.FrameLayout" package="com.android.chrome" bounds="[60,380][1020,700]" clickable="false" enabled="true">\n    <node text="Allow" class="android.widget.Button" package="com.android.chrome" bounds="[100,440][1060,560]" clickable="true" enabled="true"/>\n  </node>\n</hierarchy>')
        root = ET.fromstring(allow)
        pc._update_screen_from_dump(root)
        plan = pc.plan_tap(pc.walk(root), 1080, 2400, text="Box Score")
        self.assertEqual(plan["action"], "refused")
        self.assertEqual(plan["cover"]["text"], "Allow")
        # the page's own rows never count (the page says what is under a point)
        root = ET.fromstring(COVERED_XML)
        pc._update_screen_from_dump(root)
        plan = pc.plan_tap(pc.walk(root), 1080, 2400, text="Box Score")
        self.assertEqual((plan["action"], plan["xy"]), ("tap", (796, 491)))

    def test_wait_exact_reaches_the_helper(self):
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append((cmd, arg))
            if cmd == "wait_for":
                return json.dumps({"found": True, "text": "Open", "desc": "", "bounds": "[0,0][10,10]",
                                   "enabled": True, "waited_ms": 5, "polls": 1})
            return SAMPLE_XML
        self.allow("u2sock", side_effect=u2)
        self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_wait(self.parse(["wait", "Open", "--exact", "--quiet"]))
        self.assertEqual(rc, 0, err.getvalue())
        spec = json.loads([a for c, a in calls if c == "wait_for"][0])
        self.assertTrue(spec["exact"])
        spec = json.loads(pc.json.dumps({"x": 1}))  # json in scope
        with mock.patch.object(pc.time, "sleep"), self.cap():
            pc.cmd_wait(self.parse(["wait", "Open", "--quiet"]))
        self.assertFalse(json.loads([a for c, a in calls if c == "wait_for"][1])["exact"])

    def test_log_line_masks_typed_text_behind_options_and_inside_flows(self):
        line = pc.log_line(["--json", "type", "hunter2"], 10, 0)
        self.assertNotIn("hunter2", line)
        self.assertIn("burner --json type '…'", line)
        line = pc.log_line(["do", 'tap Password; type --field Password "hunter2"; press ENTER'], 10, 0)
        self.assertNotIn("hunter2", line)
        self.assertIn("type --field Password …", line)
        self.assertIn("press ENTER", line)
        line = pc.log_line(["do", "type --clear s3cret; type -q also"], 10, 0)
        self.assertNotIn("s3cret", line)
        self.assertNotIn("also", line)
        self.assertIn("type --clear …; type -q …", line)
        line = pc.log_line(["save", "login", 'type "my secret"'], 10, 0)
        self.assertNotIn("my secret", line)
        self.assertIn("login", line)
        # a flow with no typing is logged as it was
        self.assertIn("tap OK; press BACK", pc.log_line(["do", "tap OK; press BACK"], 10, 0))


# --------------------------------- 25. a window over the page is the screen

_BARS = """  <node index="1" text="" class="android.widget.FrameLayout" package="com.android.systemui" bounds="[0,0][1080,131]" clickable="false" enabled="true"/>
  <node index="2" text="" class="android.widget.FrameLayout" package="com.android.systemui" bounds="[0,2274][1080,2400]" clickable="false" enabled="true"/>
</hierarchy>"""
BARS_WINDOWS = """<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout" package="com.android.chrome" bounds="[0,0][1080,2400]" clickable="false" enabled="true"/>
""" + _BARS
SHADE_WINDOWS = BARS_WINDOWS.replace(_BARS, """  <node index="1" text="" class="android.widget.FrameLayout" package="com.android.systemui" bounds="[0,0][1080,2400]" clickable="false" enabled="true">
    <node index="0" text="" content-desc="Notification shade." class="android.widget.FrameLayout" package="com.android.systemui" bounds="[0,0][1080,2400]" clickable="false" enabled="true"/>
  </node>
""" + _BARS)
IME_WINDOWS = BARS_WINDOWS.replace(_BARS, """  <node index="1" text="" class="android.widget.FrameLayout" package="com.google.android.inputmethod.latin" bounds="[0,1350][1080,2274]" clickable="false" enabled="true"/>
""" + _BARS)
DIALOG_WINDOWS = BARS_WINDOWS.replace(_BARS, """  <node index="1" text="" class="android.widget.FrameLayout" package="com.google.android.permissioncontroller" bounds="[60,900][1020,1500]" clickable="false" enabled="true">
    <node index="0" text="Allow Chrome to access this device's location?" class="android.widget.TextView" package="com.google.android.permissioncontroller" bounds="[100,950][980,1050]" clickable="false" enabled="true"/>
  </node>
""" + _BARS)
VOLUME_WINDOWS = BARS_WINDOWS.replace(_BARS, """  <node index="1" text="" class="android.widget.FrameLayout" package="com.android.systemui" bounds="[900,600][1080,1400]" clickable="false" enabled="true"/>
""" + _BARS)
# the full read with the shade pulled down over the page: its rows are the screen reader's
SHADE_OVER_PAGE_XML = CHROME_XML.replace("</hierarchy>", """  <node index="1" text="" class="android.widget.FrameLayout" package="com.android.systemui" bounds="[0,0][1080,2400]" clickable="false" enabled="true">
    <node index="0" text="" class="android.widget.FrameLayout" package="com.android.systemui" bounds="[0,400][1080,600]" clickable="true" enabled="true">
      <node index="0" text="Muse" class="android.widget.TextView" package="com.android.systemui" bounds="[100,420][400,470]" clickable="false" enabled="true"/>
      <node index="1" text="Weekly money check" class="android.widget.TextView" package="com.android.systemui" bounds="[100,480][900,560]" clickable="false" enabled="true"/>
    </node>
  </node>
</hierarchy>""")


class WindowOverPageTests(OfflineTestCase):
    def test_window_over_the_page(self):
        mod = _u2mux()
        rect = mod.webview_rect(CHROME_XML)
        self.assertEqual(rect, (0, 283, 1080, 2400))
        self.assertIsNone(mod.webview_rect(SAMPLE_XML))
        chrome = "com.android.chrome"
        self.assertEqual(mod.window_over(SHADE_WINDOWS, rect, chrome), ("com.android.systemui", "Notification shade."))
        self.assertEqual(mod.window_over(DIALOG_WINDOWS, rect, chrome)[0], "com.google.android.permissioncontroller")
        self.assertIsNone(mod.window_over(BARS_WINDOWS, rect, chrome))  # the bars at the edges
        self.assertIsNone(mod.window_over(IME_WINDOWS, rect, chrome))  # the keyboard: the page knows
        self.assertIsNone(mod.window_over(VOLUME_WINDOWS, rect, chrome))  # too small
        self.assertIsNone(mod.window_over(NO_WINDOWS, rect, chrome))
        self.assertIsNone(mod.window_over("", rect, chrome))
        self.assertIsNone(mod.window_over("<hierarchy", rect, chrome))
        # the app in front is never over itself
        self.assertIsNone(mod.window_over(BARS_WINDOWS, (0, 0, 1080, 2400), chrome))

    def test_a_read_with_the_shade_over_the_page_is_the_screen_readers(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        WebPathTests._fake_cdp(self, mod, calls)
        dm = EmptyScreenTests._daemon(self, mod)
        dm._web = _FakePage()
        dm._web.visible_at = 1e9
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        dm.d = _FakeServer([SHADE_OVER_PAGE_XML], windows=SHADE_WINDOWS)
        xml = dm._dump(fresh=True)
        self.assertIn("Weekly money check", xml)  # the shade's rows, from the screen reader
        self.assertEqual(calls, [("read",)])  # the page was asked, with the look in its wait
        self.assertEqual((dm.d.looks, dm.d.calls), (1, ["dumpWindowHierarchy"]))
        self.assertIsNotNone(dm._web)  # the page is still there, under the shade
        self.assertEqual(dm._last_xml, xml)
        # the shade closed: the page is the screen again, and no full read
        dm.d = _FakeServer([], windows=BARS_WINDOWS)
        self.assertIn('text="Box Score"', dm._dump(fresh=True))
        self.assertEqual((dm.d.looks, dm.d.calls), (1, []))
        # a look that fails, or shows nothing readable, leaves the page's read standing
        dm.d = _FakeServer([], windows=None)
        self.assertIn('text="Box Score"', dm._dump(fresh=True))
        dm.d = _FakeServer([], windows="<hierarchy")
        self.assertIn('text="Box Score"', dm._dump(fresh=True))
        dm.d = SimpleNamespace(jsonrpc_call=mock.Mock(side_effect=OSError("stream closed")), info={"screenOn": True})
        self.assertIn('text="Box Score"', dm._dump(fresh=True))

    def test_an_app_over_the_page_is_the_newest_read_at_once(self):
        # Oct 7: `settings home` over a Wikipedia page: the launch's read
        # came before Settings was drawn and showed Chrome; the page's read
        # saw Settings over it and gave way, and the stale read stood, so
        # the next tap tried the page first (~4s)
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        WebPathTests._fake_cdp(self, mod, calls)
        dm = EmptyScreenTests._daemon(self, mod)
        dm._web = _FakePage()
        dm._web.visible_at = 1e9
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        settings = ('<hierarchy rotation="0"><node text="" package="com.android.settings" '
                    'class="android.widget.FrameLayout" bounds="[0,0][1080,2400]"><node text="Search settings" '
                    'package="com.android.settings" class="android.widget.TextView" bounds="[160,180][800,270]"/>'
                    '<node text="Network &amp; internet" package="com.android.settings" class="android.widget.TextView" '
                    'bounds="[160,600][800,680]"/></node></hierarchy>')
        dm.d = _FakeServer([settings], windows=settings)
        dm._batch = lambda calls, timeout=45.0: [True] * (len(calls) - 1) + [CHROME_XML]
        xml = dm._act_batch({"idle": 1000}).decode()
        self.assertIn("Search settings", xml)  # the screen now, not the read from before Settings came
        self.assertEqual(dm._last_xml, xml)  # the newest read: the next tap goes to Settings' rows
        self.assertEqual(dm.d.calls, ["dumpWindowHierarchy"])  # one read, after the window
        self.assertEqual(dm._web.visible_at, 0.0)  # the page's proof of being on screen ended
        self.assertIsNone(dm._page())  # Settings in front: no page
        # the CLI's `settings`: read again while another app still shows
        reads = [CHROME_XML, CHROME_XML, settings]

        def u2(cmd, arg="", timeout=30):
            return reads.pop(0) if len(reads) > 1 else reads[0]
        self.allow("u2sock", side_effect=u2)
        self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.allow("u2_invalidate")
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            self.assertEqual(pc.cmd_settings(self.parse(["settings", "home"])), 0)
        self.assertIn("screen: com.android.settings", out.getvalue())
        # a page of Settings shown by another package: the quick reads,
        # then the screen as it is, no long wait for Settings
        reads[:] = [SAMPLE_XML]
        installed = self.allow("app_installed", return_value=True)
        with mock.patch.object(pc.time, "sleep") as slept, self.cap() as (out, err):
            self.assertEqual(pc.cmd_settings(self.parse(["settings", "home"])), 0)
        self.assertIn("screen: com.example", out.getvalue())
        installed.assert_not_called()
        self.assertLessEqual(len(slept.call_args_list), pc.LAUNCH_REREADS + 3)

    def test_a_wait_on_a_page_moves_to_the_screen_reader_under_a_window(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = WebPathTests._fake_cdp(self, mod, calls)
        fake.find_read = lambda page, label, top=0, exact=False: (
            calls.append(("find", label)) or {"found": False}, WEB_SCREEN)
        dm = EmptyScreenTests._daemon(self, mod)
        dm._web = _FakePage()
        dm._web.visible_at = 1e9
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        dm.d = _FakeServer([SHADE_OVER_PAGE_XML, SHADE_OVER_PAGE_XML], windows=SHADE_WINDOWS)
        out = json.loads(dm.cmd_wait_for(json.dumps({"text": "Weekly money check", "timeout": 5})))
        self.assertTrue(out["found"])
        self.assertEqual([c for c in calls if c[0] == "find"], [("find", "Weekly money check")])  # the page once
        self.assertEqual(dm.d.calls, ["dumpWindowHierarchy"])  # then the screen reader, which had the words
        self.assertGreaterEqual(dm.d.looks, 2)  # a look with the find, one with the read


# --------------------------------- 26. a long wait never goes silent

class SlicedWaitTests(OfflineTestCase):
    """`burner wait` above WAIT_SLICE_S comes back every slice with a
    progress line, and the same wait continues when run again."""

    def _clock(self):
        clock = {"t": 1000.0}
        for name in ("monotonic", "time"):
            p = mock.patch.object(pc.time, name, lambda: clock["t"])
            p.start()
            self.addCleanup(p.stop)
        return clock

    def _waiting_file(self):
        path = os.path.join(tempfile.mkdtemp(), "waiting.json")
        p = mock.patch.object(pc, "WAITING_FILE", path)
        p.start()
        self.addCleanup(p.stop)
        return path

    def _polling_helper(self, clock, calls):
        """A helper that polls each chunk it is given (the clock moves by
        the chunk) and never finds the words."""
        def u2(cmd, arg="", timeout=30):
            if cmd == "wait_for":
                spec = json.loads(arg)
                calls.append(spec["timeout"])
                clock["t"] += spec["timeout"]
                return pc.U2_NOT_FOUND
            return SAMPLE_XML
        self.allow("u2sock", side_effect=u2)
        self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))

    def test_progress_hint(self):
        nodes = [_wnode(text="Vinted"), _wnode(text="Cancel"), _wnode(text="Installing…"),
                 _wnode(text="43%"), _wnode(text="Hello")]
        self.assertEqual(pc.progress_hint(nodes), "the screen shows: Installing…, 43%")
        self.assertEqual(pc.progress_hint([_wnode(text="Hello"), _wnode(desc="Search"), _wnode(text="OK")]),
                         "the screen shows: Hello, Search")
        self.assertEqual(pc.progress_hint([_wnode(text="Please wait...")]), "the screen shows: Please wait...")
        self.assertEqual(pc.progress_hint([]), "the screen shows no words")
        # a web page: its title, not its first menu words (unless something is under way)
        page = [_wnode(text="Menu", clickable=True), _wnode(text="ESPN", clickable=True),
                _wnode(desc="NFL on ESPN - Scores, Stats and Highlights", cls="android.webkit.WebView")]
        self.assertEqual(pc.progress_hint(page), "the screen shows: NFL on ESPN - Scores, Stats and Highlights")
        self.assertEqual(pc.progress_hint(page + [_wnode(text="Loading…")]), "the screen shows: Loading…")

    def test_a_long_wait_comes_back_every_slice_and_continues(self):
        clock, path, calls = self._clock(), self._waiting_file(), []
        self._polling_helper(clock, calls)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_wait(self.parse(["wait", "Open", "--timeout", "600", "--no-evidence"]))
        self.assertEqual(rc, pc.WAIT_STILL_RC)
        self.assertEqual(calls, [10, 10, 10])  # three chunks of a 30s slice
        self.assertIn('still waiting for "Open": 30s so far, 570s left; the screen shows: Hello, OK.', out.getvalue())
        self.assertIn("run the same command again", out.getvalue())
        self.assertEqual(err.getvalue().count('burner: still waiting for "Open"'), 2)  # between the chunks
        self.assertTrue(os.path.exists(path))
        # run again a little later (the assistant told the user): the same wait, continued
        clock["t"] += 20
        calls.clear()
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_wait(self.parse(["wait", "Open", "--timeout", "600", "--no-evidence"]))
        self.assertEqual(rc, pc.WAIT_STILL_RC)
        self.assertIn('"Open": 80s so far, 520s left', out.getvalue())
        # another label is another wait, from the start
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            pc.cmd_wait(self.parse(["wait", "Done", "--timeout", "600", "--no-evidence"]))
        self.assertIn('"Done": 30s so far, 570s left', out.getvalue())
        # found on a later slice: the clock is forgotten
        def found(cmd, arg="", timeout=30):
            if cmd == "wait_for":
                return json.dumps({"found": True, "text": "Open", "desc": "", "bounds": "[0,0][10,10]",
                                   "enabled": True})
            return SAMPLE_XML
        self.allow("u2sock", side_effect=found)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_wait(self.parse(["wait", "Done", "--timeout", "600", "--quiet"]))
        self.assertEqual((rc, os.path.exists(path)), (0, False))
        self.assertIn("found: Open", out.getvalue())

    def test_a_long_wait_gives_up_when_its_time_is_used_up(self):
        clock, path, calls = self._clock(), self._waiting_file(), []
        self._polling_helper(clock, calls)
        with mock.patch.object(pc.time, "sleep"), self.cap():
            pc.cmd_wait(self.parse(["wait", "Open", "--timeout", "50", "--no-evidence"]))
        self.assertEqual(calls, [10, 10, 10])
        calls.clear()
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_wait(self.parse(["wait", "Open", "--timeout", "50", "--no-evidence"]))
        self.assertEqual(rc, 1)
        self.assertEqual(calls, [10, 10])  # the 20s left, then the timeout
        self.assertIn('timeout waiting for "Open"', err.getvalue())
        self.assertFalse(os.path.exists(path))
        # --absent that runs out: the words are still showing
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_wait(self.parse(["wait", "Open", "--absent", "--no-evidence"]))
        self.assertEqual(rc, 1)
        self.assertIn('timeout: "Open" is still showing', err.getvalue())
        # a slice long forgotten (the assistant came back much later) starts afresh
        with mock.patch.object(pc.time, "sleep"), self.cap():
            pc.cmd_wait(self.parse(["wait", "Open", "--timeout", "600", "--no-evidence"]))
        clock["t"] += pc.WAIT_RESUME_S + 1
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            pc.cmd_wait(self.parse(["wait", "Open", "--timeout", "600", "--no-evidence"]))
        self.assertIn('"Open": 30s so far', out.getvalue())

    def test_a_short_wait_is_one_piece_as_before(self):
        clock, path, calls = self._clock(), self._waiting_file(), []
        self._polling_helper(clock, calls)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_wait(self.parse(["wait", "Open", "--no-evidence"]))  # --timeout 30
        self.assertEqual(rc, 1)
        self.assertEqual(calls, [10, 10, 10])
        self.assertIn('timeout waiting for "Open"', err.getvalue())
        self.assertNotIn("still waiting", out.getvalue())
        self.assertFalse(os.path.exists(path))

    def test_an_older_helper_answering_at_once_ends_the_wait_as_before(self):
        clock, path, calls = self._clock(), self._waiting_file(), []
        self.allow("u2sock", side_effect=lambda cmd, arg="", timeout=30: calls.append(cmd) or pc.U2_NOT_FOUND)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_wait(self.parse(["wait", "Open", "--timeout", "600", "--no-evidence"]))
        self.assertEqual((rc, calls), (1, ["wait_for"]))
        self.assertIn('timeout waiting for "Open"', err.getvalue())

    def test_the_json_form_of_still_waiting(self):
        clock, path, calls = self._clock(), self._waiting_file(), []
        self._polling_helper(clock, calls)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_wait(self.parse(["wait", "Open", "--timeout", "600", "--json", "--no-evidence"]))
        self.assertEqual(rc, pc.WAIT_STILL_RC)
        data = json.loads(out.getvalue())
        self.assertEqual((data["ok"], data["still"], data["waited"], data["left"]), (False, True, 30, 570))
        self.assertEqual(data["screen"], "the screen shows: Hello, OK")

    def test_the_legacy_path_slices_too(self):
        clock, path = self._clock(), self._waiting_file()
        self.allow("u2sock", return_value=None)  # no helper
        self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))

        def sleep(s):
            clock["t"] += s
        with mock.patch.object(pc.time, "sleep", sleep), self.cap() as (out, err):
            rc = pc.cmd_wait(self.parse(["wait", "Nope", "--timeout", "600", "--no-evidence"]))
        self.assertEqual(rc, pc.WAIT_STILL_RC)
        self.assertIn('still waiting for "Nope": 30s so far', out.getvalue())
        self.assertGreaterEqual(err.getvalue().count('burner: still waiting for "Nope"'), 2)

    def test_do_pauses_on_a_step_still_waiting(self):
        self.allow("u2_invalidate")
        with mock.patch.object(pc, "cmd_wait", return_value=pc.WAIT_STILL_RC), \
                mock.patch.object(pc, "cmd_press", return_value=0) as press, \
                self.cap() as (out, err):
            rc = pc.cmd_do(argparse.Namespace(flow='wait "Uninstall || Open" --exact --timeout 600; press BACK'))
        self.assertEqual(rc, pc.WAIT_STILL_RC)
        self.assertIn("do: paused at step 1 (still waiting). Tell the user, then go on with: "
                      "burner do 'wait \"Uninstall || Open\" --exact --timeout 600; press BACK'", out.getvalue())
        press.assert_not_called()
        self.assertEqual(err.getvalue(), "")


# --------------------------------- 27. the phone's questions reach the user

PERMISSION_XML = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" package="com.vinted" bounds="[0,0][1080,2400]" clickable="false" enabled="true">
    <node text="Welcome to Vinted" class="android.widget.TextView" package="com.vinted" bounds="[100,300][900,380]" clickable="false" enabled="true"/>
    <node text="Women" class="android.widget.TextView" package="com.vinted" bounds="[100,400][300,460]" clickable="true" enabled="true"/>
    <node text="Men" class="android.widget.TextView" package="com.vinted" bounds="[320,400][520,460]" clickable="true" enabled="true"/>
    <node text="Kids" class="android.widget.TextView" package="com.vinted" bounds="[540,400][740,460]" clickable="true" enabled="true"/>
    <node text="Home" class="android.widget.TextView" package="com.vinted" bounds="[760,400][960,460]" clickable="true" enabled="true"/>
    <node text="Search for items" class="android.widget.EditText" package="com.vinted" bounds="[100,500][980,580]" clickable="true" enabled="true"/>
    <node text="Nike Air Max 90" class="android.widget.TextView" package="com.vinted" bounds="[100,700][500,760]" clickable="true" enabled="true"/>
    <node text="Levi's 501" class="android.widget.TextView" package="com.vinted" bounds="[560,700][960,760]" clickable="true" enabled="true"/>
    <node text="Inbox" class="android.widget.TextView" package="com.vinted" bounds="[400,2200][600,2260]" clickable="true" enabled="true"/>
    <node text="Likes" class="android.widget.TextView" package="com.vinted" bounds="[100,2200][300,2260]" clickable="true" enabled="true"/>
  </node>
  <node text="" class="android.widget.FrameLayout" package="com.google.android.permissioncontroller" bounds="[0,0][1080,2400]" clickable="true" enabled="true">
    <node text="" class="android.widget.LinearLayout" package="com.google.android.permissioncontroller" bounds="[60,700][1020,1900]" clickable="false" enabled="true">
      <node text="Allow Vinted to access this device's location?" class="android.widget.TextView" package="com.google.android.permissioncontroller" bounds="[100,760][980,880]" clickable="false" enabled="true"/>
      <node text="" content-desc="Precise" class="android.widget.ImageView" package="com.google.android.permissioncontroller" bounds="[120,950][520,1250]" clickable="true" enabled="true"/>
      <node text="" content-desc="Approximate" class="android.widget.ImageView" package="com.google.android.permissioncontroller" bounds="[560,950][960,1250]" clickable="true" enabled="true"/>
      <node text="While using the app" class="android.widget.Button" package="com.google.android.permissioncontroller" bounds="[100,1350][980,1480]" clickable="true" enabled="true"/>
      <node text="Only this time" class="android.widget.Button" package="com.google.android.permissioncontroller" bounds="[100,1500][980,1630]" clickable="true" enabled="true"/>
      <node text="Don't allow" class="android.widget.Button" package="com.google.android.permissioncontroller" bounds="[100,1650][980,1780]" clickable="true" enabled="true"/>
    </node>
  </node>
</hierarchy>"""

NOTIFY_XML = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" package="com.vinted" bounds="[0,0][1080,2400]" clickable="false" enabled="true">
    <node text="Welcome to Vinted" class="android.widget.TextView" package="com.vinted" bounds="[100,300][900,380]" clickable="false" enabled="true"/>
  </node>
  <node text="" class="android.widget.FrameLayout" package="com.google.android.permissioncontroller" bounds="[0,0][1080,2400]" clickable="true" enabled="true">
    <node text="Allow Vinted to send you notifications?" class="android.widget.TextView" package="com.google.android.permissioncontroller" bounds="[100,1300][980,1400]" clickable="false" enabled="true"/>
    <node text="Allow" class="android.widget.Button" package="com.google.android.permissioncontroller" bounds="[100,1500][980,1630]" clickable="true" enabled="true"/>
    <node text="Don't allow" class="android.widget.Button" package="com.google.android.permissioncontroller" bounds="[100,1650][980,1780]" clickable="true" enabled="true"/>
  </node>
</hierarchy>"""

OPEN_WITH_XML = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" package="com.android.chrome" bounds="[0,0][1080,2400]" clickable="false" enabled="true">
    <node text="Store" class="android.widget.TextView" package="com.android.chrome" bounds="[100,300][900,380]" clickable="false" enabled="true"/>
  </node>
  <node text="" class="android.widget.FrameLayout" package="android" bounds="[0,1200][1080,2400]" clickable="false" enabled="true">
    <node text="Open with" class="android.widget.TextView" package="android" bounds="[100,1250][980,1330]" clickable="false" enabled="true"/>
    <node text="Play Store" class="android.widget.TextView" package="android" bounds="[100,1400][500,1600]" clickable="true" enabled="true"/>
    <node text="Aurora Store" class="android.widget.TextView" package="android" bounds="[560,1400][960,1600]" clickable="true" enabled="true"/>
    <node text="Just once" class="android.widget.Button" package="android" bounds="[100,2000][500,2130]" clickable="true" enabled="true"/>
    <node text="Always" class="android.widget.Button" package="android" bounds="[560,2000][960,2130]" clickable="true" enabled="true"/>
  </node>
</hierarchy>"""

PERMISSION_MANAGER_XML = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" package="com.google.android.permissioncontroller" bounds="[0,0][1080,2400]" clickable="false" enabled="true">
    <node text="Permission manager" class="android.widget.TextView" package="com.google.android.permissioncontroller" bounds="[100,150][900,230]" clickable="false" enabled="true"/>
    <node text="Location" class="android.widget.TextView" package="com.google.android.permissioncontroller" bounds="[100,400][900,500]" clickable="true" enabled="true"/>
    <node text="Camera" class="android.widget.TextView" package="com.google.android.permissioncontroller" bounds="[100,600][900,700]" clickable="true" enabled="true"/>
    <node text="Microphone" class="android.widget.TextView" package="com.google.android.permissioncontroller" bounds="[100,2200][900,2300]" clickable="true" enabled="true"/>
  </node>
</hierarchy>"""

VOLUME_XML = SAMPLE_XML.replace("</hierarchy>", """  <node text="" class="android.widget.FrameLayout" package="com.android.systemui" bounds="[900,600][1080,1400]" clickable="false" enabled="true">
    <node text="" content-desc="Media volume" class="android.widget.SeekBar" package="com.android.systemui" bounds="[920,650][1060,1300]" clickable="true" enabled="true"/>
    <node text="" content-desc="Settings" class="android.widget.ImageButton" package="com.android.systemui" bounds="[940,1310][1040,1390]" clickable="true" enabled="true"/>
  </node>
</hierarchy>""")


class PhoneAsksTests(OfflineTestCase):
    def _nodes(self, xml):
        root = ET.fromstring(xml)
        pc._update_screen_from_dump(root)
        return pc.walk(root)

    def test_system_prompt(self):
        asked = pc.system_prompt(self._nodes(PERMISSION_XML), 1080, 2400)
        self.assertEqual(asked["text"], "Allow Vinted to access this device's location?")
        self.assertEqual(asked["options"], ["Precise", "Approximate", "While using the app", "Only this time", "Don't allow"])
        asked = pc.system_prompt(self._nodes(NOTIFY_XML), 1080, 2400)
        self.assertEqual((asked["text"], asked["options"]), ("Allow Vinted to send you notifications?", ["Allow", "Don't allow"]))
        asked = pc.system_prompt(self._nodes(OPEN_WITH_XML), 1080, 2400)
        self.assertEqual((asked["text"], asked["options"]), ("Open with", ["Play Store", "Aurora Store", "Just once", "Always"]))
        # a system app's whole screen is no dialog; an app's own rows are the app's
        self.assertIsNone(pc.system_prompt(self._nodes(PERMISSION_MANAGER_XML), 1080, 2400))
        self.assertIsNone(pc.system_prompt(self._nodes(SAMPLE_XML), 1080, 2400))
        self.assertIsNone(pc.system_prompt(self._nodes(OVERLAY_XML), 1080, 2400))
        # the volume panel asks nothing
        self.assertIsNone(pc.system_prompt(self._nodes(VOLUME_XML), 1080, 2400))

    def test_the_screen_names_the_question_first(self):
        root = ET.fromstring(PERMISSION_XML)
        pc._update_screen_from_dump(root)
        with self.cap() as (out, err):
            pc.print_screen(root)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "screen: com.vinted")
        self.assertEqual(lines[1], "  asked: Allow Vinted to access this device's location? | options: Precise / Approximate / "
                                   "While using the app / Only this time / Don't allow | the user decides: ask, then burner tap \"their choice\"")
        self.assertTrue(any("Don't allow (click)" in l for l in lines[2:]))
        # --json state carries it
        self.allow("ui_dump", return_value=ET.fromstring(NOTIFY_XML))
        with self.cap() as (out, err):
            rc = pc.cmd_state(self.parse(["state", "--json"]))
        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["asked"]["options"], ["Allow", "Don't allow"])
        self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        with self.cap() as (out, err):
            pc.cmd_state(self.parse(["state", "--json"]))
        self.assertNotIn("asked", json.loads(out.getvalue()))

    def test_a_wait_ends_on_the_phone_s_question(self):
        clock = SlicedWaitTests._clock(self)
        SlicedWaitTests._waiting_file(self)
        calls = []

        def u2(cmd, arg="", timeout=30):
            if cmd == "wait_for":
                spec = json.loads(arg)
                calls.append(spec["timeout"])
                clock["t"] += spec["timeout"]
                return pc.U2_NOT_FOUND
            return PERMISSION_XML if arg == "cached" else SAMPLE_XML
        self.allow("u2sock", side_effect=u2)
        self.allow("ui_dump", return_value=ET.fromstring(PERMISSION_XML))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_wait(self.parse(["wait", "Likes", "--timeout", "600", "--no-evidence"]))
        self.assertEqual((rc, calls), (pc.WAIT_STILL_RC, [10]))  # one chunk, then the question
        self.assertIn("the phone asks: Allow Vinted to access this device's location? | options: Precise / Approximate / "
                      "While using the app / Only this time / Don't allow", out.getvalue())
        self.assertIn("tap their answer", out.getvalue())
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_wait(self.parse(["wait", "Likes", "--timeout", "600", "--json", "--no-evidence"]))
        self.assertEqual(json.loads(out.getvalue())["asked"]["options"][-1], "Don't allow")
        # the legacy path (no helper) sees it on its first read (words that
        # are on the app under the dialog still count as found, as the helper's do)
        self.allow("u2sock", return_value=None)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_wait(self.parse(["wait", "Messages", "--timeout", "600", "--no-evidence"]))
        self.assertEqual(rc, pc.WAIT_STILL_RC)
        self.assertIn("the phone asks:", out.getvalue())

    def test_a_chain_pauses_on_the_phone_s_question(self):
        looks = self.allow("u2sock", side_effect=lambda cmd, arg="", timeout=30: PERMISSION_XML)
        with mock.patch.object(pc, "cmd_start", return_value=0), \
                mock.patch.object(pc, "cmd_tap", return_value=0) as tap, \
                self.cap() as (out, err):
            rc = pc.cmd_do(argparse.Namespace(flow='start com.vinted; tap "Likes"; press BACK'))
        self.assertEqual(rc, pc.WAIT_STILL_RC)
        self.assertIn("do: paused after step 1: the phone asks: Allow Vinted to access this device's location? | options: "
                      "Precise / Approximate / While using the app / Only this time / Don't allow. Ask the user, tap their "
                      "answer, then go on with: burner do 'tap \"Likes\"; press BACK'", out.getvalue())
        tap.assert_not_called()
        self.assertEqual([c[0] for c in looks.call_args_list], [("dump", "cached")])
        # the last step's own screen says it; no pause after the last step
        with mock.patch.object(pc, "cmd_start", return_value=0), self.cap() as (out, err):
            rc = pc.cmd_do(argparse.Namespace(flow="start com.vinted"))
        self.assertEqual(rc, 0)

    def test_start_with_a_permission_prompt_in_front_is_the_app_up_and_asking(self):
        # a newly installed app asks for a permission on its first start:
        # the permission controller is in front, with the app under it
        self.allow("scrcpy_send", return_value=True)
        self.allow("adb_or_ensure")
        self.allow("u2_invalidate")
        self.allow("nav_record")
        self.allow("u2sock", side_effect=lambda cmd, arg="", timeout=30: NOTIFY_XML)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_start(self.parse(["start", "com.vinted"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("launched com.vinted", out.getvalue())
        self.assertIn("asked: Allow Vinted to send you notifications? | options: Allow / Don't allow", out.getvalue())
        self.assertNotIn("didn't come to the front", err.getvalue())

    def test_an_opaque_web_view_is_said(self):
        # the window's flags, asked only for an opaque screen: plain here
        adb = self.allow("adb_or_ensure", return_value=SimpleNamespace(
            returncode=0, stdout="  Window #11 Window{14b5236 u0 com.espn.score_center/x}:\n"
            "    fl=LAYOUT_IN_SCREEN LAYOUT_INSET_DECOR HARDWARE_ACCELERATED\n", stderr=""))
        # ESPN's login screen, Oct 5: a WebView with no rows under it, and
        # nothing else of the app readable (the status bar aside)
        opaque = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" package="com.espn.score_center" bounds="[0,0][1080,2400]" clickable="false" enabled="true">
    <node text="" class="android.widget.FrameLayout" package="com.espn.score_center" bounds="[0,0][1080,2337]" clickable="false" enabled="true">
      <node text="" content-desc="ONEID UI MOBILE" class="android.webkit.WebView" package="com.espn.score_center" bounds="[0,0][1080,2337]" clickable="false" enabled="true"/>
      <node text="" class="android.widget.ImageView" package="com.espn.score_center" bounds="[0,0][1080,2337]" clickable="false" enabled="true"/>
    </node>
  </node>
  <node text="" class="android.widget.FrameLayout" package="com.android.systemui" bounds="[0,0][1080,136]" clickable="false" enabled="true">
    <node text="1:44" class="android.widget.TextView" package="com.android.systemui" bounds="[47,39][143,97]" clickable="false" enabled="true"/>
  </node>
</hierarchy>"""
        root = ET.fromstring(opaque)
        pc._update_screen_from_dump(root)
        nodes = pc.walk(root)
        self.assertEqual(pc.opaque_webview(nodes, 1080, 2400)["name"], "ONEID UI MOBILE")
        with self.cap() as (out, err):
            pc.print_screen(root)
        self.assertIn('  opaque: a web view "ONEID UI MOBILE" fills the screen with nothing readable', out.getvalue())
        self.assertIn("burner shot --out .", out.getvalue())
        # a web view with rows under it, or beside readable rows of the app, is not opaque
        self.assertIsNone(pc.opaque_webview(pc.walk(ET.fromstring(CHROME_XML)), 1080, 2400))
        readable = opaque.replace('<node text="" class="android.widget.ImageView"',
                                  '<node text="Log in with Disney" class="android.widget.TextView"')
        self.assertIsNone(pc.opaque_webview(pc.walk(ET.fromstring(readable)), 1080, 2400))
        self.assertIsNone(pc.opaque_webview(pc.walk(ET.fromstring(SAMPLE_XML)), 1080, 2400))
        # --json state carries it, and a wait's progress line names it
        self.allow("ui_dump", return_value=ET.fromstring(opaque))
        with self.cap() as (out, err):
            pc.cmd_state(self.parse(["state", "--json"]))
        self.assertEqual(json.loads(out.getvalue())["opaque"]["name"], "ONEID UI MOBILE")
        self.assertFalse(json.loads(out.getvalue())["opaque"]["secure"])
        self.assertIn("web view with nothing readable", pc._wait_hint())
        self.assertEqual(adb.call_count, 3)  # once per screen said opaque, never otherwise
        self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        with self.cap() as (out, err):
            pc.cmd_state(self.parse(["state"]))
        self.assertEqual(adb.call_count, 3)

    def test_a_shielded_web_view_is_said_as_such(self):
        # ESPN's sign-in (Disney's OneID lightbox), Oct 5: FLAG_SECURE on the
        # window (a black picture) and its words kept from the screen reader
        dump = ("  Window #11 Window{14b5236 u0 com.espn.score_center/com.disney.id.android.lightbox.LightboxActivity}:\n"
                "    mAttrs={(0,0)(fillxfill) ty=BASE_APPLICATION fmt=TRANSLUCENT\n"
                "    fl=LAYOUT_IN_SCREEN SECURE LAYOUT_INSET_DECOR SPLIT_TOUCH HARDWARE_ACCELERATED\n"
                "  Window #12 Window{29cf67d u0 com.espn.score_center/com.espn.onboarding.EspnOnboardingActivity}:\n"
                "    fl=LAYOUT_IN_SCREEN LAYOUT_INSET_DECOR SPLIT_TOUCH HARDWARE_ACCELERATED\n")
        self.assertTrue(pc.window_is_secure(dump, "com.espn.score_center"))  # the first window listed: in front
        self.assertFalse(pc.window_is_secure("  Window #12 Window{29cf67d u0 com.espn.score_center/x}:" + chr(10)
                                             + "    fl=LAYOUT_IN_SCREEN" + chr(10), "com.espn.score_center"))
        self.assertIsNone(pc.window_is_secure(dump, "com.example"))
        self.assertIsNone(pc.window_is_secure("", "com.example"))
        self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=0, stdout=dump, stderr=""))
        opaque = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" package="com.espn.score_center" bounds="[0,0][1080,2400]" clickable="false" enabled="true">
    <node text="" content-desc="ONEID UI MOBILE" class="android.webkit.WebView" package="com.espn.score_center" bounds="[0,0][1080,2337]" clickable="false" enabled="true"/>
  </node>
</hierarchy>"""
        with self.cap() as (out, err):
            pc.print_screen(ET.fromstring(opaque))
        self.assertIn('  opaque: a web view "ONEID UI MOBILE" fills the screen with nothing readable, in a window '
                      'the app shields (a picture of it is black too): nothing on the phone can see or drive this part. '
                      'The user does it by hand on the phone, or the same on the site in Chrome may do', out.getvalue())
        self.allow("ui_dump", return_value=ET.fromstring(opaque))
        with self.cap() as (out, err):
            pc.cmd_state(self.parse(["state", "--json"]))
        self.assertTrue(json.loads(out.getvalue())["opaque"]["secure"])
        self.assertEqual(pc._wait_hint(), "the screen is a web view the app shields (nothing readable, and a picture of it is black)")

    def test_the_helper_hands_out_its_newest_read_without_a_round_trip(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([])  # never asked
        dm._last_xml, dm._last_xml_t = SAMPLE_XML, mod._time.monotonic()
        self.assertEqual(dm.cmd_dump("cached"), SAMPLE_XML.encode())
        dm._last_xml_t = mod._time.monotonic() - 11
        self.assertEqual(dm.cmd_dump("cached"), b"")  # too old to be the screen now
        dm._last_xml, dm._last_xml_t = "", mod._time.monotonic()
        self.assertEqual(dm.cmd_dump("cached"), b"")
        self.assertEqual(dm.d.calls, [])


# --------------------------------- 28. a tap by --xy in one round trip

class CoordinateTapTests(OfflineTestCase):
    def test_the_visible_tab_is_found_in_one_probe_s_time_and_a_busy_one_waited_for(self):
        cdp = _cdp()
        probes = []

        class Tab:
            """A tab's session: the current one (first) is busy loading and
            answers only when given long enough; the others are frozen."""
            def __init__(self, dev, target, probe_s=None, pipeline=True):
                probes.append((target, probe_s))
                self.target, self.visible_at, self.closed = target, 0.0, False
                if target == "A":
                    if probe_s is None:
                        raise TimeoutError("timed out")  # busy: no answer in a probe's time
                    self.visible_at = 1.0  # answered at last: visible
                elif target == "B":
                    raise TimeoutError("timed out")  # frozen in the background
                # C: answers hidden

            def close(self):
                self.closed = True
        with mock.patch.object(cdp, "Page", Tab), \
                mock.patch.object(cdp, "pages", lambda dev: [{"id": "A"}, {"id": "B"}, {"id": "C"}, {"id": "D"}]):
            page = cdp.front_page(None)
        self.assertEqual(page.target, "A")
        self.assertEqual(probes, [("A", None), ("B", None), ("C", None), ("A", cdp.LOAD_PROBE_S)])  # SCAN_TABS at once, then the current tab again
        # a hidden current tab with a visible one behind it: the visible one, and the hidden one closed
        probes.clear()

        class Tab2(Tab):
            def __init__(self, dev, target, probe_s=None, pipeline=True):
                probes.append((target, probe_s))
                self.target, self.visible_at, self.closed = target, (1.0 if target == "B" else 0.0), False
        with mock.patch.object(cdp, "Page", Tab2), \
                mock.patch.object(cdp, "pages", lambda dev: [{"id": "A"}, {"id": "B"}]):
            page = cdp.front_page(None)
        self.assertEqual((page.target, probes), ("B", [("A", None), ("B", None)]))
        # none visible and the current tab refused (Chrome shows a native screen): no page
        class Refused(Tab):
            def __init__(self, dev, target, probe_s=None, pipeline=True):
                raise ConnectionError("refused")
        with mock.patch.object(cdp, "Page", Refused), \
                mock.patch.object(cdp, "pages", lambda dev: [{"id": "A"}]):
            with self.assertRaises(RuntimeError):
                cdp.front_page(None)

    def test_chrome_s_own_site_prompt_is_a_question_for_the_user(self):
        # browserleaks.com/geo in Chrome, Oct 5: Chrome's own dialog, in its window
        chrome = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" package="com.android.chrome" bounds="[0,0][1080,2400]" clickable="false" enabled="true">
    <node text="" content-desc="Web View" class="android.webkit.WebView" package="com.android.chrome" bounds="[0,283][1080,2254]" clickable="false" enabled="true"/>
    <node text="browserleaks.com/geo" class="android.widget.EditText" package="com.android.chrome" bounds="[200,160][700,260]" clickable="true" enabled="true"/>
    <node text="" content-desc="browserleaks.com wants to use your device's location" class="android.widget.LinearLayout" package="com.android.chrome" bounds="[27,650][1053,1969]" clickable="false" enabled="true">
      <node text="browserleaks.com wants to use your device's location" class="android.widget.TextView" package="com.android.chrome" bounds="[195,713][990,828]" clickable="false" enabled="true"/>
      <node text="Precise" class="android.widget.TextView" package="com.android.chrome" bounds="[337,950][843,1008]" clickable="false" enabled="true"/>
      <node text="Approximate" class="android.widget.TextView" package="com.android.chrome" bounds="[337,1213][843,1271]" clickable="false" enabled="true"/>
      <node text="Allow while visiting the site" class="android.widget.Button" package="com.android.chrome" bounds="[90,1459][990,1608]" clickable="true" enabled="true"/>
      <node text="Allow this time" class="android.widget.Button" package="com.android.chrome" bounds="[90,1608][990,1757]" clickable="true" enabled="true"/>
      <node text="Never allow" class="android.widget.Button" package="com.android.chrome" bounds="[90,1757][990,1906]" clickable="true" enabled="true"/>
    </node>
  </node>
</hierarchy>"""
        root = ET.fromstring(chrome)
        pc._update_screen_from_dump(root)
        asked = pc.system_prompt(pc.walk(root), 1080, 2400)
        self.assertEqual(asked["text"], "browserleaks.com wants to use your device's location")
        self.assertEqual(asked["options"], ["Allow while visiting the site", "Allow this time", "Never allow"])
        with self.cap() as (out, err):
            pc.print_screen(root)
        self.assertIn("asked: browserleaks.com wants to use your device's location | options: Allow while visiting the site / Allow this time / Never allow", out.getvalue())
        # an app's own ask before the system's, in a dialog of its own
        own = SAMPLE_XML.replace("</hierarchy>", """  <node text="" class="android.widget.LinearLayout" package="com.example" bounds="[60,800][1020,1500]" clickable="false" enabled="true">
    <node text="Vinted would like to send you notifications" class="android.widget.TextView" package="com.example" bounds="[100,850][980,950]" clickable="false" enabled="true"/>
    <node text="Not now" class="android.widget.Button" package="com.example" bounds="[100,1300][500,1420]" clickable="true" enabled="true"/>
    <node text="Turn on" class="android.widget.Button" package="com.example" bounds="[560,1300][980,1420]" clickable="true" enabled="true"/>
  </node>
</hierarchy>""")
        asked = pc.system_prompt(pc.walk(ET.fromstring(own)), 1080, 2400)
        self.assertEqual((asked["text"], asked["options"]), ("Vinted would like to send you notifications", ["Not now", "Turn on"]))
        # a page's own rows that ask (a cookie banner's words under the WebView that fills the screen) are the page's
        page = _cdp().page_xml(dict(WEB_SCREEN, rows=[{"text": "This site wants to use cookies", "kind": "text", "l": 0, "t": 100, "w": 400, "h": 30},
                                                     {"text": "Allow", "kind": "button", "click": True, "l": 0, "t": 150, "w": 100, "h": 30}]), 283, 2400)
        self.assertIsNone(pc.system_prompt(pc.walk(ET.fromstring(page)), 1080, 2400))
        self.assertIsNone(pc.system_prompt(pc.walk(ET.fromstring(SAMPLE_XML)), 1080, 2400))

    def test_the_look_after_a_launch_knows_the_page_s_app(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = WebPathTests._fake_cdp(self, mod, calls)
        fake.LOAD_PROBE_S = 6.0
        fake.CHROME_PACKAGES = ("com.android.chrome",)
        fake.front_page = lambda dev, current=None, first_probe_s=None, **kw: _FakePage()
        dm = EmptyScreenTests._daemon(self, mod)
        launcher = SAMPLE_XML.replace("com.example", "com.google.android.apps.nexuslauncher")
        dm._last_xml, dm._last_xml_t = launcher, mod._time.monotonic()  # the read before: HOME
        # Chrome's own window fills the screen: not one over the page
        dm.d = _FakeServer([CHROME_XML], windows=BARS_WINDOWS)
        self.assertIn('text="Box Score"', dm.cmd_dump("page").decode())
        # the shade over Chrome right after the launch: still said
        dm.d = _FakeServer([CHROME_XML], windows=SHADE_WINDOWS)
        self.assertEqual(dm.cmd_dump("page"), b"")
        # a tab not yet visible right after the launch is asked again, then found
        answers = iter([RuntimeError("no visible page in Chrome's first 3 tabs"), _FakePage()])

        def late(dev, current=None, first_probe_s=None, **kw):
            a = next(answers)
            if isinstance(a, Exception):
                raise a
            return a
        fake.front_page = late
        dm.d = _FakeServer([CHROME_XML] * 2, windows=BARS_WINDOWS)  # a read of the screen per try
        self.assertIn('text="Box Score"', dm.cmd_dump("page").decode())

    def test_a_cold_open_asks_the_page_straight_after_the_launch(self):
        # Chrome not in front: the helper can't open the link in a page,
        # the link is launched by intent, and the page is asked at once
        self.allow("scrcpy_send", return_value=False)  # Chrome started through the scrcpy helper: not here
        page = _cdp().page_xml(WEB_SCREEN, 283, 2400)
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append((cmd, arg if cmd != "act" else json.loads(arg)))
            if cmd == "act":
                pc._u2_status = "err act not sent: not a page"
                return None
            return page if arg.split(" ")[0] == "page" else "100"
        self.allow("u2sock", side_effect=u2)
        adb = self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.allow("u2_invalidate")
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_open(self.parse(["open", "https://www.espn.com/nfl/"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(adb.call_count, 2)  # Chrome brought up first (not here: no Chrome), then the intent
        self.assertEqual([c[0] for c in calls], ["act", "dump"])
        self.assertEqual((calls[1][0], calls[1][1].split()[0]), ("dump", "page"))
        self.assertIn("Box Score (click) (796,491)", out.getvalue())
        # the page can't be reached (a Chrome still starting, a native screen): the launch read, as before
        calls.clear()

        def u2_none(cmd, arg="", timeout=30):
            calls.append((cmd, arg if cmd != "act" else json.loads(arg)))
            if cmd == "act":
                if "open" in json.loads(arg):
                    pc._u2_status = "err act not sent: not a page"
                    return None
                return SAMPLE_XML.replace("com.example", "com.android.chrome")  # the launch read
            return "" if arg.split(" ")[0] == "page" else "100"
        self.allow("u2sock", side_effect=u2_none)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_open(self.parse(["open", "https://www.espn.com/nfl/"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual([c[0] for c in calls][:3], ["act", "dump", "act"])
        self.assertIn("screen: com.android.chrome", out.getvalue())

    def test_the_helper_contacts_the_page_after_a_launch_without_a_native_read(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = WebPathTests._fake_cdp(self, mod, calls)
        fake.LOAD_PROBE_S = 6.0
        fake.front_page = lambda dev, current=None, first_probe_s=None, **kw: (
            calls.append(("front_page", kw.get("quick"), kw.get("hint"))) or _FakePage())
        dm = EmptyScreenTests._daemon(self, mod)
        mod.log = lambda *a: None
        # one read of the screen first: Chrome's window is in front (asking
        # the tabs of a Chrome still coming up kept it from coming up)
        launcher = SAMPLE_XML.replace("com.example", "com.android.launcher3")
        dm.d = _FakeServer([CHROME_XML])
        dm._last_xml, dm._last_xml_t = SAMPLE_XML, mod._time.monotonic()  # the newest read: not Chrome
        dm._web_retry_at = mod._time.monotonic() + 100  # a cooldown that a launch overrides
        xml = dm.cmd_dump("page https://www.espn.com/nfl/").decode()
        self.assertIn('text="Box Score"', xml)
        # quick: short bounds, no sweep; the link's rows that say "Loading" waited for
        self.assertEqual(calls, [("front_page", True, "https://www.espn.com/nfl/"), ("read",), ("read_loaded",)])
        self.assertEqual(dm.d.calls, ["dumpWindowHierarchy"])
        # a Chrome still starting: the launcher in front twice (no tab asked),
        # then Chrome refusing once, then the page; Chrome's bar is the hint
        calls.clear()
        bar = CHROME_XML.replace('<node text="" class="android.webkit.WebView"',
                                 '<node text="espn.com/nfl" resource-id="com.android.chrome:id/url_bar" class="android.widget.EditText"'
                                 ' package="com.android.chrome" bounds="[200,160][700,260]" clickable="true" enabled="true"/>\n'
                                 '    <node text="" class="android.webkit.WebView"')
        dm.d = _FakeServer([launcher, launcher, bar, bar])
        answers = iter([OSError("refused"), _FakePage()])

        def starting(dev, current=None, first_probe_s=None, **kw):
            a = next(answers)
            calls.append(("front_page", kw.get("quick"), kw.get("hint")))
            if isinstance(a, Exception):
                raise a
            return a
        fake.front_page = starting
        self.assertIn('text="Box Score"', dm.cmd_dump("page https://www.espn.com/nfl/").decode())
        self.assertEqual([c for c in calls if c[0] == "front_page"], [("front_page", True, "espn.com/nfl")] * 2)
        self.assertEqual(len(dm.d.calls), 4)  # a read of the screen per try
        # a native screen of Chrome's: nothing, after the tries
        dm.d = _FakeServer([CHROME_XML] * dm.LAUNCH_CONTACT_TRIES)
        fake.front_page = mock.Mock(side_effect=RuntimeError("no visible page in Chrome's first 3 tabs"))
        self.assertEqual(dm.cmd_dump("page"), b"")
        self.assertEqual(fake.front_page.call_count, dm.LAUNCH_CONTACT_TRIES)

    def test_a_launch_with_the_screen_off_wakes_it_to_look_for_the_page(self):
        # Oct 6: a Wikipedia open took 66s with the screen off: "nothing
        # readable" and pages "hidden" on every try, the screen woken only after
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = WebPathTests._fake_cdp(self, mod, calls)
        fake.LOAD_PROBE_S = 6.0
        fake.front_page = lambda dev, current=None, first_probe_s=None, **kw: (
            calls.append(("front_page", kw.get("quick"), kw.get("hint"))) or _FakePage())
        dm = EmptyScreenTests._daemon(self, mod)
        mod.log = lambda *a: None
        link = "https://en.m.wikipedia.org/wiki/Main_Page"
        woke = []
        dm._wake = lambda: woke.append(1)
        dm.d = _FakeServer([BARS_XML, CHROME_XML], screen_on=False)
        dm._last_xml, dm._last_xml_t = SAMPLE_XML, mod._time.monotonic()  # the newest read: not Chrome
        self.assertIn('text="Box Score"', dm.cmd_dump("page " + link).decode())
        self.assertEqual(woke, [1])
        self.assertEqual([c for c in calls if c[0] == "front_page"], [("front_page", True, link)])
        # the screen on and no app in the reads: the tabs aren't asked (a
        # Chrome still coming up was kept from coming up by them, Oct 5)
        calls.clear()
        woke.clear()
        dm.d = _FakeServer([BARS_XML] * dm.LAUNCH_CONTACT_TRIES, screen_on=True)
        self.assertEqual(dm.cmd_dump("page " + link), b"")
        self.assertEqual(([c for c in calls if c[0] == "front_page"], woke), ([], []))

    def test_an_old_read_of_chrome_is_checked_before_its_tabs_are_scanned(self):
        # no page in hand, and the read that says Chrome is in front is old:
        # the screen is looked at first (woken when off); another app in
        # front then means no scan of Chrome's tabs ("hidden" after 4s, Oct 6)
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = WebPathTests._fake_cdp(self, mod, calls)
        fake.front_page = mock.Mock(side_effect=AssertionError("no scan of Chrome's tabs"))
        dm = EmptyScreenTests._daemon(self, mod)
        mod.log = lambda *a: None
        dm._web = None
        woke = []
        dm._wake = lambda: woke.append(1)
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic() - 60
        dm.d = _FakeServer([BARS_XML, SAMPLE_XML], screen_on=False)
        self.assertIsNone(dm._page())
        self.assertEqual((woke, dm.d.calls), ([1], ["dumpWindowHierarchy"] * 2))
        self.assertEqual(dm._last_xml, SAMPLE_XML)  # the look is the newest read
        # Chrome still in front: the tabs are asked as before
        fake.front_page = mock.Mock(return_value=_FakePage())
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic() - 60
        dm.d = _FakeServer([CHROME_XML], screen_on=True)
        self.assertIsNotNone(dm._page())
        fake.front_page.assert_called_once()
        # a young read: no look first
        dm._web = None
        fake.front_page.reset_mock()
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        dm.d = _FakeServer([], screen_on=True)
        dm._page()
        self.assertEqual(dm.d.calls, [])
        fake.front_page.assert_called_once()

    def test_the_current_tab_gets_the_long_probe_from_the_start_after_a_launch(self):
        cdp = _cdp()
        probes = []

        class Tab:
            def __init__(self, dev, target, probe_s=None, pipeline=True):
                probes.append((target, probe_s))
                self.target, self.visible_at = target, (1.0 if (target == "A" and probe_s) else 0.0)
                if target != "A":
                    raise TimeoutError("timed out")

            def close(self):
                pass
        with mock.patch.object(cdp, "Page", Tab), \
                mock.patch.object(cdp, "pages", lambda dev: [{"id": "A"}, {"id": "B"}, {"id": "C"}]):
            page = cdp.front_page(None, first_probe_s=6.0)
        self.assertEqual(page.target, "A")
        self.assertEqual(probes, [("A", 6.0), ("B", None), ("C", None)])  # no second round

    def test_touch_at_maps_the_screen_point_into_the_page(self):
        cdp = _cdp()

        class Page(_ScriptedPage):
            def call_many(self, cmds, timeout=10.0, raise_errors=True):
                self.sent = cmds
                return super().call_many(cmds, timeout, raise_errors)
        page = Page(cdp, {"READ_JS": WEB_SCREEN})
        r = cdp.touch_at(page, 553, 463, WEB_SCREEN, 283)  # the Box Score row's corner (dpr 2, no zoom)
        self.assertEqual((r["screen"], r["how"], r["ready"]), (WEB_SCREEN, "touch", "complete"))
        self.assertEqual(page.calls, [("many", ["Input.dispatchTouchEvent", "Input.dispatchTouchEvent", "READ_JS"])])
        self.assertEqual(page.sent[0][1]["touchPoints"], [{"x": 276.5, "y": 90.0}])
        # a zoomed-out page (no viewport meta): the scale and the corner count
        zoomed = dict(WEB_SCREEN, vs=0.5, vx=0, vy=0)
        page = Page(cdp, {"READ_JS": zoomed})
        cdp.touch_at(page, 553, 463, zoomed, 283)
        self.assertEqual(page.sent[0][1]["touchPoints"], [{"x": 553.0, "y": 180.0}])

    def test_the_helper_hands_a_point_on_the_page_to_the_page(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = WebPathTests._fake_cdp(self, mod, calls)
        fake.touch_at = lambda page, x, y, screen, top, idle_ms=1200: (
            calls.append(("touch_at", x, y, top, screen is page.last_read))
            or {"screen": WEB_SCREEN, "ready": "complete", "how": "touch"})
        fake.front_page = lambda dev, current=None, **kw: current  # the page in hand, as the real one keeps it
        dm = EmptyScreenTests._daemon(self, mod)
        dm._web = _FakePage()
        dm._web.visible_at = 1e9
        dm._web.last_read = WEB_SCREEN
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        dm.d = _FakeServer([])  # the screen reader isn't asked
        dm._batch = mock.Mock(side_effect=AssertionError("no click through the screen reader"))
        xml = dm.cmd_act(json.dumps({"tap": [796, 491], "idle": 1200})).decode()
        self.assertEqual(calls, [("touch_at", 796, 491, 283, True)])
        self.assertIn('text="Box Score"', xml)
        self.assertEqual(dm.d.looks, 1)  # the windows, looked at in the touch's wait
        # a quiet one: no screen back, and the read kept for the next step
        calls.clear()
        dm._last_xml = CHROME_XML.replace("Box Score", "Scores")  # the page read before, without the row
        self.assertEqual(dm.cmd_act(json.dumps({"tap": [796, 491], "quiet": True, "idle": 1200})), b"ok")
        self.assertIn('text="Box Score"', dm._last_xml)
        # a point outside the WebView (the address bar): the screen reader's click, as before
        dm._last_xml = CHROME_XML
        sent = []
        dm._batch = lambda c, timeout=45.0: sent.append(c) or [None] * (len(c) - 1) + [CHROME_XML]
        calls.clear()
        dm.cmd_act(json.dumps({"tap": [540, 100], "idle": 1200}))
        self.assertEqual([m for m, _ in sent[0]][:2], ["wakeUp", "click"])
        self.assertEqual(sent[0][1][1], [540, 100])
        self.assertEqual([c for c in calls if c[0] == "touch_at"], [])
        # a window over the page since the read the point came from: said,
        # and the screen reader's read is the newest
        calls.clear()
        dm.d = _FakeServer([SHADE_OVER_PAGE_XML], windows=SHADE_WINDOWS)
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"tap": [796, 491], "idle": 1200}))
        self.assertIn("act failed after sending: a window over the page", str(cm.exception))
        self.assertEqual(dm._last_xml, SHADE_OVER_PAGE_XML)

    def test_a_refused_label_tap_plans_from_the_helper_s_read_and_lists_without_a_picture(self):
        # the helper read the screen, found "OK" twice and refused: the CLI
        # plans on that read (no second read) and lists the candidates
        self.allow("wake_async", return_value=mock.Mock())
        self.allow("u2_invalidate")
        self.allow("ui_dump", side_effect=AssertionError("the helper's read is the screen: no read of its own"))
        shot = self.allow("_capture_evidence", side_effect=AssertionError("the candidates are the evidence"))
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append((cmd, arg))
            if cmd == "act":
                pc._u2_status = "err act not sent: 2 rows read 'OK'"
                return None
            return AMBI_XML if arg == "cached" else "100"
        self.allow("u2sock", side_effect=u2)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_tap(self.parse(["tap", "OK"]))
        self.assertEqual(rc, 1)
        self.assertIn("ambiguous tap", err.getvalue())
        self.assertIn("--index", err.getvalue())
        self.assertEqual([c[0] for c in calls], ["act", "dump"])
        shot.assert_not_called()

    def test_a_page_s_own_count_of_rows_is_said_with_no_look_at_the_screen_reader(self):
        # bbc.com, Oct 7: the page had the headline twice, both out of view;
        # the CLI's look at the screen reader's rows found none and said
        # "no match", and the agent scrolled to find them
        self.allow("wake_async", return_value=mock.Mock())
        self.allow("u2_invalidate")
        self.allow("ui_dump", side_effect=AssertionError("the page's count is the answer: no read"))
        self.allow("_capture_evidence", side_effect=AssertionError("the count is the evidence"))
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append(cmd)
            pc._u2_status = "err act not sent: 2 rows on the page read 'Edit' (a, a)"
            return None
        self.allow("u2sock", side_effect=u2)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_tap(self.parse(["tap", "Edit"]))
        self.assertEqual((rc, calls), (1, ["act"]))
        self.assertIn("ambiguous tap: 2 rows on the page read 'Edit' (a, a); use --index N", err.getvalue())

    def test_a_tap_by_xy_plans_from_the_helper_s_newest_read(self):
        self.allow("wake_async", return_value=mock.Mock())
        self.allow("u2_invalidate")
        self.allow("nav_record_action")
        self.allow("ui_dump", side_effect=AssertionError("no read of its own: the helper's newest read is the screen"))
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append((cmd, json.loads(arg) if cmd == "act" else arg))
            if cmd == "dump" and arg == "cached":
                return TAP_XML
            return SAMPLE_XML if cmd == "act" else "100"
        self.allow("u2sock", side_effect=u2)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_tap(self.parse(["tap", "--xy", "0.5,0.5"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(calls[0], ("dump", "cached"))
        self.assertEqual([c for c in calls if c[0] == "act"], [("act", {"tap": [540, 1200], "idle": pc.IDLE_ACT_MS})])
        self.assertIn("tapped --xy 0.5,0.5", out.getvalue())
        self.assertIn("screen: com.example", out.getvalue())
        # no young read at the helper: a read of its own, as before
        self.allow("ui_dump", return_value=ET.fromstring(TAP_XML))
        calls.clear()
        self.allow("u2sock", side_effect=lambda cmd, arg="", timeout=30:
                   calls.append((cmd, arg)) or ("" if arg == "cached" else SAMPLE_XML if cmd == "act" else "100"))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_tap(self.parse(["tap", "--xy", "0.5,0.5"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(calls[0], ("dump", "cached"))


class AirbnbRoundTests(OfflineTestCase):
    """The Airbnb cabin task through Muse, Oct 5: a card's title under its
    link, the page's tab listed fourth, a cold start judged too soon, two
    re-reads after every date tapped."""

    def test_same_address_matches_a_link_and_chrome_s_bar(self):
        cdp = _cdp()
        link = "https://www.airbnb.com/s/Stillwater/homes?checkin=2026-10-09"
        self.assertTrue(cdp.same_address(link, "airbnb.com/s/Stillwater/homes?checkin=2026-10-09…"))  # the bar's cut
        self.assertTrue(cdp.same_address(link, "https://airbnb.com/s/Stillwater/homes/"))
        self.assertTrue(cdp.same_address("https://www.espn.com/nfl/", "espn.com"))  # the bar shows the site alone
        self.assertFalse(cdp.same_address("https://www.espn.com/nfl/", "https://www.airbnb.com/"))
        self.assertFalse(cdp.same_address("", "airbnb.com"))
        self.assertFalse(cdp.same_address("https://a.io/", "a.io"))  # too short to say

    def test_front_page_probes_the_hinted_tab_with_the_first_ones(self):
        cdp = _cdp()
        probes = []

        class Tab:
            def __init__(self, dev, target, probe_s=None, pipeline=True):
                probes.append((target, probe_s))
                # D is the one on screen (a link launched Chrome into it); the rest answer hidden
                self.target, self.visible_at = target, (1.0 if target == "D" else 0.0)

            def close(self):
                pass
        tabs = [{"id": "A", "url": "https://www.espn.com/"}, {"id": "B", "url": "https://x.com/"},
                {"id": "C", "url": "https://y.com/"}, {"id": "D", "url": "https://www.airbnb.com/s/Woodbury/homes?x=1"},
                {"id": "E", "url": "https://z.com/"}]
        with mock.patch.object(cdp, "Page", Tab), mock.patch.object(cdp, "pages", lambda dev: tabs):
            page = cdp.front_page(None, hint="https://www.airbnb.com/s/Woodbury/homes?x=1")
        self.assertEqual(page.target, "D")
        self.assertEqual(probes, [("A", None), ("B", None), ("C", None), ("D", None)])  # one round
        # what Chrome's bar shows (no scheme, the path cut) hints the same tab
        probes.clear()
        with mock.patch.object(cdp, "Page", Tab), mock.patch.object(cdp, "pages", lambda dev: tabs):
            page = cdp.front_page(None, hint="airbnb.com/s/Woodbury/ho…")
        self.assertEqual((page.target, len(probes)), ("D", 4))

    def test_dead_tabs_at_the_link_s_address_are_closed_once_the_page_is_found(self):
        # six dead tabs at one address answered "refused" first on every
        # cold open (Oct 5): leftovers of earlier opens, closed through DevTools
        cdp = _cdp()
        closed = []

        class Tab:
            def __init__(self, dev, target, probe_s=None, pipeline=True):
                self.target, self.visible_at = target, (1.0 if target == "D" else 0.0)
                if target in ("B", "C"):
                    raise ConnectionError("")

            def close(self):
                pass
        tabs = [{"id": "A", "url": "https://www.airbnb.com/s/homes"}, {"id": "B", "url": "https://www.airbnb.com/s/homes"},
                {"id": "C", "url": "https://www.espn.com/nfl/"}, {"id": "D", "url": "https://www.airbnb.com/s/homes"}]
        with mock.patch.object(cdp, "Page", Tab), mock.patch.object(cdp, "pages", lambda dev: tabs), \
                mock.patch.object(cdp, "http_get", lambda dev, path, timeout=4.0: closed.append(path) or {}):
            page = cdp.front_page(None, hint="https://www.airbnb.com/s/homes")
        self.assertEqual(page.target, "D")
        self.assertEqual(closed, ["/json/close/B"])  # dead and at the address; not C (elsewhere), not A (alive), not D
        # no hint: nothing closed
        closed.clear()
        with mock.patch.object(cdp, "Page", Tab), mock.patch.object(cdp, "pages", lambda dev: tabs), \
                mock.patch.object(cdp, "http_get", lambda dev, path, timeout=4.0: closed.append(path) or {}):
            cdp.front_page(None)
        self.assertEqual(closed, [])

    def test_front_page_after_a_launch_is_one_short_round(self):
        # quick: the first tabs and the hinted ones, short bounds, no long
        # second look at the first tab, no sweep of the others
        cdp = _cdp()
        probes = []

        pipelined = []

        class Tab:
            def __init__(self, dev, target, probe_s=None, pipeline=True):
                probes.append((target, probe_s))
                pipelined.append(pipeline)
                self.target, self.visible_at = target, 0.0
                if target == "A":
                    raise TimeoutError("timed out")  # busy: no answer in a probe's time

            def close(self):
                pass
        tabs = [{"id": t, "url": "https://%s.com/" % t.lower()} for t in "ABCDEFGH"]
        tabs[4]["url"] = "https://www.airbnb.com/s/homes"
        with mock.patch.object(cdp, "Page", Tab), mock.patch.object(cdp, "pages", lambda dev: tabs):
            with self.assertRaises(RuntimeError) as cm:
                cdp.front_page(None, hint="airbnb.com/s/homes", quick=True)
        self.assertEqual(sorted(probes), [("A", None), ("B", None), ("C", None), ("E", None)])
        # after a launch the commands wait for the handshake's answer (Chrome
        # dropped every session that sent them with it, Oct 7)
        self.assertEqual(set(pipelined), {False})
        self.assertEqual(str(cm.exception), "no visible page among Chrome's 4 tabs probed: "
                         "a.com: no answer (TimeoutError); b.com: hidden; c.com: hidden; airbnb.com/s/homes: hidden")

    def test_front_page_answers_as_soon_as_a_tab_says_visible(self):
        # a cold open after HOME, Oct 5: the link's tab (hinted, listed
        # fourth) answered within a second; the first tab, frozen, was
        # waited out for its 6s bound before the scan returned
        cdp = _cdp()
        import time as _time
        probes, closed = [], []

        class Tab:
            def __init__(self, dev, target, probe_s=None, pipeline=True):
                probes.append((target, probe_s))
                self.target, self.visible_at = target, 0.0
                if target == "A":
                    _time.sleep(0.6)  # frozen: answers (hidden) only late
                elif target == "D":
                    self.visible_at = 1.0

            def close(self):
                closed.append(self.target)
        tabs = [{"id": "A", "url": "https://a.com/"}, {"id": "B", "url": "https://b.com/"},
                {"id": "C", "url": "https://c.com/"}, {"id": "D", "url": "https://www.airbnb.com/rooms/1"}]
        t0 = _time.monotonic()
        with mock.patch.object(cdp, "Page", Tab), mock.patch.object(cdp, "pages", lambda dev: tabs):
            page = cdp.front_page(None, first_probe_s=6.0, hint="https://www.airbnb.com/rooms/1")
            took = _time.monotonic() - t0
            self.assertEqual(page.target, "D")
            self.assertLess(took, 0.4)  # not the frozen tab's bound
            # the hinted tab got the long bound with the first one; the rest the short one
            self.assertEqual(sorted(probes), [("A", 6.0), ("B", None), ("C", None), ("D", 6.0)])
            self.assertEqual(sorted(closed), ["B", "C"])  # hidden ones closed; A still being asked
            _time.sleep(0.7)
            self.assertIn("A", closed)  # answered late: closed by itself

    def test_front_page_sweeps_the_next_tabs_when_the_first_are_hidden(self):
        cdp = _cdp()
        probes = []

        class Tab:
            def __init__(self, dev, target, probe_s=None, pipeline=True):
                probes.append((target, probe_s))
                self.target, self.visible_at = target, (1.0 if target == "F" else 0.0)

            def close(self):
                pass
        tabs = [{"id": t, "url": "https://%s.com/" % t.lower()} for t in "ABCDEFGHIJKLMN"]
        with mock.patch.object(cdp, "Page", Tab), mock.patch.object(cdp, "pages", lambda dev: tabs):
            page = cdp.front_page(None)
        self.assertEqual(page.target, "F")
        # the first three; the current one answered hidden, so no long
        # probe of it; then the next MORE_TABS at once
        self.assertEqual([p[0] for p in probes], list("ABC") + list("DEFGHIJK"))
        self.assertTrue(all(p[1] is None for p in probes))

    def test_front_page_says_what_each_tab_answered(self):
        cdp = _cdp()

        class Tab:
            def __init__(self, dev, target, probe_s=None, pipeline=True):
                self.target, self.visible_at = target, 0.0
                if target == "B":
                    raise TimeoutError("timed out")
                if target == "C":
                    raise ConnectionError("websocket handshake refused: HTTP/1.1 500 Internal Server Error")

            def close(self):
                pass
        tabs = [{"id": "A", "url": "https://www.airbnb.com/s/Woodbury/homes"},
                {"id": "B", "url": "https://x.com/"}, {"id": "C", "url": ""}]
        with mock.patch.object(cdp, "Page", Tab), mock.patch.object(cdp, "pages", lambda dev: tabs):
            with self.assertRaises(RuntimeError) as cm:
                cdp.front_page(None)
        self.assertEqual(str(cm.exception), "no visible page among Chrome's 3 tabs probed: "
                         "airbnb.com/s/woodbury/homes: hidden; x.com: no answer (TimeoutError); "
                         "(no address): refused (websocket handshake refused: HTTP/1.1 500 Internal Server Error)")
        self.assertEqual(cdp._answer(ConnectionError("")), "refused")

    def test_url_bar_of_reads_chrome_s_address_bar(self):
        mod = _u2mux()
        xml = ('<hierarchy rotation="0"><node index="0" text="airbnb.com/s/Woodbury/homes?checkin=2026&amp;x=1" '
               'resource-id="com.android.chrome:id/url_bar" class="android.widget.EditText" package="com.android.chrome" '
               'bounds="[200,160][700,260]" clickable="true" enabled="true"/></hierarchy>')
        self.assertEqual(mod.url_bar_of(xml), "airbnb.com/s/Woodbury/homes?checkin=2026&x=1")
        self.assertEqual(mod.url_bar_of(SAMPLE_XML), "")
        self.assertEqual(mod.url_bar_of(""), "")

    def test_the_helper_hints_the_tab_scan_with_the_bar_or_the_link_it_couldnt_open(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = WebPathTests._fake_cdp(self, mod, calls)
        hints = []
        fake.front_page = lambda dev, current=None, **kw: hints.append(kw.get("hint")) or _FakePage()
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([CHROME_XML] * 3)  # the launch check's reads
        mod.log = lambda *a: None
        # Chrome not in front: the link is launched by the CLI, and remembered
        dm._last_xml, dm._last_xml_t = SAMPLE_XML, mod._time.monotonic()
        with self.assertRaises(RuntimeError):
            dm.cmd_act(json.dumps({"open": "https://www.airbnb.com/s/Woodbury/homes"}))
        self.assertEqual(dm._address_hint(SAMPLE_XML), "https://www.airbnb.com/s/Woodbury/homes")
        # Chrome's bar on the newest read comes first
        bar = CHROME_XML.replace(
            '<node text="" class="android.webkit.WebView"',
            '<node text="espn.com/nfl" resource-id="com.android.chrome:id/url_bar" class="android.widget.EditText"'
            ' package="com.android.chrome" bounds="[200,160][700,260]" clickable="true" enabled="true"/>\n'
            '    <node text="" class="android.webkit.WebView"')
        self.assertEqual(dm._address_hint(bar), "espn.com/nfl")
        dm._last_xml = bar
        dm._dump(fresh=True)
        self.assertEqual(hints, ["espn.com/nfl"])
        # a link just launched, told to `dump page`, is the hint and is remembered
        dm._last_xml = SAMPLE_XML
        self.assertIn('text="Box Score"', dm.cmd_dump("page https://www.airbnb.com/rooms/1").decode())
        self.assertEqual(hints[-1], "https://www.airbnb.com/rooms/1")
        self.assertEqual(dm._address_hint(SAMPLE_XML), "https://www.airbnb.com/rooms/1")
        # no bar, nothing opened lately: no hint (an older cdp module takes none)
        dm._opened_at = -1e9
        fake.front_page = lambda dev, current=None: _FakePage()
        dm._last_xml = CHROME_XML
        self.assertIn("WebView", dm._dump(fresh=True))

    def test_the_helper_logs_where_a_web_tap_went(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = WebPathTests._fake_cdp(self, mod, calls)
        fake.tap = lambda page, label, index=None, idle_ms=1200: {
            "found": True, "count": 1, "label": label, "how": "touch", "screen": WEB_SCREEN,
            "at": [164.1, 458.4], "tag": "a", "over": "a", "moved": True, "covered": False}
        logged = []
        mod.log = lambda *a: logged.append(" ".join(str(x) for x in a))
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([])
        dm._last_xml, dm._last_xml_t = CHROME_XML, mod._time.monotonic()
        dm.cmd_act(json.dumps({"tap_label": "Cabin in St. Croix Falls city"}))
        self.assertIn("web tap: touch on <a> at 164,458 laid over the words, scrolled into view", logged)

    def test_a_cold_open_tells_the_helper_the_link(self):
        self.allow("scrcpy_send", return_value=False)  # Chrome started through the scrcpy helper: not here
        self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.allow("u2_invalidate")
        calls = []

        def u2(cmd, arg="", timeout=30):
            calls.append((cmd, arg))
            if cmd == "act":
                pc._u2_status = "err act not sent: not a page"  # Chrome not in front
                return None
            return CHROME_XML if cmd == "dump" else "100"
        self.allow("u2sock", side_effect=u2)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_open(self.parse(["open", "https://www.airbnb.com/s/Woodbury/homes"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn(("dump", "page https://www.airbnb.com/s/Woodbury/homes"), calls)
        self.assertIn("screen: com.android.chrome", out.getvalue())

    def test_a_coordinate_tap_rereads_an_unchanged_screen_once(self):
        self.allow("wake_async", return_value=mock.Mock())
        self.allow("u2_invalidate")
        self.allow("nav_record_action")
        reads = self.allow("ui_dump", return_value=ET.fromstring(TAP_XML))

        def u2(cmd, arg="", timeout=30):
            if cmd == "dump" and arg == "cached":
                return TAP_XML
            return TAP_XML if cmd == "act" else "100"  # the tap changed nothing the read shows
        self.allow("u2sock", side_effect=u2)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_tap(self.parse(["tap", "--xy", "0.5,0.5"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(reads.call_count, 1)  # one re-read, not two
        self.assertIn("tapped --xy 0.5,0.5", out.getvalue())

    def test_start_gives_an_installed_app_s_cold_start_longer(self):
        self.allow("scrcpy_send", return_value=True)
        self.allow("u2_invalidate")
        self.allow("nav_record")
        adb = self.allow("adb_or_ensure", return_value=SimpleNamespace(
            returncode=0, stdout="package:/data/app/x/base.apk\n", stderr=""))
        play = SAMPLE_XML.replace("com.example", "com.android.vending")
        reads = []
        self.allow("u2sock", side_effect=lambda cmd, arg="", timeout=30:
                   reads.append(cmd) or (play if len(reads) <= pc.LAUNCH_REREADS + 3 else SAMPLE_XML))
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_start(self.parse(["start", "com.example"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(len(reads), pc.LAUNCH_REREADS + 4)  # the quick reads, then more while the app started
        self.assertEqual(adb.call_count, 1)  # asked once whether the app is installed
        self.assertIn("screen: com.example", out.getvalue())
        self.assertNotIn("didn't come to the front", err.getvalue())
        # an app that isn't installed gets its verdict after the quick reads
        self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=1, stdout="", stderr=""))
        reads.clear()
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_start(self.parse(["start", "com.nope"]))
        self.assertEqual(rc, 1)
        self.assertEqual(len(reads), 1 + pc.LAUNCH_REREADS)
        self.assertIn("com.nope isn't installed", err.getvalue())

    def test_front_page_names_the_first_tabs_and_counts_the_rest(self):
        cdp = _cdp()

        class Tab:
            def __init__(self, dev, target, probe_s=None, pipeline=True):
                self.target, self.visible_at = target, 0.0
                if target not in ("A", "F", "G"):
                    raise ConnectionError("")

            def close(self):
                pass
        tabs = [{"id": t, "url": "https://%s.com/" % t.lower()} for t in "ABCDEFGHIJKL"]
        with mock.patch.object(cdp, "Page", Tab), mock.patch.object(cdp, "pages", lambda dev: tabs):
            with self.assertRaises(RuntimeError) as cm:
                cdp.front_page(None)
        self.assertEqual(str(cm.exception), "no visible page among Chrome's 11 tabs probed: "
                         "a.com: hidden; b.com: refused; c.com: refused; the other 8: 6 refused, 2 hidden")

    def test_the_home_key_waits_less_for_the_launcher(self):
        self.allow("u2_invalidate")
        self.allow("nav_record")
        calls = []
        self.allow("u2sock", side_effect=lambda cmd, arg="", timeout=30: calls.append((cmd, json.loads(arg))) or SAMPLE_XML)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            self.assertEqual(pc.cmd_press(self.parse(["press", "HOME"])), 0)
            self.assertEqual(pc.cmd_press(self.parse(["press", "BACK"])), 0)
        self.assertEqual([c[1] for c in calls], [{"key": 3, "idle": pc.IDLE_HOME_MS}, {"key": 4, "idle": pc.IDLE_ACT_MS}])
        self.assertIn("pressed HOME", out.getvalue())

    def test_every_kind_of_space_is_one_space_when_words_are_matched(self):
        # the Clock app, Oct 5: "6:30 AM" drawn with a narrow no-break space
        # before AM; the assistant typed a plain one; the exact match missed
        # the row, the fuzzy one found the two descriptions around it
        self.assertEqual(pc.plain_words("6:30\u202fAM"), "6:30 AM")
        self.assertEqual(pc.plain_words("  a\u00a0\u00a0b \t c "), "a b c")
        self.assertEqual(pc.plain_words(""), "")
        clock = SAMPLE_XML.replace("</hierarchy>", """  <node text="" content-desc="Alarm Tomorrow 6:30 AM Alarm is currently enabled." class="android.view.ViewGroup" package="com.example" bounds="[0,400][1080,700]" clickable="true" enabled="true">
    <node text="6:30\u202fAM" class="android.widget.TextView" package="com.example" bounds="[100,500][400,660]" clickable="true" enabled="true"/>
    <node text="" content-desc="6:30 AM alarm" class="android.widget.Switch" package="com.example" bounds="[800,500][1030,660]" clickable="true" enabled="true"/>
  </node>
</hierarchy>""")
        nodes = pc.walk(ET.fromstring(clock))
        hits = pc.find_nodes(nodes, "6:30 AM")
        self.assertEqual([n["text"] for n in hits], ["6:30\u202fAM"])
        plan = pc.plan_tap(nodes, 1080, 2400, text="6:30 AM")
        self.assertEqual((plan["action"], plan["xy"]), ("tap", (250, 580)))
        mod = _u2mux()
        self.assertEqual(mod.plain_words("6:30\u202fAM"), "6:30 AM")
        row, alt = mod.label_node(clock, "6:30 AM")
        self.assertEqual((row["text"], alt), ("6:30\u202fAM", "6:30 AM"))
        self.assertEqual(mod.find_node(clock, "6:30 AM", fuzzy=False)["text"], "6:30\u202fAM")
        self.assertEqual(mod.find_node(clock, "6:30 am", fuzzy=False)["text"], "6:30\u202fAM")

    def test_a_read_the_helper_refused_for_a_lost_phone_goes_to_ensure(self):
        # Oct 6: adb's own reader can't read while the helper's server
        # holds the phone; four tries at it ran into the 60s watchdog
        self._guards["ui_dump"].stop()  # the real one; what it falls back to is mocked below
        root = ET.fromstring(SAMPLE_XML)
        dumps = iter([None, root])
        calls = []

        def fast_dump(fresh=False):
            d = next(dumps)
            if d is None:
                pc._u2_status = ("err the phone is gone from adb (AdbError: device not found) "
                                 "and did not come back: the link through the tunnel is down")
            return d
        with mock.patch.object(pc, "_u2_status", "ok"), mock.patch.object(pc, "fast_dump", fast_dump), \
                mock.patch.object(pc, "ensure", lambda heal_fast_paths=True: calls.append(heal_fast_paths) or True), \
                mock.patch.object(pc, "adb_or_ensure", lambda *a, **k: self.fail("adb's reader isn't tried")), \
                mock.patch.object(pc, "wake", lambda: self.fail("no wake either")):
            self.assertIs(pc.ui_dump(), root)
            self.assertEqual(calls, [False])  # adb only: the helper heals itself on its next command
            # the helper still refusing after ensure: the reason, at once
            dumps = iter([None, None])
            with self.assertRaises(pc.PhoneUnreachable) as cm:
                pc.ui_dump()
            self.assertIn("gone from adb", str(cm.exception))
        self.assertFalse(pc.helper_lost_phone())

    def test_an_unreachable_phone_is_one_line_not_a_traceback(self):
        # the review of Oct 6: ui_dump's "the phone is unreachable" printed
        # as a traceback, which reads as a crash in burner
        def unreachable(args):
            raise pc.PhoneUnreachable("the phone is unreachable: the phone is gone from adb (AdbError: "
                                      "device '127.0.0.1:15555' not found) and did not come back")
        with mock.patch.object(pc, "cmd_state", unreachable), mock.patch.object(sys, "argv", ["burner", "state"]), \
                self.cap() as (out, err), self.assertRaises(SystemExit) as cm:
            pc.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("burner: the phone is unreachable: the phone is gone from adb", err.getvalue())
        self.assertIn("Run `burner ensure`", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        # the helper's word that its link answers nothing is a lost phone too
        with mock.patch.object(pc, "_u2_status", "err no answer from the phone's adb: the link died with the request out"):
            self.assertTrue(pc.helper_lost_phone())
        with mock.patch.object(pc, "_u2_status", "err act not sent: not on the last read"):
            self.assertFalse(pc.helper_lost_phone())

    def test_type_with_a_field_that_took_no_text_types_another_way(self):
        # the phone's setText answering false is "no editable field took the
        # text": the field is tapped by its label and typed the other ways
        self.allow("u2_invalidate")
        self.allow("nav_record")
        calls = []

        def u2(cmd, arg="", timeout=30):
            spec = json.loads(arg) if cmd == "act" else {}
            calls.append((cmd, spec))
            if cmd == "act" and spec.get("field"):
                pc._u2_status = ("err act failed after sending: no editable field took the text "
                                 "(the phone's setText said no)")
                return None
            return SAMPLE_XML
        self.allow("u2sock", side_effect=u2)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_type(self.parse(["type", "--field", "Password", "hunter2"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual([(c, s.get("set_text"), s.get("field"), s.get("tap_label")) for c, s in calls][:3],
                         [("act", "hunter2", "Password", None), ("act", None, None, "Password"),
                          ("act", "hunter2", None, None)])
        self.assertIn("typed 7 chars", out.getvalue())

    def test_a_fuzzy_label_never_means_a_bar_icon(self):
        # the battery task, Oct 6: "Battery" below the fold; the fuzzy match
        # found the status bar's "Battery 79 percent." and tapped it
        icon = ('<node text="" content-desc="Battery 79 percent." class="android.widget.ImageView" '
                'package="com.android.systemui" bounds="[900,30][1000,110]" clickable="false" enabled="true"/>')
        screen = SAMPLE_XML.replace("</hierarchy>", icon + "</hierarchy>")
        nodes = pc.walk(ET.fromstring(screen))
        self.assertEqual(pc.find_nodes(nodes, "Battery", exact=False), [])
        self.assertEqual(pc.plan_tap(nodes, 1080, 2400, text="Battery")["action"], "nomatch")
        self.assertEqual(len(pc.find_nodes(nodes, "Battery 79 percent.")), 1)  # an exact match still finds it
        # a row of the app with the words: found
        row = screen.replace("</hierarchy>", '<node text="Battery saver" class="android.widget.TextView" '
                             'package="com.android.settings" bounds="[100,1500][900,1600]" clickable="true" enabled="true"/></hierarchy>')
        self.assertEqual([n["text"] for n in pc.find_nodes(pc.walk(ET.fromstring(row)), "Battery", exact=False)],
                         ["Battery saver"])
        # the helper's waits alike
        mod = _u2mux()
        self.assertIsNone(mod.find_node(screen, "Battery"))
        self.assertEqual(mod.find_node(row, "Battery")["text"], "Battery saver")
        self.assertEqual(mod.find_node(screen, "Battery 79 percent.", fuzzy=False)["desc"], "Battery 79 percent.")

    def test_a_failed_tap_s_evidence_takes_no_screenshot(self):
        # Oct 6: a failed `tap Storage` took 3.3s, 1.45s of it a screenshot
        # the assistant never opened (it took its own with `burner shot`)
        dump = self.allow("ui_dump", return_value=ET.fromstring(SAMPLE_XML))
        with mock.patch.object(pc, "shot_fast", side_effect=AssertionError("no screenshot")), \
                mock.patch.object(pc, "_cached_root", return_value=None):
            ev = pc._capture_evidence("tap-fail")
        self.assertIsNone(ev["screenshot"])
        # the rows as the screen prints them (Oct 7: the first row listed
        # was the status bar's clock)
        self.assertEqual(ev["screen"], pc.screen_lines(pc.walk(ET.fromstring(SAMPLE_XML)), *pc.screen_dims())[0])
        self.assertIn("Hello (300,250)", ev["screen"])
        clock = SAMPLE_XML.replace("</hierarchy>", '<node text="8:14" class="android.widget.TextView" '
                                   'package="com.android.systemui" bounds="[40,10][160,90]"/></hierarchy>')
        dump.reset_mock()
        with mock.patch.object(pc, "_cached_root", return_value=ET.fromstring(clock)):
            ev2 = pc._capture_evidence("tap-fail")
        self.assertFalse(any("8:14" in row for row in ev2["screen"]))
        dump.assert_not_called()  # the newest read, not one more
        text = pc._format_evidence(ev)
        self.assertIn("screen was showing:", text)
        self.assertIn("(`burner state` lists it all; `burner shot` shows it)", text)

    def test_the_third_scroll_the_same_way_earns_a_hint(self):
        # Muse scrolled up eight times, a read each, to reach the top (Oct 5)
        import datetime
        now = 1_800_000_000.0
        stamp = lambda dt: datetime.datetime.fromtimestamp(now - dt).strftime("%Y-%m-%d %H:%M:%S")
        up = lambda dt: "%s   1523ms exit 0 burner scroll up\n" % stamp(dt)
        self.assertTrue(pc.scroll_streak([up(30), up(10)], "up", now))
        self.assertTrue(pc.scroll_streak(["%s    20ms exit 0 burner state\n" % stamp(60), up(30), up(10)], "up", now))
        self.assertFalse(pc.scroll_streak([up(30), "%s    20ms exit 0 burner state\n" % stamp(10)], "up", now))
        self.assertFalse(pc.scroll_streak([up(30), up(10)], "down", now))  # the other way
        self.assertFalse(pc.scroll_streak([up(300), up(10)], "up", now))  # too long ago
        self.assertFalse(pc.scroll_streak(["%s   900ms exit 0 burner scroll up --times 3\n" % stamp(30), up(10)], "up", now))
        self.assertFalse(pc.scroll_streak([up(10)], "up", now))
        # the hint, from the command log, on a native scroll
        self.allow("live_screen_dims", return_value=(1080, 2400))
        self.allow("_scrcpy_swipe", return_value=True)
        self.allow("read_after_root", return_value=(ET.fromstring(SAMPLE_XML), ""))
        log = os.path.join(tempfile.mkdtemp(), "commands.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write(up(30) + up(10))
        with mock.patch.object(pc, "COMMANDS_LOG", log), mock.patch.object(pc, "_screen_pkg", "com.example"), \
                mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc._scroll_plain("up", 1)
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("(3 scrolls up in a row: `burner scroll up --times 5` goes further in one command, "
                      "`burner scroll top` to the end)", out.getvalue())
        # a page in Chrome scrolled by the page itself: the same hint
        self.allow("act_and_read", return_value=("ok", ET.fromstring(CHROME_XML), ""))
        with mock.patch.object(pc, "COMMANDS_LOG", log), mock.patch.object(pc, "_screen_pkg", "com.android.chrome"),                 mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            self.assertEqual(pc._web_scroll("up", 1), 0)
        self.assertIn("(3 scrolls up in a row:", out.getvalue())
        # --times 2: no hint (the assistant uses it already); no streak: no hint
        with mock.patch.object(pc, "COMMANDS_LOG", log), mock.patch.object(pc, "_screen_pkg", "com.example"), \
                mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            pc._scroll_plain("up", 2)
            pc._scroll_plain("down", 1)
        self.assertNotIn("in a row", out.getvalue())

    def test_burner_tabs_lists_chrome_s_tabs(self):
        mod = _u2mux()
        calls = []
        fake = WebPathTests._fake_cdp(self, mod, calls)
        fake.pages = lambda dev: [{"id": "A", "url": "https://www.airbnb.com/s/homes", "title": "Airbnb", "type": "page"},
                                  {"id": "B", "url": "https://www.espn.com/nfl/", "title": "NFL", "type": "page"}]
        dm = EmptyScreenTests._daemon(self, mod)
        dm.d = _FakeServer([])
        self.assertEqual(json.loads(dm.cmd_tabs("").decode()),
                         [{"id": "A", "url": "https://www.airbnb.com/s/homes", "title": "Airbnb"},
                          {"id": "B", "url": "https://www.espn.com/nfl/", "title": "NFL"}])
        self.allow("u2sock", return_value=dm.cmd_tabs("").decode())
        with self.cap() as (out, err):
            self.assertEqual(pc.cmd_tabs(self.parse(["tabs"])), 0)
        self.assertEqual(out.getvalue(), "2 tabs in Chrome" + chr(10) + "  https://www.airbnb.com/s/homes  Airbnb"
                         + chr(10) + "  https://www.espn.com/nfl/  NFL" + chr(10))
        self.allow("u2sock", return_value="")
        with self.cap() as (out, err):
            self.assertEqual(pc.cmd_tabs(self.parse(["tabs"])), 1)
        self.assertIn("can't be listed", err.getvalue())

    def test_a_link_launched_carries_burner_s_own_tab_id(self):
        self.allow("scrcpy_send", return_value=False)  # Chrome started through the scrcpy helper: not here
        adb = self.allow("adb_or_ensure", return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.allow("u2_invalidate")
        def u2(cmd, arg="", timeout=30):
            if cmd == "act":
                pc._u2_status = "err act not sent: not a page"  # Chrome not in front: the link is launched
                return None
            return CHROME_XML if cmd == "dump" else "100"
        self.allow("u2sock", side_effect=u2)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            pc.cmd_open(self.parse(["open", "https://www.airbnb.com/s/homes"]))
        scripts = [c[0][1] for c in adb.call_args_list]
        self.assertIn("resolve-activity", scripts[0])  # Chrome brought up first (no Chrome here: the intent)
        script = [s for s in scripts if "android.intent.action.VIEW" in s][0]
        self.assertIn("--es com.android.browser.application_id burner", script)
        self.assertNotIn("application_id com.android.chrome", script)

    def test_a_link_while_chrome_is_in_the_background_loads_in_the_tab_chrome_comes_back_to(self):
        # a link launched by intent added a tab every time (120 on a phone)
        # and the new tab took up to 17s to answer, Oct 5: Chrome is brought
        # up as a tap on its icon does, and its tab loads the link
        self.allow("scrcpy_send", return_value=True)  # Chrome started through the scrcpy helper
        self.allow("adb_or_ensure", return_value=SimpleNamespace(
            returncode=0, stdout="  mFocusedApp=ActivityRecord{1 u0 com.android.chrome/.Main t1}\n", stderr=""))
        self.allow("u2_invalidate")
        calls = []

        def u2(cmd, arg="", timeout=30):
            spec = json.loads(arg) if cmd == "act" else arg
            calls.append((cmd, spec))
            if cmd == "act" and not spec.get("launched"):
                pc._u2_status = "err act not sent: not a page"  # Chrome not in front
                return None
            return CHROME_XML if cmd == "act" else "100"
        self.allow("u2sock", side_effect=u2)
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_open(self.parse(["open", "https://www.airbnb.com/s/homes"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual([c for c in calls if c[0] == "act"],
                         [("act", {"open": "https://www.airbnb.com/s/homes", "idle": pc.IDLE_LAUNCH_MS}),
                          ("act", {"open": "https://www.airbnb.com/s/homes", "launched": True, "idle": pc.IDLE_LAUNCH_MS})])
        self.assertNotIn(("dump", "page https://www.airbnb.com/s/homes"), calls)  # no intent, no launch read
        self.assertIn("opened https://www.airbnb.com/s/homes", out.getvalue())
        self.assertIn("screen: com.android.chrome", out.getvalue())
        # the same with Chrome named on the command line, as the guide has it
        calls.clear()
        with mock.patch.object(pc.time, "sleep"), self.cap() as (out, err):
            rc = pc.cmd_open(self.parse(["open", "https://www.airbnb.com/s/homes", "com.android.chrome"]))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual([c[1].get("launched") for c in calls if c[0] == "act"], [None, True])
        self.assertNotIn(("dump", "page https://www.airbnb.com/s/homes"), calls)

    def test_the_helper_opens_a_link_after_a_launch_the_launch_way(self):
        mod = _u2mux()
        EmptyScreenTests.no_sleep(self, mod)
        calls = []
        fake = WebPathTests._fake_cdp(self, mod, calls)
        fake.front_page = lambda dev, current=None, **kw: calls.append(("front_page", kw.get("quick"), kw.get("hint"))) or _FakePage()
        fake.navigate = lambda page, url, idle_ms=1000: calls.append(("navigate", url)) or {"screen": WEB_SCREEN, "ready": "complete"}
        dm = EmptyScreenTests._daemon(self, mod)
        mod.log = lambda *a: None
        dm.d = _FakeServer([CHROME_XML])  # the launch check: Chrome in front
        dm._last_xml, dm._last_xml_t = SAMPLE_XML, mod._time.monotonic()  # the newest read: the launcher
        xml = dm.cmd_act(json.dumps({"open": "https://www.espn.com/nfl/", "launched": True, "idle": 1000})).decode()
        self.assertIn('text="Box Score"', xml)
        self.assertEqual(calls[:2], [("front_page", True, "https://www.espn.com/nfl/"), ("navigate", "https://www.espn.com/nfl/")])
        # without "launched", the newest read decides, as before: not a page
        dm._last_xml, dm._web = SAMPLE_XML, None
        with self.assertRaises(RuntimeError) as cm:
            dm.cmd_act(json.dumps({"open": "https://www.espn.com/nfl/"}))
        self.assertEqual(str(cm.exception), "act not sent: not a page")

    def test_the_update_waits_for_a_helper_that_is_connected_not_just_up(self):
        # after an update the helper answered "dead" (its server being
        # restarted) and the update said "helpers restarted."; the next
        # command paid 50s (Oct 5)
        answers = iter(["dead", "dead", "alive"])
        self.allow("u2sock", side_effect=lambda cmd, arg="", timeout=30: next(answers))
        with mock.patch.object(pc.os.path, "exists", return_value=True), mock.patch.object(pc.time, "sleep"):
            self.assertTrue(pc.wait_for_helper(5))
        self.allow("u2sock", return_value="dead")
        clock = iter([0.0, 0.0, 10.0])
        with mock.patch.object(pc.os.path, "exists", return_value=True), mock.patch.object(pc.time, "sleep"),                 mock.patch.object(pc.time, "time", side_effect=lambda: next(clock, 10.0)):
            self.assertFalse(pc.wait_for_helper(5))

    def test_a_selected_tab_takes_the_tap_beside_a_title_with_the_same_words(self):
        # the Clock app, Oct 5: the title "Alarms" and the navigation bar's
        # "Alarms" tab, none of them clickable (the tab reads as selected):
        # the helper refused the tap as two rows, the plan too
        clock = """<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" package="com.google.android.deskclock" bounds="[0,0][1080,2400]" clickable="false" enabled="true">
    <node text="Alarms" class="android.widget.TextView" package="com.google.android.deskclock" bounds="[63,177][245,262]" clickable="false" enabled="true"/>
    <node text="" content-desc="Alarms" class="android.widget.FrameLayout" package="com.google.android.deskclock" bounds="[0,2169][216,2337]" clickable="false" enabled="true" selected="true">
      <node text="Alarms" class="android.widget.TextView" package="com.google.android.deskclock" bounds="[48,2280][166,2320]" clickable="false" enabled="true" selected="true"/>
    </node>
    <node text="" content-desc="World Clock" class="android.widget.FrameLayout" package="com.google.android.deskclock" bounds="[216,2169][432,2337]" clickable="false" enabled="true" selected="false"/>
  </node>
</hierarchy>"""
        root = ET.fromstring(clock)
        pc._update_screen_from_dump(root)
        plan = pc.plan_tap(pc.walk(root), 1080, 2400, text="Alarms")
        self.assertEqual((plan["action"], plan["xy"]), ("tap", (108, 2253)))
        mod = _u2mux()
        row, alt = mod.label_node(clock, "Alarms")
        self.assertEqual(row["bounds"], "[0,2169][216,2337]")
        # two tabs alike, neither selected: still two rows
        two = clock.replace('selected="true"', 'selected="false"').replace('content-desc="World Clock"', 'content-desc="Alarms"')
        self.assertEqual(pc.plan_tap(pc.walk(ET.fromstring(two)), 1080, 2400, text="Alarms")["action"], "ambiguous")
        with self.assertRaises(RuntimeError):
            mod.label_node(two, "Alarms")
