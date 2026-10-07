# Developer tools

For working on burner itself. None of these are needed to use it.

| Tool | What it does | Needs the phone |
|---|---|---|
| `loop.sh` | One speed round: bench, then verify, then compare with the last round. Exit 1 on a failed check or a slowdown. | yes |
| `bench.py` (`bench.sh`) | Times everyday commands: a warm-up pass ("cold"), then the median and max of N runs. `--save` appends to `run/bench-history.jsonl`. | yes |
| `benchcmp.py` | Compares two saved runs. Flags anything 20% and 300ms slower, or failing now. | no |
| `verify.py` | Behaviour checks a bench can't see: scroll distance, a fresh read after a scroll, row taps, dialog refusal, landscape taps, unique screenshot names. | yes |
| `tracesum.py` | Where a command's time went, from `BURNER_TRACE=json`: adb calls, sleeps, u2 RPCs, unaccounted. | no |
| `pagetaps.py` | burner's page taps against a local headless Chrome, each round trip delayed: pages that move the target after the scroll (a late image, a growing slot, a target that never stops) and pages where a good touch must pass. Exit 1 when a tap clicked anything but its target. | no (needs Chrome) |
| `linkbench.sh` | Raw link timings (one adb call, batched, parallel). | yes |
| `offburner.py` | Reads a pasted assistant timeline and lists every place it drove the phone without `burner`. | no |
| `fresh-install-test.sh` | Installs this checkout into a throwaway HOME and checks a clean install needs no hand patches. | no (needs network) |
| `sync_skill.py` | Copies SKILL.md to `docs/skill.md` and `skills/burner/SKILL.md`. `--check` only reports. | no |
| `hooks/pre-commit` | Runs `sync_skill.py --check` and the offline tests before each commit. | no |

Project skills for Claude live in `.claude/skills/`: `speed-loop` (the
bench, optimize, verify cycle) and `muse-loop` (testing through Muse).

## Saving a screen for a test

When a tap is refused or lands wrong on the phone:

```
burner dump --save tests/fixtures/<name>.xml
```

Add a case to `tests/fixtures/cases.json` with the right answer, check that
it fails, then fix `plan_tap` and the code it calls in `bin/burner`. Screens
can hold personal text, so save from neutral screens (Settings, Play Store)
or edit the text out before committing.

## Turning on the pre-commit hook

Once per checkout or worktree:

```
git config core.hooksPath tools/hooks
```

## Several sessions in one folder

Two sessions committing from one folder swept each other's unfinished
edits into their commits twice on Oct 2. Give each session its own
worktree inside the folder (`.worktrees/` is ignored):

```
git fetch origin
git worktree add --detach .worktrees/<session> origin/master
cd .worktrees/<session>
# work, then:
git fetch origin && git rebase origin/master
git push origin HEAD:master
```

The worktrees share one `.git`, so they are cheap. Each one needs its own
`bash install.sh` only if it will talk to the phone. `git worktree list`
shows them, and `git worktree remove .worktrees/<session>` cleans up.

## Benching from the dev machine (Windows)

burner runs on Linux and macOS, so on Windows use WSL. This lets a session on
the Windows machine bench and verify directly, instead of going through Muse
every round.

1. Install WSL once, from an admin PowerShell: `wsl --install -d Ubuntu`,
   then restart.
2. Tailscale must be running on Windows. Inside WSL, check the phone answers:
   `ping -c1 <phone's tailnet IP>`. If it doesn't, add `networkingMode=mirrored`
   under `[wsl2]` in `%UserProfile%\.wslconfig` and run `wsl --shutdown`.
3. Inside WSL, run `sudo apt-get install -y socat` (the tunnel uses it;
   with no `HTTPS_PROXY` set it goes straight to the phone), then
   `git clone https://github.com/useburner/burner ~/burner && cd ~/burner && bash install.sh`,
   then `bin/burner setup`. If `install.sh` fails with `$'\r': command not
   found`, the clone predates `.gitattributes`: run
   `git rm -q --cached -r . && git reset -q --hard` in `~/burner` to check
   the scripts out again with LF line endings. This computer is new to the phone, so it pairs
   once: someone taps Pair on the phone.
4. Bench any commit, including unpushed work in the Windows folder:

   ```
   wsl -e bash -lc "cd ~/burner && git fetch -q /mnt/c/Users/<you>/burner HEAD && git checkout -q FETCH_HEAD && bash tools/loop.sh"
   ```

Keep Muse for end-to-end checks of what the user actually sees.
