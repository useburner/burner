<img src="https://useburner.si/logo.png" alt="burner logo" width="120">

# burner

Give your AI assistant a physical side phone.

**burner runs on your real phone, with real apps, real bluetooth and your real
home connection.**

It lets your AI assistant use a spare Android phone that sits in a drawer at
home, so it can use the apps you use.

## Use my burner to…

```
Use my burner to screen my Tinder matches and tell me who's worth a reply.
```

```
Use my burner to keep my Snapchat streaks alive while I'm on vacation.
```

```
Use my burner to spot underpriced flips on Vinted before anyone else sees them.
```

```
Use my burner to download the Airbnb app and find me a cabin for next weekend.
```

```
Use my burner to get last Friday's Uber Eats dinner ready to reorder.
```

```
Use my burner to check my Ring app and tell me who came to the door.
```

```
Use my burner to play my chill playlist on the living room speaker.
```

```
Use my burner to turn off the lamps I left on. I'm already at the airport.
```

## Get started

Paste this to your AI assistant:

```
Connect to https://useburner.si/skill.md
```

Your AI assistant reads the guide and walks you through the rest. Setup takes
about 10 minutes, once, and any spare Android phone works (iPhone soon). After
that, plug the phone in, leave it in a drawer, and never touch it again.

## Questions

**Why would I want this?**
Because a lot of what you'd want your AI assistant to do lives on a phone. Amazon
blocks AI assistants, Tinder only works as a phone app, and your lamp talks
bluetooth. burner gives it a real phone on your own Wi-Fi with bluetooth at home,
so it can use the apps you use, and apps and websites don't think you're in
another country.

**What do I need?**
A spare Android phone (iPhone soon), a free Tailscale account and 10 minutes.
That's it. For now, burner works with Muse as your AI assistant.

**Do I need to pay or sign up?**
No. burner is free and open source, and there's no burner account or burner
server. The one other app it uses, Tailscale, is free for personal use, so a
normal setup at home costs nothing.

**Does it work when I'm away from home?**
Yes. The phone stays plugged in at home, and your AI assistant can use it from
wherever you are.

**Is my stuff private?**
Yes. It runs on your own phone, on your own Wi-Fi at home, not in someone
else's data center. There's no burner account and no burner server. Only you
and your AI assistant can reach the phone.

**Will apps see it as coming from my home?**
Yes. The phone uses your own Wi-Fi, so apps see a regular phone at your house,
not a computer in some faraway data center. It also uses your location like any
phone would, so nearby stores, delivery, and local listings match where you
live.

**What if the phone restarts?**
It comes back on its own. You don't need to do anything.

**Will it buy things on its own?**
Never without your explicit okay. Every purchase, message, or post waits for
your yes.

**Give me the technical details.**
Start with [How it works](#how-it-works) below. The full setup steps and every
command are in [SKILL.md](SKILL.md).

**How do I get rid of it?**
Just tell your AI assistant to uninstall burner and forget it ever existed.

**I like this.**
Thanks. Let me know on X: [@tropoFarmer](https://x.com/tropoFarmer).

## Security notes

- It's your phone on your own Wi-Fi. Only computers signed in to your own
  Tailscale can reach it.
- Your phone's address lives in `config.env` on your computer and is kept out
  of GitHub. Pairing codes are never saved anywhere.
- Nothing is ever sent or communicated outside of your private Tailscale
  network.

## Gotchas

- After a restart, give the phone about 2 minutes to come back. If your AI
  assistant still can't reach it, `burner ensure` reconnects it.
- Tailscale has to turn itself on after a restart. Your AI assistant sets that
  up during setup, so you never have to open the app.
- Keep the phone on its charger with the screen allowed to stay on. If the
  screen goes dark, your AI assistant can't see what's on it.
- No SIM needed. The phone only uses Wi-Fi, and sign-in codes come by email.
- Nothing gets bought, sent or posted without your yes. Every time.
- The first connection right after pairing sometimes fails. Trying again fixes
  it.

## How it works

burner is a small command-line tool that your AI assistant runs. It
connects to the phone through Android's wireless debugging, over Tailscale (or
your home Wi-Fi when both are on the same network). From there it reads what's
on the screen, taps, types, moves app icons around the home screen, opens apps
and links, and takes screenshots, the same way you would. Sign-in codes come
from your email, so the phone doesn't need a SIM. Everything the phone does in
your apps happens on your own Wi-Fi.

The full setup steps and command reference are in [SKILL.md](SKILL.md).

## License

MIT, co-authors Muse, Claude & [@tropoFarmer](https://x.com/tropoFarmer). See [LICENSE](LICENSE).
