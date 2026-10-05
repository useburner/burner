---
name: burner
description: Use when the user says "use my burner", "on my burner", "what's on my burner?" (screenshot its screen and send the image), or asks you to do something in a phone app (open an app, tap, type, scroll, check an order). burner gives you a real Android phone the user set aside for you, for apps with no API or website you can automate. This is the fast guide: every action prints the screen it ends on.
---

# burner, the fast way

burner gives you, the AI assistant, a real Android phone the user set aside
for you: the burner phone. You drive it with the `burner` command. Call it by
its full path, `~/burner/bin/burner`, because each of your commands runs in a
fresh shell. "Use my burner" means this phone.

**Get this version:** `curl -fsSL https://raw.githubusercontent.com/useburner/burner/fast/docs/install.sh | BURNER_REF=fast bash`,
then read `~/burner/SKILL-fast.md` (this guide) instead of SKILL.md from now
on. `burner version` shows `on branch fast` when it worked. First-time setup
(pairing a phone) is in https://useburner.si/skill.md; do that first, then
run the line above.

## Work fast

Every step you take costs a round trip, so the fewer steps, the faster.

1. **One command per screen change. Its output is the screen.** `tap`,
   `scroll`, `open`, `start`, `type`, `press`, `wait` and `dismiss` wait for
   the screen to settle and print it: `screen: <app>`, then one row per thing
   on screen, top to bottom, with `(click)` on rows you can tap and `(on)` or
   `(off)` on switches. Don't follow an action with `dump`, `state`, a
   screenshot or a sleep: you already have the result.
2. **Screenshots only to see pictures** (a photo, a map, a chart):
   `burner shot --out .`, then send the file. For words, the rows are enough.
3. **Never sleep.** A page still loading: `burner wait "Text" --timeout 30`.
4. **Links skip menus:** `burner open "https://www.vinted.com/catalog?search_text=nike"`
   opens inside the app when it is installed. Deep links too: `burner open market://details?id=com.tinder com.android.vending`.
5. **Chain steps you are sure of:** `burner do 'start com.tinder; wait "Likes"'`
   runs them in one call and prints the last screen. `burner recipes` lists saved chains.
6. **Tap by the words on the row:** `burner tap "Connected devices"`. Two
   matches: add `--index 1`. No words on it: `burner snap`, then `burner tap @e3`.
7. **Type into the focused field:** `burner type "text"` (`--field "Search"`
   taps that field first). It prints the screen, so check the text there before
   any Send, Post, Buy or Submit tap, and only tap it with the user's go-ahead.
8. A command that fails or hangs: run `burner ensure` once and try again. If it
   still fails, tell the user plainly what it printed (it says what is wrong).
   When the user asks why something was slow, `burner log --last 20` shows
   each command's time; paste it rather than guessing.

## Common requests

| The user says | Do this |
|---|---|
| "What's on my burner?" / "Take a screenshot of my burner" | The screen right now: `burner shot --out .` and send the image. |
| "Use Tinder to…" (any app or service by name) | `burner apps tinder` finds its package, then `burner start <package>`: the launch prints the first screen. If the app isn't installed or stops you at a sign-in wall, read the site in the phone's browser instead: `burner open "https://…" com.android.chrome` (read-only, and tell the user). |
| "Install Snapchat" (any free app) | `PLAY_PACKAGE=com.snapchat.android burner recipe play-install` with the package (a Play Store link ends in `?id=<package>`). Name only: `APP_QUERY=Vinted burner recipe play-search`, pick the right result (`burner tap "Vinted" --index 0`), then `burner recipe play-install-current`. The recipes open the store themselves; a listing that says Open or Installed means it's already there. Never buy an app. |
| "Turn on Bluetooth" / a phone setting | `burner settings bluetooth` opens that page in one call (`burner settings` lists the pages: wifi, display, sound, battery, apps, location, `app <package>`...). Switch rows show `(on)` or `(off)`; tap the row to flip it. |
| "Check my burner" / battery / "is it on?" | `burner status`. |
| "Check my notifications" / "anything new?" | `burner notifications` prints them and changes nothing. |
| "Check my Amazon order" | `burner amazon-status`. |
| "Check my messages" | `burner apps messages`, `burner start <package>`, read the rows; open a conversation only to read it. |
| "Scroll down" / "scroll to the top" | `burner scroll down`, `burner scroll top`: inside the open app, never HOME or an edge swipe. |
| "Do this every time" / "save that" | Turn the steps that worked into a recipe with `burner save <name> 'step' ...` (labels, not coordinates; `$VARIABLE` for text that changes), test it once with `burner recipe <name>`. |
| "Update my burner" | `burner update` (it stays on this version), then re-read `~/burner/SKILL-fast.md`. |
| "Uninstall burner" / "forget it" | `burner uninstall` lists the phone changes it undoes; `--yes` once the user agrees, then delete `~/burner` and your notes about it. |

## Rules

- **Stop before submitting.** Typing and submitting are two steps with their
  own go-ahead each. Never type and tap Send, Post, Buy or Submit in one
  unattended flow. Purchases need the user's yes for the exact item, price and
  payment method, every time.
- **Never sign in, sign up or pick an account for the user.** A sign-in
  screen, "Continue with Google" or an account picker: stop and ask.
