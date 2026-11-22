"""tipd + Voice ID routing, all in TIP_FAKE=1 mode."""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contract  # noqa: E402
import stepup  # noqa: E402
import tipd  # noqa: E402


@pytest.fixture
def base(monkeypatch):
    monkeypatch.setenv("TIP_FAKE", "1")
    monkeypatch.delenv("TIP_FAKE_VOICE", raising=False)
    monkeypatch.delenv("TIP_FAKE_CAPTCHA", raising=False)
    srv = tipd.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def call(base, method, path, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def who():
    return "u" + uuid.uuid4().hex[:8]


def tip(base, **kw):
    code, d = call(base, "POST", "/tip", kw)
    assert code == 200
    return contract.from_dict(contract.TipResult, d)


def passport(base, to, max_cents=2000, daily_cents=5000):
    code, d = call(base, "POST", "/passport", {"agent": "claude-code", "to": to, "max_cents": max_cents,
                                               "daily_cents": daily_cents, "purpose": "pay for APIs"})
    assert code == 200 and d["status"] == "issued", d
    return d["token"], d["passport"]


@pytest.fixture
def no_voice(monkeypatch):
    calls = []
    real = stepup.voice_id

    def spy(prompt):
        calls.append(prompt)
        return real(prompt)
    monkeypatch.setattr(stepup, "voice_id", spy)
    return calls


def test_human_paid_via_voice(base):
    r = tip(base, to="@" + who(), amount_cents=5000)
    assert r.status == "paid" and r.via == "voice" and r.exit_code == 0
    assert r.receipt_url and r.voice.passed and r.voice.score is not None


def test_wrong_voice(base, monkeypatch):
    monkeypatch.setenv("TIP_FAKE_VOICE", "wrong_voice")
    r = tip(base, to=who(), amount_cents=5000)
    assert r.status == "voice_failed" and r.exit_code == contract.Exit.VOICE_FAILED
    assert r.voice.words_ok and not r.voice.voice_ok


def test_wrong_words(base, monkeypatch):
    monkeypatch.setenv("TIP_FAKE_VOICE", "wrong_words")
    r = tip(base, to=who(), amount_cents=5000)
    assert r.status == "voice_failed" and not r.voice.words_ok


def test_passport_in_scope_skips_voice(base, no_voice):
    to = who()
    token, _ = passport(base, [to])
    no_voice.clear()
    r = tip(base, to=to, amount_cents=500, reason="API credits", agent="claude-code", passport=token)
    assert r.status == "paid" and r.via == "passport" and r.voice is None
    assert no_voice == []


def test_passport_over_cap_goes_to_voice(base, no_voice):
    to = who()
    token, _ = passport(base, [to], max_cents=2000)
    no_voice.clear()
    r = tip(base, to=to, amount_cents=20000, reason="big one", passport=token)
    assert r.status == "paid" and r.via == "voice"
    assert r.passport.result == "out_of_scope"
    assert len(no_voice) == 1 and "outside its passport" in no_voice[0]


def test_passport_unknown_recipient_goes_to_voice(base):
    token, _ = passport(base, [who()])
    r = tip(base, to=who(), amount_cents=100, reason="x", passport=token)
    assert r.via == "voice" and r.passport.result == "out_of_scope"


def test_junk_passport_denied(base):
    r = tip(base, to=who(), amount_cents=100, reason="x", passport="not-a-passport")
    assert r.status == "denied" and r.exit_code == contract.Exit.DENIED


def test_passport_without_reason_denied(base):
    to = who()
    token, _ = passport(base, [to])
    r = tip(base, to=to, amount_cents=100, passport=token)
    assert r.status == "denied" and "--for" in r.message


def test_amount_zero_denied(base):
    assert tip(base, to=who(), amount_cents=0).status == "denied"
    assert tip(base, to="@", amount_cents=100).status == "denied"


def test_daily_cap_counts_earlier_payments(base):
    to = who()
    token, _ = passport(base, [to], max_cents=2000, daily_cents=3000)
    assert tip(base, to=to, amount_cents=2000, reason="a", passport=token).via == "passport"
    r = tip(base, to=to, amount_cents=1500, reason="b", passport=token)
    assert r.via == "voice" and "daily" in r.passport.reason


def test_timeout_charges_nothing(base, monkeypatch):
    monkeypatch.setattr(contract, "TIMEOUT_S", 0.5)
    monkeypatch.setattr(contract.FAKES["audio"], "record", lambda s: time.sleep(1.5) or b"")
    to = who()
    r = tip(base, to=to, amount_cents=5000)
    assert r.status == "timeout" and r.exit_code == contract.Exit.TIMEOUT
    time.sleep(1.5)  # let the worker finish late
    ledger = contract.FAKES["ledger"]
    assert ledger.get(r.id) is None
    code, d = call(base, "GET", f"/tip/{r.id}")
    assert code == 200 and d["status"] == "timeout"


def test_get_tip_and_verify(base):
    r = tip(base, to=who(), amount_cents=700)
    code, d = call(base, "GET", f"/tip/{r.id}")
    assert code == 200 and d["status"] == "paid" and d["id"] == r.id
    code, d = call(base, "GET", f"/verify/{r.id}")
    assert code == 200 and d["ok"] and d["entry"]["amount_cents"] == 700
    assert d["entry"]["approver"] == contract.OWNER
    assert call(base, "GET", "/tip/tp_nope")[0] == 404
    code, d = call(base, "GET", "/verify/tp_nope")
    assert code == 200 and not d["ok"] and d["entry"] is None


def test_health_and_errors(base):
    code, d = call(base, "GET", "/health")
    assert code == 200 and d["ok"] and d["fake"] == sorted(contract.MODULES)
    assert call(base, "GET", "/nope")[0] == 404
    assert call(base, "POST", "/tip", raw=b"{not json")[0] == 400


def test_passport_issue_fails_on_wrong_voice(base, monkeypatch):
    monkeypatch.setenv("TIP_FAKE_VOICE", "wrong_voice")
    code, d = call(base, "POST", "/passport", {"agent": "codex", "to": ["@x"], "max_cents": 1,
                                               "daily_cents": 1, "purpose": "p"})
    assert code == 200 and d["status"] == "voice_failed" and "token" not in d


def test_captcha_path(base, monkeypatch):
    monkeypatch.setenv("TIP_FAKE_CAPTCHA", "1")
    r = tip(base, to=who(), amount_cents=5000)
    assert r.status == "paid" and r.voice.method == "captcha"
    assert r.voice.phrase == "fake captcha words" and r.voice.score is not None
    monkeypatch.setenv("TIP_FAKE_VOICE", "wrong_voice")
    assert tip(base, to=who(), amount_cents=5000).status == "voice_failed"


def test_enroll(base):
    code, d = call(base, "POST", "/enroll", {"samples": 2})
    assert code == 200 and d["status"] == "enrolled"


def test_server_survives_module_crash(base, monkeypatch):
    def boom(*a):
        raise RuntimeError("stripe down")
    monkeypatch.setattr(contract.FAKES["pay"], "charge", boom)
    r = tip(base, to=who(), amount_cents=100)
    assert r.status == "error" and "stripe down" in r.message
    assert call(base, "GET", "/health")[0] == 200


def test_random_phrase():
    p = stepup.random_phrase().split()
    assert len(p) >= 3 and p[0] in stepup.ADJECTIVES and p[1] in stepup.ANIMALS
