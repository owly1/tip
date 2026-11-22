#!/usr/bin/env bash
# Demo step 1: issue Claude Code a passport (needs Matthew's Voice ID), then open Claude Code with it.
# Run from anywhere: ./demo/go.sh   (tipd must already be running in another terminal)
set -euo pipefail
cd "$(dirname "$0")"

MAX="${TIP_DEMO_MAX:-20}"; DAILY="${TIP_DEMO_DAILY:-50}"
echo "issuing a passport for claude-code: @weatherapi, @tinycache, up to \$$MAX each, \$$DAILY/day"
OUT="$(tip passport issue --agent claude-code --to @weatherapi,@tinycache \
        --max "$MAX" --daily "$DAILY" --for "tip the maintainers of this project's dependencies" \
        --expires 2h --json)" || { echo "$OUT"; echo "passport not issued"; exit 1; }
export TIP_PASSPORT="$(python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' <<<"$OUT")"
echo "passport issued. starting Claude Code (manual mode: auto mode blocks payments even with the allow rule)"
echo 'try: "tip the maintainer of weatherapi $5 as thanks", then "...$200"'
exec claude --permission-mode auto
