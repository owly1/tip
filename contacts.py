"""Who to notify when you tip someone: name -> ntfy channel, in contract.HOME/contacts.json. Owner: Jasper.

The recipient installs the free ntfy app (iOS, Android, web) and subscribes to their channel name.
After each successful payment tip posts one message there, so their phone shows it.
Stdlib only, so the CLI can use it without loading Stripe.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import urllib.request
from typing import Optional

import contract

# shortcut: public ntfy.sh channels are private only because the name is unguessable;
# self-host ntfy or use ntfy access tokens before real users
_CHANNEL = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _path():
    return contract.HOME / "contacts.json"


def _load() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:  # never overwrite a damaged file
        raise ValueError(f"can't read {p} ({e}); fix or delete it") from e
    return d if isinstance(d, dict) else {}


def link(name: str, channel: Optional[str] = None) -> str:
    """Save who to notify for `name`; makes up an unguessable channel if none is given. Returns the channel."""
    who = contract.normalize_to(name)
    if not who:
        raise ValueError("who are you linking? e.g. tip link sam")
    if channel is None:
        channel = f"tip-{re.sub(r'[^A-Za-z0-9_-]', '-', who)[:24]}-{secrets.token_hex(6)}"
    channel = channel.strip()
    if not _CHANNEL.match(channel):
        raise ValueError(f"not an ntfy channel name (letters, digits, - and _, up to 64): {channel!r}")
    d = _load()
    d[who] = {"ntfy": channel}
    contract.HOME.mkdir(parents=True, exist_ok=True)
    tmp = _path().with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, _path())
    return channel


def channel_for(name: str) -> Optional[str]:
    try:
        entry = _load().get(contract.normalize_to(name))
    except (ValueError, OSError):
        return None
    return entry.get("ntfy") if isinstance(entry, dict) else None


def notify(to: str, title: str, message: str, click: Optional[str] = None) -> bool:
    """Push one message to `to`'s phone. Never raises: the payment has already happened."""
    channel = channel_for(to)
    if not channel:
        return False
    body = {"topic": channel, "title": title, "message": message, "tags": ["moneybag"]}
    if click:
        body["click"] = click
    server = os.environ.get("TIP_NTFY_URL", "https://ntfy.sh").rstrip("/")
    req = urllib.request.Request(server + "/", data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3):
            return True
    except Exception as e:  # notification is best effort
        print(f"[tip] couldn't notify {to}: {e}", file=sys.stderr, flush=True)
        return False
