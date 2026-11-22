"""Real HMAC passports: valid, forged, expired, out of scope."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contract  # noqa: E402
import passport  # noqa: E402
from contract import PassportRequest  # noqa: E402


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(contract, "HOME", tmp_path)
    monkeypatch.delenv("TIP_PASSPORT_KEY", raising=False)
    return tmp_path


def issue(**kw):
    req = dict(agent="claude-code", to=["@Sam"], max_cents=2000, daily_cents=5000, purpose="lunch")
    req.update(kw)
    return passport.issue(PassportRequest(**req))


def test_valid_token_ok_and_key_file_is_private(home):
    token, p = issue()
    c = passport.check(token, "sam", 1500, 0)
    assert c.result == "ok" and c.passport == p and p.to == ["sam"]
    if os.name != "nt":  # Windows has no Unix file modes
        assert stat.S_IMODE(os.stat(home / "passport.key").st_mode) == 0o600


def test_flipped_byte_is_invalid():
    token, _ = issue()
    payload, sig = token.split(".")
    i = len(payload) // 2
    forged = payload[:i] + ("A" if payload[i] != "A" else "B") + payload[i + 1:] + "." + sig
    assert passport.check(forged, "sam", 100, 0).result == "invalid"


def test_other_key_is_invalid(monkeypatch):
    monkeypatch.setenv("TIP_PASSPORT_KEY", "key-one")
    token, _ = issue()
    monkeypatch.setenv("TIP_PASSPORT_KEY", "key-two")
    c = passport.check(token, "sam", 100, 0)
    assert c.result == "invalid" and c.reason == "bad signature"


def test_check_before_any_issue_is_invalid_and_writes_nothing(home):
    assert passport.check("e30=.AAAA", "sam", 100, 0).reason == "bad signature"
    assert not (home / "passport.key").exists()


def test_unsigned_fake_token_is_invalid():
    token, _ = contract._fake_passport().issue(PassportRequest("claude-code", ["sam"], 2000, 5000, "x"))
    assert passport.check(token, "sam", 100, 0).result == "invalid"


@pytest.mark.parametrize("junk", ["", "abc", "a.b.c", "ü.ü", ".", "e30=.AAAA"])
def test_junk_is_invalid_not_an_error(junk):
    assert passport.check(junk, "sam", 100, 0).result == "invalid"


def test_expired_is_invalid():
    token, _ = issue(expires_s=-1)
    c = passport.check(token, "sam", 100, 0)
    assert c.result == "invalid" and c.reason == "passport expired"


@pytest.mark.parametrize("to,amount,spent,reason", [
    ("alex", 100, 0, "alex is not on this passport"),
    ("sam", 2001, 0, "over the 2000c per-payment cap"),
    ("sam", 1500, 4000, "over the 5000c daily cap"),
])
def test_out_of_scope(to, amount, spent, reason):
    token, _ = issue()
    c = passport.check(token, to, amount, spent)
    assert (c.result, c.reason) == ("out_of_scope", reason) and c.passport is not None


def test_exactly_at_the_caps_is_ok():
    token, _ = issue()
    assert passport.check(token, "@SAM", 2000, 3000).result == "ok"