- **Verification codes come from email, never SMS** (the phone has no SIM):
  `burner vcode --from 'from:sender@example.com'` waits for the code box,
  fetches the code from the user's Gmail and types it; add `--submit "Continue"`
  only when the user OK'd submitting it. Codes are never stored.
- **Close pop-ups without agreeing:** `burner dismiss` taps Not now, Skip,
  Don't allow, Got it, Close. Refuse permissions and trials the task doesn't
  need, and say what you declined.
- **Only look at what the task needs.** No `dumpsys`, no raw `adb`, no account
  lists. burner's commands cover everything the phone needs.
- **Keep the user posted** in one line per step on the phone. A command stops
  itself after 60 seconds (exit code 124).
- The phone stays on its charger (the screen only stays on while charging).

## Commands

Run `burner <command> --help` for every option. Every action below prints the
screen it ends on unless you add `--quiet`.

```
Look
  burner shot [--out PATH]          screenshot (--out . to send it in chat)
  burner state                      the app in front and the rows on screen (no action)
  burner dump [--all] [--fresh]     every item on screen: text, type, position
  burner snap                       numbered rows (@e1…) for exact taps
  burner status                     is it up? battery, charging, screen, storage, apps
  burner notifications              read the phone's notifications (changes nothing)
  burner apps [name]                installed apps (--all includes system apps)

Act (each prints the screen it ends on)
  burner tap "Text"                 tap by label (--index N, --fuzzy, "A || B", @e3 from snap)
  burner tap --xy 0.5,0.8           tap a spot (0 to 1 across and down)
  burner type "text"                type into the focused field (--field "Hint", --clear)
  burner press BACK|HOME|ENTER      press a key (--repeat N)
  burner scroll [down|up|left|right|top|bottom]   scroll the open app (--times N, --to "Text")
  burner wait "Text"                wait for text (--timeout 30, "A || B" for either, --absent to wait for it to go)
  burner start <package>            open an app on its first screen
  burner settings [page]            a Settings page by name: bluetooth, wifi, display, battery, apps, app <package>...
  burner open <url> [package]       open a link, in one app if a package is given
  burner dismiss                    close pop-ups: Not now, Skip, Don't allow, Got it, Close

Flows
  burner do 'step; step'            several steps in one call; stops at the first failure
  burner recipe <name>              run a saved flow; burner recipes lists them
  burner save <name> 'step' ...     save a repeatable task as a recipe
  burner amazon-status              latest Amazon order status

Codes, health, upkeep
  burner gcode --from 'from:x@y'    newest code from Gmail (--mins 15)
  burner vcode --from 'from:x@y'    wait for the code box, fetch, type (--submit "Continue")
  burner doctor                     full health check
  burner ensure                     reconnect everything (5 seconds; up to a minute when it has to turn Wireless debugging back on)
  burner update                     update burner (stays on this version)
  burner version                    build, branch and skill rev
  burner log                        the last commands run, how long each took, exit codes
  burner uninstall [--yes]          undo the phone changes setup made
```

Details that save a step:

- `burner tap` refuses to guess: two matches for a label fails and lists them
  (`--index N` picks one). `"A || B"` tries labels in order. A label behind a
  dialog is refused: `burner dismiss` or `burner press BACK` first.
- `burner type` replaces the field's content, so `--clear` is rarely needed.
  Apps that ignore it get the text as key presses instead, automatically.
- `burner wait` polls on the phone every quarter second; a tap right after it
  needs no new read.
- A recipe file has one step per line in `burner do` format, `#` comments,
  and `$VAR` from the environment: `PLAY_PACKAGE=com.example.app burner recipe play-install`.
- `burner update` keeps `config.env`, the pairing and your own recipes.

## Troubleshooting

| Problem | Fix |
|---|---|
| Phone unreachable after a restart | Give it about 2 minutes, then `burner ensure`. |
| `burner ensure` says Wireless debugging is off | Android turns it off after a Wi-Fi blip or a restart; `burner ensure` asks the phone's adb-auto-enable app to turn it back on by itself and waits (the app's routine takes about a minute, so let it run). If it says the app isn't answering, ask the user to open adb-auto-enable on the phone once, or to turn on Wireless debugging in Developer options, then `burner ensure`. |
| Commands hang, or exit 124 | `burner ensure` restarts the connection and the on-phone helper (5 seconds; up to a minute when Wireless debugging was off). |
| `burner tap` says a label is cut off at an edge | burner nudges the screen a little by itself and taps once the row is whole; if it still says so, `burner scroll up` (top edge) or `burner scroll down` (bottom edge), then tap again. |
| `burner dump` comes back empty | burner wakes a dark screen and restarts the on-phone helper by itself; if reads stay empty, phone on its charger, then `burner ensure`. |
| `adb unauthorized` | The phone forgot this computer: pair again (setup step 5 in https://useburner.si/skill.md). |
| Apps say there is no internet | In the Tailscale app on the phone, turn off "Use Tailscale DNS settings". |
| `burner version` doesn't say `on branch fast` | Run the install line at the top of this guide again. |

Setup for a new phone or computer, and the security notes, are in the full
guide: https://useburner.si/skill.md. After that setup, run the install line
at the top of this guide to switch to the fast version.
