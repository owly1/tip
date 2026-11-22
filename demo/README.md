# demo/: Claude Code pays with tip

A fake project (`forecast-cli`) whose dependencies have maintainers you can tip.

```
terminal 1:  tipd                      # TIP_FAKE=... while parts are still fake
terminal 2:  ./demo/go.sh              # Voice ID → passport → Claude Code opens here
```
Then in Claude Code:
1. `tip the maintainer of weatherapi $5 as thanks` → pays instantly under the passport
2. `tip the maintainer of weatherapi $200 as thanks` → over the $20 cap → laptop asks for Matthew's voice

Why it's set up this way:
- `.claude/settings.json` allows exactly one command, `Bash(tip *)`. Everything else still asks.
- Claude Code must run in **manual** mode: auto mode blocks `tip` (it moves money) even with the allow rule. Tested on 2.1.330.
- `CLAUDE.md` pulls in `AGENTS.md` (a link to the repo one), so the agent knows the exit codes and never retries.
- The passport lives only in this shell's `TIP_PASSPORT`; it expires in 2 hours.
