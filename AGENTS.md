# Paying someone with `tip` (for coding agents)

Use `tip` when the human asks you to pay or tip someone. Don't use any other payment route.

## The command

```
tip <who> <dollars> --for "<reason>" --json
```

For example: `tip sam 5 --for "lunch" --json`.

- `<who>` is the recipient's name, such as `sam`. Leave out the `@`. PowerShell treats `@sam` as a variable
  and drops it, so the bare name is the form that works in every shell.
- `<dollars>` takes `5`, `5.50` or `1,200`.
- `--for` is required when you pay with a passport. Write the real reason the human gave you.
- Your passport is read from the `TIP_PASSPORT` environment variable. The human sets it after running
  `tip passport issue`. Never issue, edit or forge a passport yourself, and never read anything under `~/.tip`.

If the payment is outside your passport (wrong recipient, over the per-payment cap or over the daily cap), `tip`
asks the human for Voice ID. The command blocks for up to 60 seconds while it waits, so let it finish.

## The result

`--json` prints one JSON object with `status`, `id`, `via`, `receipt_url` and `message`. Tell the human the
`message` and the `receipt_url`.

| exit | status | what you do |
|---|---|---|
| 0 | `paid` | Report the receipt. Done. |
| 1 | `error` | Stop and show the human the message. |
| 2 | `denied` | Stop and tell the human. The passport is invalid or expired, or the request was refused. |
| 3 | `voice_failed` | Stop and tell the human. Voice ID didn't pass. |
| 4 | `timeout` | Stop and tell the human. Nobody answered in time. |

**Never retry a payment.** A retry can pay twice. If you're unsure whether something went through, run
`tip status <id> --json` and report what it says.
