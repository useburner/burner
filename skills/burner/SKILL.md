---
name: burner
description: Use when the user says "use my burner", "on my burner", "what's on my burner?" (screenshot its screen and send the image), or asks you to do something in a phone app (open an app, tap, type, scroll, check an order). burner gives you a real Android phone the user set aside for you, for apps with no API or website you can automate. Also covers first-time setup.
---

# burner

burner gives you, the AI assistant, a real Android phone that the user has set
aside for you: the burner phone. You drive it with the `burner` command, so you
can use phone apps that have no API, no MCP server and no website you can
automate: marketplaces, banking apps, social apps, and stores that block bots
in the browser.

In this guide "you" is the AI assistant and "the user" is the person you're
helping. When the user says "use my burner", "use my burner phone" or "do it on
my burner", they mean this phone.

**Not set up yet?** If the `burner` command isn't installed, or `burner doctor`
isn't green, go to [Setup](#setup) first. If the user only pointed you at this
guide ("Connect to useburner.si/skill.md", "set up burner"), they want it set
up: start Setup now, without asking whether they meant it.

## Common requests

| The user says | Do this |
|---|---|
| "What's on my burner?" or "Take a screenshot of my burner" | They mean the screen right now, not the installed apps. Take a screenshot and send the image (see below). |
| "Check my Amazon order" | `burner amazon-status` does it in one step. |
| "Use Tinder to…" (any app or service by name) | Use the phone's app, not a website. `burner apps tinder` finds its package, then `burner start <package>`. Use the website in the phone's browser only if the app isn't installed or walls you at sign-in (see "Working fast"). |
| "Install Snapchat" (any free app) | See "Installing apps" below. Never search the Play Store by tapping, and never tap by screen coordinates. |
| "Check my burner" / "is my burner on?" / battery, storage | `burner status` answers it in one call. Don't dig through `dumpsys`, which can expose accounts and other personal details. |
| "Check my notifications" / "anything new?" | `burner notifications` prints a count, then each notification (title and text), and changes nothing. Summarise what it printed for the user. |
| "Check my messages" | Open the Messages app (`burner apps messages`, then `burner start <package>`), read it with `burner state` or a screenshot, and open a conversation only to read it. Don't query `dumpsys` or the SMS database: Android hides the words there, and it can expose accounts. |
| "Do this every time" / "save that" / a task they repeat | See "Saving a task the user repeats" below: `burner save`. |
| A short word or name you don't recognize ("about", "weekly-orders") | It may be a saved recipe. Run `burner recipes`. If one matches, tell the user which one and what it does, and run it (`burner recipe <name>`) once they say yes. If they said "run <name>", just run it. |
| "Uninstall burner" / "forget it" | Run `burner uninstall` first: it lists the phone changes it will undo (screen lock, stay awake, helper apps, Wireless debugging). Run it with `--yes` once the user agrees, then delete the `~/burner` folder and any notes you saved about burner. Don't search files or read the installer. |
| "Update my burner" | Run `burner update`, then re-read `~/burner/SKILL.md` (the instructions change with updates), and say in one line that it's up to date. |
| "Move Chrome to the second page" / "put these apps in a folder" | See "Rearranging the home screen" below: `burner drag`. |
| "Scroll to the top" or "scroll down" | They mean inside the app that's open now. Use `burner scroll top`, `burner scroll down` and so on. Don't press HOME or swipe from a screen edge: that leaves the app or opens the app drawer. |

## Installing apps

Free apps only; never buy one. If the Play Store shows a price instead of
Install, stop and tell the user.

- **Know the package** (like `com.snapchat.android`), or look it up on the web
  (a Play Store link ends in `?id=<package>`): `PLAY_PACKAGE=<package> burner
  recipe play-install`. It opens the listing, taps Install and waits.
- **Only know the name:** `APP_QUERY=Vinted burner recipe play-search` opens
  the Play Store results (spaces as `%20`). Check the right app is listed
  (take a screenshot, results can be ads or lookalikes), `burner tap "Vinted"
  --index 0`, confirm the listing is the one you want, then `burner recipe
  play-install-current`.

