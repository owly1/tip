#!/usr/bin/env bash
# ./attack.sh on    clone answers every challenge (background, log in attack.log)
# ./attack.sh off   stop it (do this before humans test!)
# ./attack.sh       show whether it's on
cd "$(dirname "$0")"
VOICE="${TIP_CLONE_VOICE_ID:-$(grep -E '^TIP_CLONE_VOICE_ID=' .env | cut -d= -f2-)}"
running() { pgrep -f "attack.py --voice-id" >/dev/null; }
case "${1:-status}" in
  on)  running && { echo "attack already ON"; exit 0; }
       nohup .venv/bin/python -u attack.py --voice-id "$VOICE" --loop > attack.log 2>&1 &
       sleep 2; running && echo "attack ON: the clone will answer the next challenges (stay silent)" || { echo "failed:"; cat attack.log; } ;;
  off) pkill -f "attack.py --voice-id" && echo "attack OFF: humans can test now" || echo "attack was already off" ;;
  *)   running && echo "attack is ON" || echo "attack is OFF"; [ -f attack.log ] && tail -3 attack.log ;;
esac
