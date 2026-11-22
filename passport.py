"""Agent passports, signed with local HMAC (key from TIP_PASSPORT_KEY or contract.HOME). Owner: Jasper.

Signatures are fixed by contract.py (see the interfaces block there).
Token: b64url(passport JSON) + "." + b64url(HMAC-SHA256(key, first part)).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import asdict
from typing import Optional

import contract
from contract import Passport, PassportCheck, PassportRequest, normalize_to


# shortcut: key readable by anything with a shell as this user (an agent could mint passports),
# move it to the Keychain/Secure Enclave or Alter before real money
def _key() -> Optional[bytes]:
    env = os.environ.get("TIP_PASSPORT_KEY", "")
    if env:
        return env.encode("utf-8")
    path = contract.HOME / "passport.key"
    return path.read_bytes() if path.exists() else None


def _new_key() -> bytes:
    contract.HOME.mkdir(parents=True, exist_ok=True)
    fd = os.open(contract.HOME / "passport.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(secrets.token_bytes(32))
    return _key()


def _sign(key: bytes, payload: bytes) -> bytes:
    return base64.urlsafe_b64encode(hmac.new(key, payload, hashlib.sha256).digest())


def issue(req: PassportRequest) -> tuple[str, Passport]:
    p = Passport(id="pp_" + secrets.token_hex(5), agent=req.agent, owner=contract.OWNER,
                 to=[normalize_to(t) for t in req.to], max_cents=req.max_cents,
                 daily_cents=req.daily_cents, purpose=req.purpose,
                 expires_at=time.time() + req.expires_s)
    payload = base64.urlsafe_b64encode(json.dumps(asdict(p)).encode("utf-8"))
    return (payload + b"." + _sign(_key() or _new_key(), payload)).decode("ascii"), p


def check(token: str, to: str, amount_cents: int, spent_today_cents: int) -> PassportCheck:
    """Read-only: tipd calls it twice per payment. No key yet means nothing was ever issued."""
    try:
        payload, sig = token.strip().encode("ascii").split(b".")
    except (ValueError, AttributeError):
        return PassportCheck("invalid", "unreadable passport")
    key = _key()
    if key is None or not hmac.compare_digest(sig, _sign(key, payload)):
        return PassportCheck("invalid", "bad signature")
    try:
        p = Passport(**json.loads(base64.urlsafe_b64decode(payload)))
    except Exception:  # signed by us, so only a key mix-up gets here
        return PassportCheck("invalid", "unreadable passport")
    # same order and reasons as contract._fake_passport
    if p.expires_at < time.time():
        return PassportCheck("invalid", "passport expired", p)
    if normalize_to(to) not in p.to:
        return PassportCheck("out_of_scope", f"{to} is not on this passport", p)
    if amount_cents > p.max_cents:
        return PassportCheck("out_of_scope", f"over the {p.max_cents}c per-payment cap", p)
    if spent_today_cents + amount_cents > p.daily_cents:
        return PassportCheck("out_of_scope", f"over the {p.daily_cents}c daily cap", p)
    return PassportCheck("ok", "", p)