## Working fast

Every screen read costs several seconds, so skip screens when you can.

- **Search with a link, not the keyboard.** Many sites and apps take the
  search in the address: `burner open "https://www.vinted.com/catalog?search_text=nike%20sneakers&price_to=35"`.
  If the app is installed, the link usually opens inside it.
- **Prefer the app, but don't fight a wall.** If an app stops you at a
  sign-in or onboarding screen you can't get past without signing in, you may
  browse the site in the phone's browser instead (`burner open <url>`), as
  long as it's read-only. Tell the user you did.
- **Close pop-ups in one go.** `burner dismiss` closes permission asks
  ("Don't allow"), tips ("Got it") and upsells ("Not now", "Close") without
  agreeing to anything. Refuse permissions and free trials unless the task
  needs them, and tell the user what you declined.

## Saving a task the user repeats

When the user says "do this every time", "save that" or "make that a recipe",
or does the same task again and again, turn what worked into a recipe.
`burner recipes` lists the ones that exist.

1. Do the task once, and note the steps that worked.
2. Clean them up: use labels (`tap "Orders"`), not coordinates or `@e` handles;
   put a `wait "Text"` after every step that loads a new screen; use a link
   (`open "https://…"`) in place of tapping through menus; and write anything
   that changes from run to run as a `$VARIABLE` (`type "$QUERY"`).
3. Save it, one step per argument, with no `burner` in front:
   `burner save weekly-orders --desc "Open my Amazon orders" 'open
   "https://www.amazon.com/gp/css/order-history"' 'wait "Your Orders"'`.
   `save` checks every step first and refuses fixed typed text (use a
   `$VARIABLE`, or `--allow-text`) and `@e` handles.
4. Run it once (`QUERY=shoes burner recipe weekly-orders`) to check it works,
   and tell the user its name and what to say: "run weekly-orders".

Pick a name that isn't one of burner's own (`burner recipes` shows them).
Recipes you save are kept when burner updates.

## Details

`burner apps` matches package names, not app names. Most match the app
(`com.tinder`), but some don't (X is `com.twitter.android`). If nothing
matches, run `burner apps` and read the full list.

**Sending a screenshot.** Save it where your chat can show files from, usually
your working folder: `burner shot --out .` saves a new, uniquely named file
there and prints its path. Attach that file. Don't reuse a file name: chat
apps cache images by name and keep showing the old screenshot. Plain
`burner shot` saves under `~/burner/shots/`, which many chat apps can't
display.

## Rules

**Stop before submitting.** Typing text and submitting it are two separate
steps, each needing its own go-ahead. Never type into a field and tap
Send, Post, Buy or Submit in the same unattended flow.

1. Type the text (`burner type`), then stop.
2. Check what the phone actually shows: take a screenshot and look at it, or
   run `burner dump` and confirm the field holds what you meant to type.
3. Only then tap the submit button, and only if the user approved that exact
   submission.

This covers purchases, messages, posts, emails, forms and anything else that
can't be undone.

**Never buy on your own.** Every purchase needs the user's explicit approval,
every time, for the exact item, price and payment method, not "buy something
like this".

**Verification codes come from email, never SMS.** The burner phone has no
SIM, so a screen that sends a code by text message is a dead end. `burner
gcode` and `burner vcode` read the code from the user's connected Gmail and
type it as plain text. Codes are never stored, and you never ask the user to
paste one. Use them only for a sign-in or check the user started and asked you
to finish. `vcode` stops after typing; add `--submit "Continue"` only when the
user has OK'd submitting that code.

**Privacy.** Typed text is never recorded. Navigation memory (`burner
whereami`, `burner route`) stores screen layouts and the kind of action taken,
never typed text, passwords or messages. Screenshots are never stored in it.

