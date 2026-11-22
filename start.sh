#!/usr/bin/env bash
# One window, one command: ./start.sh   → checks, then runs tipd here (this window = the demo screen).
# Ctrl+C here stops tipd. Everything else (tip, attack) goes in another window.
cd "$(dirname "$0")"
if lsof -tiTCP:7777 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "an old tipd is still running on :7777, stopping it"; kill $(lsof -tiTCP:7777 -sTCP:LISTEN); sleep 0.5
fi
.venv/bin/python cli.py doctor --no-mic | grep -E "✗|BAD" && { echo; echo "fix the ✗ above first"; exit 1; }
echo "checks ok. starting tipd..."; echo
export TIP_DEMO_ATTACK=1
exec .venv/bin/python tipd.py
