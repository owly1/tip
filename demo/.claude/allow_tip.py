"""PreToolUse hook: let Claude Code run `tip` in auto mode.

tip enforces its own limits (signed passport caps, Voice ID for anything over them, "decline"),
so a plain `tip ...` command is safe to run without Claude Code's own payment check.
Only a single `tip` invocation is approved: no ; && || | ` $( ) > < or newlines, so nothing
can be chained onto it. Everything else falls through to Claude Code's normal permission flow.
"""
import json
import re
import shlex
import sys

try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
if data.get("tool_name") != "Bash":
    sys.exit(0)
cmd = (data.get("tool_input") or {}).get("command", "").strip()
if not cmd or re.search(r"[;&|`$<>\n\\]|\(|\)", cmd):
    sys.exit(0)
try:
    words = shlex.split(cmd)
except ValueError:
    sys.exit(0)
if not words or words[0] != "tip" or (len(words) > 1 and words[1] in ("passport", "enroll")):
    sys.exit(0)  # issuing passports / enrolling stays with the human
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "permissionDecisionReason": "tip enforces its own passport caps and Voice ID",
}}))