**Never sign in, sign up or pick an account for the user.** If an app isn't
signed in, or shows "Continue with Google", "Sign in" or an account picker,
stop and tell them plainly ("Tinder isn't signed in on your burner. Want to
sign in, or should I stop here?"). Signing in is always their call.

**Keep the user posted, and don't get stuck.** Say in a line what you're
about to do on the phone. If a command takes more than about 15 seconds,
tell them what you're waiting on. burner stops any command that gets no
result in 60 seconds (exit code 124). If one fails or times out, run
`burner ensure` once and try again; if it still fails, tell the user plainly
what's wrong instead of retrying quietly.

**Only look at what the task needs.** Don't read the phone's accounts, email
addresses or other personal details (for example with `adb shell dumpsys`)
unless the user asks for them.

**Keep the phone on its charger.** The screen only stays on while charging,
and when it goes dark `burner dump` comes back empty.

## Tapping precisely

`burner tap` refuses to guess. If a label matches two or more things on screen,
it fails and lists them instead of tapping the first one. Re-run with
`--index N` to pick one, or use a longer label that's unique. `"A || B"` tries
labels in order, so `burner tap "Checkout || Proceed to checkout"` survives a
rename. `--fuzzy` allows partial matches.

For multi-step flows, `burner snap` lists the same rows as `burner dump`,
numbered `@e1`, `@e2` and so on, and `burner tap @e3` taps exactly that one
with no re-matching. Handles only last for one screen: any tap, key press,
typing or app launch throws them away.

When you need to know what a tap did, add `--settle`. It waits until the screen
stops changing (a third of a second quiet, 10 seconds at most) and prints only what
appeared or disappeared, or `unchanged`. It re-reads the screen at least twice,
so it adds about 3 seconds: use it on taps whose result you must see, not on
every tap, and don't follow it with `burner state` or a screenshot (it already
told you). Waiting for a page or app to load: `burner wait "Text"`, not a fixed
sleep.

A plain `burner dump` can reuse a read from the last 2 seconds. When the
screen changes on its own (a page loading, an app updating, someone using the
phone), use `burner dump --fresh` to read it as it is now.

## Rearranging the home screen

`burner press HOME` shows the first page; if the key left an app in front,
it opens the home screen itself and says so. `burner drag "Chrome" 0.5,0.3`
long-presses the Chrome icon, carries it and lets go at that spot (0 to 1
across and down); the icons there make room. Dropped on another app
(`burner drag "Chrome" "Maps"`) it makes a folder, or joins one. `left` or
`right` between the two turns one page each: `burner drag "Chrome" right
0.5,0.3` puts it on the next page (after a turn, give the drop as X,Y: names
are found on the page you started on). A name shown twice (on the page and in
the dock) needs `burner snap` and `@eN`. Check the screen it prints: if a
shortcuts menu opened instead, `burner press BACK` and try `--hold 1200`.
Move only what the user asked for, and never drop on "Remove" or "Uninstall"
at the top of the screen.

## Commands

Run `burner <command> --help` for every option.

If `burner` isn't found, call it by its full path, `~/burner/bin/burner`,
rather than relying on an `export PATH=...` from an earlier command (many
assistants start each command in a fresh shell). If `~/burner` doesn't exist,
this is a new computer: do setup steps 1 and 3, then `burner doctor`. A
computer the phone hasn't seen before also needs pairing once (step 5).

