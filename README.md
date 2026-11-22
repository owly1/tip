# tip

Pay someone from your terminal, approved by your voice. Coding agents can pay too, but only within limits you set.

```
$ tip sam 5 --for coffee
Verification required: speak golden camel twenty one
✓ transfer approved  $5.00 → @sam
  receipt  view receipt ↗
  tip id   tp_5eff57c5a9
```

## Install

Installs the `tip` command so it works in any terminal:

```
uv tool install --python 3.13 git+https://github.com/owly1/tip
```

or with pipx (Python 3.10 to 3.13):

```
pipx install git+https://github.com/owly1/tip
```

You need a microphone and speakers, a [Stripe](https://dashboard.stripe.com/test/apikeys) **test** secret key (`sk_test_…`) and an [ElevenLabs](https://elevenlabs.io/app/settings/api-keys) API key.

## Set up

```
tip enroll
```

It asks for your two keys with hidden typing, checks them, and saves them to `~/.tip/.env`, which only you can read. Then it records your voice: read each phrase after the beep. If your computer asks for microphone access, allow it.

## Pay someone

```
tip sam 5 --for coffee
```

Say the phrase after the beep.

- Payments of $50 or more ask for two phrases.
- Say "decline" to refuse a payment.
- `tip --dev sam 5` shows every check: what it heard, your voice score and the threshold.

## Let an agent pay

Give the agent a passport. Issuing one needs your voice:

```
tip passport claude-code sam alex max-20
export TIP_PASSPORT="…"      # the line it prints; in PowerShell: $env:TIP_PASSPORT="…"
```

The agent runs the same command, for example `tip sam 5 --for "lunch" --json`.

| the payment | what happens |
|---|---|
| to someone on the passport, within $20 a day | paid, no voice needed |
| over the limit, or to anyone else | asks for your voice |
| forged or expired passport | denied |

[AGENTS.md](AGENTS.md) tells agents how to call `tip` and read its exit codes. For Claude Code, allow the command in `.claude/settings.json` with `{"permissions": {"allow": ["Bash(tip *)"]}}`.

## Notify the person you pay

```
tip link sam
```

Sam installs the free [ntfy](https://ntfy.sh) app and subscribes to the channel name this prints. Every tip to Sam then shows up on his phone.

## Check a payment

```
tip verify tp_5eff57c5a9     # is the ledger's hash chain intact up to this payment?
tip status tp_5eff57c5a9
```

Every payment is written to a hash-chained ledger in `~/.tip`. If someone edits a line, `verify` fails from that line on.

## Good to know

- **Test money only.** tip refuses Stripe live keys. Payments go to your own Stripe sandbox; paying out to the recipient's account is the next step.
- **The voice check stops other people, not every voice clone.** In our tests a clone came close, so payments of $50 or more need two phrases, and agents are capped by their passports.
- **No server, nothing left running.** `tip` does everything in your terminal and exits.
- Tested on macOS. Windows support is written but untested.
