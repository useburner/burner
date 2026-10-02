#!/usr/bin/env python3
"""Time burner's everyday commands against the phone.

    python3 tools/bench.py                 # 3 runs after a warm-up, median/max table
    python3 tools/bench.py --runs 5
    python3 tools/bench.py --save          # also append to run/bench-history.jsonl
    BURNER_TRACE=1 python3 tools/bench.py  # also show each adb call

The first pass is reported separately as "cold": the first command after
`burner update` or a helper restart is slow once, and mixing it into the
median hid real changes last time. tools/benchcmp.py compares saved runs.
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time

import verify  # same folder: its adb helper and Settings reset

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BURNER = os.path.join(ROOT, "bin", "burner")
HISTORY = os.path.join(ROOT, "run", "bench-history.jsonl")
SHOT = os.path.join(tempfile.gettempdir(), "burner-bench.jpg")

# (name, argv). Order matters: start Settings before tapping in it. Each
# pass begins with Settings closed (untimed), so `start` and the search tap
# measure the same thing every pass.
#
# An action prints the screen it ends on, so its row is what an assistant
# pays for one step (the action, the wait for the screen to settle, and
# the read). The "(quiet)" rows are the bare action, for comparison with
# the history from before Oct 2, when every action was bare.
COMMANDS = [
    ("help", ["--help"]),
    ("status", ["status"]),
    ("state", ["state"]),
    ("dump", ["dump"]),
    ("dump (cached)", ["dump"]),
    ("shot", ["shot", "--out", SHOT]),
    ("notifications", ["notifications"]),
    ("apps", ["apps"]),
    ("start", ["start", "com.android.settings"]),
    ("scroll down", ["scroll", "down"]),
    ("scroll up", ["scroll", "up"]),
    # On Pixels search is its own app: the first BACK only hides its
    # keyboard, the second leaves it, so a scroll after the tap would
    # rightly stop ("left Settings").
    ("tap", ["tap", "Search settings || Search"]),
    ("type", ["type", "wifi"]),
    ("back", ["press", "BACK"]),
    ("back 2", ["press", "BACK"]),
    ("tap (quiet)", ["tap", "--quiet", "Search settings || Search"]),
    ("back (quiet)", ["press", "--quiet", "BACK"]),
]


def run_once(argv):
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, BURNER] + argv,
                           capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:  # one hung command mustn't lose the run
        return (time.time() - t0) * 1000, 124, "timed out after 180s"
    ms = (time.time() - t0) * 1000
    return ms, r.returncode, r.stderr


def json_field(argv, key, timeout):
    """One field of a `burner ... --json` reply, or None if it can't be read."""
    try:
        r = subprocess.run([sys.executable, BURNER] + argv + ["--json"],
                           capture_output=True, text=True, timeout=timeout)
        return (json.loads(r.stdout) or {}).get(key)
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


def phone_model():
    return json_field(["status"], "model", 60)


def commit():
    return json_field(["version"], "commit", 30)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", type=int, default=3, help="timed passes after the warm-up")
    ap.add_argument("--save", action="store_true", help="append to " + HISTORY)
    ap.add_argument("--json", action="store_true", help="print the record as JSON")
    ap.add_argument("--only", help="comma-separated command names to run")
    args = ap.parse_args()
    cmds = COMMANDS
    if args.only:
        want = {w.strip() for w in args.only.split(",")}
        cmds = [c for c in COMMANDS if c[0] in want]
    trace = bool(os.environ.get("BURNER_TRACE"))

    uses_settings = any(argv[0] in ("start", "tap", "press", "scroll") for _, argv in cmds)

    def reset(fn):  # one adb hiccup mustn't lose the run
        if not uses_settings:
            return
        try:
            fn()
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            print("bench: couldn't reset Settings: {}".format(e), file=sys.stderr)

    results = {name: {"runs": [], "exit": 0} for name, _ in cmds}
    for p in range(args.runs + 1):  # pass 0 is the cold/warm-up pass
        reset(verify.stop_settings)
        for name, argv in cmds:
            ms, rc, err = run_once(argv)
            if p == 0:
                results[name]["cold"] = round(ms)
            else:
                results[name]["runs"].append(round(ms))
            if rc != 0:
                results[name]["exit"] = rc
                results[name]["error"] = (err.strip().splitlines() or [""])[0][:160]
            if trace and p == args.runs:
                sys.stderr.write("".join("    " + l + "\n" for l in err.splitlines()))
    for r in results.values():
        r["median"] = round(statistics.median(r["runs"])) if r["runs"] else None
        r["max"] = max(r["runs"]) if r["runs"] else None

    record = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "commit": commit(),
              "model": phone_model(), "runs": args.runs, "results": results}
    if args.json:
        print(json.dumps(record))
    else:
        print("{:<16} {:>8} {:>8} {:>8}  {}".format("command", "median", "max", "cold", ""))
        for name, r in results.items():
            print("{:<16} {:>6}ms {:>6}ms {:>6}ms  {}".format(
                name, r["median"], r["max"], r.get("cold"),
                "" if r["exit"] == 0 else "exit {}: {}".format(r["exit"], r.get("error", ""))))
        print("commit {}  phone {}".format((record["commit"] or "?")[:7], record["model"] or "?"))
    if args.save:
        os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
        with open(HISTORY, "a") as f:
            f.write(json.dumps(record) + "\n")
    reset(verify.open_settings)  # leave the phone on Settings home
    # A failing command is a failed round, even if it failed last round too
    # (benchcmp only flags commands that newly fail).
    return 1 if any(r["exit"] != 0 for r in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