```
Look at the screen
  burner shot [--out PATH]          screenshot (--out . to send it in chat)
  burner status                     is it up? battery, charging, screen, storage, apps
  burner state                      open app + the main text on screen
  burner text [--from N]            the words uncut: in Chrome the whole page, top to bottom
  burner dump [--all] [--fresh]     every item on screen: text, type, position
  burner snap [--all]               numbered list (@e1…) for exact taps

Act
  burner dismiss                    close pop-ups: Not now, Skip, Don't allow, Got it, Close
  burner tap "Text"                 tap by label (--index N, --fuzzy, --settle, "A || B")
  burner tap @e3                    tap a snap handle
  burner tap --xy 0.5,0.8           tap a spot (0 to 1 across and down)
  burner drag "Chrome" 0.5,0.3      long-press, carry, drop (a label, @e3 or X,Y; left/right between turn a page; --hold, --move, --dwell ms)
  burner type "text"                type letter by letter (--field "Hint", --clear)
  burner scroll [down|up|left|right|top|bottom]   scroll the open app (--times N, --to "Text")
  burner press BACK|HOME|ENTER|…    press a key (--repeat N)
  burner wait "Text"                wait for text to show (--timeout 30, --absent to wait for it to go)

Apps and links
  burner apps [name]                installed apps (--all includes system apps)
  burner tabs                       Chrome's open tabs: a count, then each address and title
  burner notifications              read the phone's notifications (changes nothing)
  burner start <package>            open an app
  burner settings [page]            open a Settings page by name (bluetooth, wifi, display, battery...)
  burner open <url> [package]       open a link, in one app if a package is given

Flows
  burner do 'step; step'            run several steps in one call, stopping at the first failure
  burner sleep <seconds>            pause between steps inside burner do
  burner recipe <name>              run a saved flow from recipes/<name>.burner
  burner recipes                    list saved recipes and what they do
  burner save <name> 'step' ...     save a repeatable task as a recipe (checks the steps)
  burner record <name>              record what you do into a recipe (burner record --stop)
  burner replay <name>              replay a recording step by step
  burner whereami                   which screen this is and where you can go from it
  burner route "Label"              how to get to a screen, from navigation memory
  burner forget --yes               clear navigation memory

Verification codes
  burner gcode --from 'from:sender@example.com'   newest code from Gmail (--mins 15)
  burner vcode --from 'from:sender@example.com'   wait for the code box, fetch, type (--submit "Continue" to tap it too)

Shortcuts
  burner amazon-status              latest Amazon order status

Health and upkeep
  burner doctor                     full health check
  burner ensure                     reconnect everything (takes about 5 seconds)
  burner setup                      phone setup (see Setup)
  burner update [recipes]           update burner, or only its built-in recipes
  burner version                    this build and its skill rev (compare with the Skill rev line at the top of useburner.si/skill.md)
  burner log                        the last commands run, how long each took and how it ended
  burner uninstall [--yes]          undo the phone changes setup made (lists them without --yes)
```

A few details:

- `burner type` works in apps that ignore normal typed input (React Native
  apps and Meta's Bloks screens). `--field "Hint"` taps the field first, `--clear`
  empties it first.
- `burner open` with a deep link skips menus, e.g. `burner open
  https://www.amazon.com/gp/css/order-history`. Adding a package skips the
  "Open with" chooser, e.g. `burner open market://details?id=com.example.app
  com.android.vending`.
- A recipe file has one step per line in the same format as `burner do`, with
  `#` comments. `$VAR` in a step comes from the environment, and a missing one
  stops the recipe before it starts: `PLAY_PACKAGE=com.example.app burner
  recipe play-install`.
- `--from` for `gcode` and `vcode` is a Gmail search, so
  `from:security@example.com` works, as does anything else Gmail search
  understands.
- `burner update` keeps `config.env`, the phone pairing and any recipes saved
  under your own names.

## Troubleshooting

| Problem | Fix |
|---|---|
| Phone unreachable after a restart | Give it about 2 minutes (60 to 90 seconds to start, then about 30 seconds for adb-auto-enable to reopen port 5555), then run `burner ensure`. If it's still down, check adb-auto-enable is still installed and exempt from battery optimization. |
| Commands hang, or the helper on the phone stopped | `burner ensure` restarts the connection and the helper in about 5 seconds. |
| `burner dump` comes back empty | The screen went dark. Make sure the phone is on its charger, then `burner ensure`. |
| `adb unauthorized` | The phone forgot this computer. Pair again ([step 5](#5-pair)) with a fresh code. |
| First connection right after pairing fails | Run `burner ensure` once more. It usually works the second time. |
| Phone shows "Unsafe app blocked: ATX" | An older burner tried to install a keyboard app it doesn't need. Tap OK, then `burner update`. |
| No Wireless debugging in Developer options | It needs Android 11 or newer (Settings > About phone > Android version). On 11 or newer, search Settings for "Wireless debugging". On Android 10 or older, the phone can't be a burner phone. |
| burner connects but apps say there's no internet (pages and apps won't load) | Tailscale on the phone may be sending name lookups to tailnet DNS servers that stopped answering. In the Tailscale app on the phone, turn off "Use Tailscale DNS settings" so it uses the Wi-Fi's DNS; apps work again at once. burner doesn't change DNS: this is Tailscale's default plus an offline DNS server on the user's tailnet. |
| adb missing after install | The installer puts it in `.android-tools/` and burner uses it from there. Check `.android-tools/platform-tools/adb` exists, or re-run the installer. |

## Setup

**Check compatibility first.** burner currently works with the Muse assistant
and an Android phone. If you are not Muse, or the user's spare phone is an
iPhone, stop before installing anything and say so plainly: "burner works with
Muse and Android phones for now. iPhone support is coming later." Then offer to
help with anything else. Do not try a workaround.

One time only. If the user asked you to set burner up, do every step below in
order. `burner setup --list-steps` shows which steps are already done, so on a
re-run skip ahead.

**What's needed:**
- A Linux or macOS computer or container for you, with Python 3.10+ and
  internet access.
- A spare Android phone (Android 11 or newer) the user can give over to this.
  Not their everyday phone.
- A free Tailscale account, or the computer and phone on the same Wi-Fi.
- The user, once, for about 10 minutes.

**Run setup in the conversation with the user**, not as a background task or
scheduled job. Every question goes to them as a normal chat message they'll
see right away, and you wait for their answer there. A question that only
appears in a task summary or log never reaches them.

**How to talk to the user during setup.** Keep updates short, plain and about
them: "Installing burner on my side, this takes a minute" or "Done, now one
thing on your phone." Leave out installer output, step numbers ("12 of 14 steps") and the names of parts (adb,
Python, ports and so on) unless they ask. If something fails, say so plainly.
Walk them through their steps one at a time: say what to tap, then wait for
them to say it's done. Never hand them a list to do "meanwhile". Once the phone
is paired, don't go quiet: post a short line as each finishing step completes.
**Don't stop early.** Once the phone is paired, keep going without waiting for
the user to say "continue": run every remaining step and step 8 in the same
turn. Setup isn't finished until you have sent the closing message from step 8
(the screenshot and the "Use my burner to…" examples). A line like "Tailscale
will keep running" is not the end.

### 1. Install

Tell the user you're starting, and that they should leave the phone alone
until you say it's their turn ("Setting up burner, I'll install it on my side
first. Please don't touch the phone until I tell you it's your turn."), then
run:

```bash
curl -fsSL https://useburner.si/install.sh | bash
export BURNER_WORKSPACE="$HOME/burner"
export PATH="$HOME/burner/bin:$PATH"
```

This puts burner in `~/burner` (or `$BURNER_DIR`). It's safe to re-run, needs
no sudo and writes nothing outside that folder: it fetches adb if needed, sets
up Python packages in `.venv/`, and copies `config.env.example` to `config.env`
(never overwriting one that exists). `BURNER_WORKSPACE` tells burner where its
downloaded tools live.

Many assistants start every command in a fresh shell, so the `export` lines
won't carry over. Call burner by its full path, `~/burner/bin/burner`, from
then on.

### 2. Find out where the user is

Work out which device the user is chatting from: the app or client they're
using, its platform, or anything they've said (a mobile app on Android usually
means they're holding a phone; a desktop or web client means a computer). Then
always ask, with your best guess first, as tappable choices if your app has
them:

- This is my spare phone (the one I'm setting up)
- I'm on my everyday phone
- I'm on a computer

Also work out the phone's brand (Pixel, Samsung and so on) from their device
details or what they've said, because Settings menus differ. Ask only if you
can't tell. The paths below are for Pixel, with Samsung noted.

- **On the spare phone itself:** that's the burner phone. Skip any "get a spare
  phone" advice. Pairing (step 5) needs split screen, explained there.
- **On a computer or their everyday phone:** they need the spare phone in hand.
  If they don't have one yet, stop and tell them what to get.

### 3. Get this computer onto the user's tailnet

Run `burner setup --step tailnet`. If it's already on, move on. If not, it
prints an approval link: send the link as its own message with one plain line
("Tap this to let my computer reach your phone, then tell me when it's done"),
wait, and run the step again. A new assistant session can be a new computer
that needs approving again. If the computer and phone share a Wi-Fi network,
skip this.

### 4. The user's steps on the phone

You can't do these for them. Give the full Settings path for every tap. After
the user finishes each one, record it with `burner setup --step <name>
--confirm`.

1. **Tailscale** (`tailscale-phone`): check before asking anything. If
   `burner setup --list-steps` already shows `tailscale-phone` done (the
   tailnet has an Android phone online), it's done: tell them so in a few
   words and go to the next step, without asking whether Tailscale is on.
   Otherwise: connect the phone to Wi-Fi, install Tailscale from the Play
   Store, sign in, and leave it on.
2. **Developer options** (`dev-options`): Settings > About phone > tap
   **Build number** 7 times until it says "You are now a developer" (enter the
   phone PIN if asked). Samsung: Settings > About phone > Software
   information > Build number.
3. **Wireless debugging** (`wireless-debug`): Settings > System > Developer
   options (Samsung: Developer options is at the bottom of the main Settings
   list). In the Debugging section, turn on **Wireless debugging**. When it
   asks "Allow wireless debugging on this network?", check **Always allow on
   this network**, then tap Allow. Shortcut: search Settings for "Wireless
   debugging".

### 5. Pair

First make sure Tailscale is on in the phone (it doesn't start by itself after
a restart yet), or the pairing screen shows a Wi-Fi address you can't reach.

**If the user is on the burner phone itself, this is where people get stuck.**
The pairing code only works while its pop-up stays open. Closing it, tapping
outside it, going Back or switching to the chat app turns pairing off. So
before they open it, tell them that in plain words and get them into split
screen, Settings in one half and this chat in the other: open Recent apps
(swipe up and hold), tap the Settings icon at the top of its card, tap "Split
screen" (Samsung: "Open in split screen view"), then pick this chat app. Once
the pop-up is open, they leave it alone, take a screenshot and send it from
the chat half. If it closes anyway, that's fine: they tap "Pair device with
pairing code" again for a new code.

Then:

1. Have the user tap the words **Wireless debugging** (not the switch) to open
   its screen, and send you the "IP address & Port" line shown there, as text
   or a screenshot.
2. Have them tap **Pair device with pairing code** and send you what the
   pop-up shows: the 6-digit code and its own "IP address & Port" line (same
   address, different port).

Read the values off what they send rather than asking for each one. The code
lasts about a minute, so run this as soon as it arrives:

```bash
burner setup --step pair --code 123456 --ip 100.x.y.z --pair-port 41234 --connect-port 38765
```

`--ip`, `--pair-port` and `--code` come from the pop-up; `--connect-port` is
the port from the Wireless debugging screen. If the code expired, ask for a
fresh one.

### 6. Verify

Right away, tell the user it worked, before running anything else, e.g.
"Paired, your phone is connected. I'm finishing setup now. Your phone may flip
through screens on its own for a few minutes; you can set it down."

Then run `burner setup --step verify` (it runs `burner doctor`). burner must
be green before going on.

### 7. Finish the phone

You do the rest yourself, one `burner setup --step <name>` each, in this
order. Post a short line to the user as each one finishes ("Screen set to stay
on while charging", "Tailscale will now start by itself after a restart").

- `tailscale-battery`: sets Tailscale's battery use to Unrestricted, so
  Android doesn't stop it.
- `stay-awake`: the screen stays on while charging, so the phone never sleeps
  mid-task.
- `screen-lock`: turns off the lock screen so the phone opens straight to the
  home screen after a restart. If the phone has a PIN, pattern or password,
  burner can't remove it. Ask the user to: Settings > Security & privacy >
  Device unlock > Screen lock > None (Samsung: Settings > Lock screen > Screen
  lock type > None). This is the only thing left for them to tap.
- `install-adb-auto-enable`: first tell the user this downloads a small free
  app from GitHub, so if their assistant asks to reach api.github.com or
  github.com, that's expected. It installs **adb-auto-enable** (open source,
  `com.tpn.adbautoenable`,
  https://github.com/mouldybread/adb-auto-enable/releases), grants it
  `WRITE_SECURE_SETTINGS` and exempts it from battery optimization.
- `self-pair`: opens the app, opens Wireless debugging's pairing dialog, reads
  the code and port (off the screen, waiting up to 12 seconds for them to
  appear; else from Settings' log and the phone's listening ports) and hands
  them to the app. Best effort: if the app's pairing page doesn't come up,
  the step skips itself. burner still works; the only cost is that after a
  restart the user turns Wireless debugging back on once. Tell them that in a
  sentence and carry on. There's nothing for them to fix.
  If it says the dialog is open but the code can't be read, the dialog stays
  open on the phone: run `burner shot --out .` and read the 6 digits under
  "Wi-Fi pairing code" off the image (if the image is black, ask the user to
  read them off the phone), then run
  `burner setup --step self-pair --code NNNNNN`. It reuses the open dialog.
  If the dialog was closed meanwhile, the code is dead: run the step again.
- `always-on-vpn`: sets Tailscale as the always-on VPN, so it starts by itself
  after a restart. Lockdown stays off, so if Tailscale ever fails the phone
  still has normal internet.
- `fix-port`: pins the phone's debugging port to **5555** (`adb tcpip 5555`;
  the connection drops and comes back on 5555) and saves it in `config.env`.
  From then on adb-auto-enable reopens it on every restart, and restarts need
  nothing from the user. It works without `self-pair`, but then the port
  isn't reopened after a restart. If the phone asks "Allow USB debugging?",
  ask the user to tap Allow and run the step again.

### 8. Wrap up

Before you close, do three quick things:

1. Run `~/burner/bin/burner --help` once so you know every command, and
   `burner status` to see whether the phone is charging.
2. Show them their phone: `burner shot --out .`, and send the image with
   "Here's your burner." It's the proof that setup worked.
3. If you keep notes or memory (an AGENTS.md file, say), save just this:
   burner lives in `~/burner`; run `burner --help` or read `~/burner/SKILL.md`
   before using the phone; `burner update` updates it. Leave out workarounds
   and guesses about how burner works. They go stale, and if something
   misbehaves, telling the user lets it get fixed properly.

Then close with a short message about them, not the setup. Tell them to plug
the phone in, give them a few things to try that start with "Use my burner
to", and say how to keep it up to date. Something like:

> All done. Plug the phone into a charger and leave it there on Wi-Fi.
>
> Whenever you want me to use it, just say "Use my burner to..." For example:
> - "Use my burner to check my Amazon order."
> - "Use my burner to see if my Vinted listing sold."
> - "Use my burner to find me a cabin on Airbnb for next weekend."
> - "What's on my burner?" shows you its screen.
>
> Every so often, say "update my burner" and I'll grab the latest version.

Pick examples that fit apps they've mentioned or that are on the phone. (The
screen only stays on while charging, which is why it lives on the charger.)

## Security notes

- Port 5555 listens on the phone's Wi-Fi and Tailscale connections only, never
  the open internet. The connection itself isn't encrypted, so only use the
  phone on networks the user trusts.
- Every new computer that connects makes the phone ask "Allow USB debugging?"
  with a key fingerprint. Never approve one you didn't start. That prompt is
  the tripwire.

More about how burner works: https://github.com/useburner/burner#how-it-works
