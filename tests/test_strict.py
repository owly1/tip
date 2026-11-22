import json, threading, urllib.request
import pytest
import contract, stepup


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("TIP_FAKE", "1")
    monkeypatch.delenv("TIP_FAKE_VOICE", raising=False)
    yield contract.FAKES


def _scores(monkeypatch, fake, seq):
    it = iter(seq)
    monkeypatch.setattr(fake["voice"], "score", lambda wav: next(it))


def test_small_payment_one_round(monkeypatch, fake):
    _scores(monkeypatch, fake, [0.52])
    vc = stepup.voice_id("x", 100)
    assert vc.passed and vc.threshold == fake["voice"].THRESHOLD


def test_big_payment_needs_average(monkeypatch, fake):
    _scores(monkeypatch, fake, [0.56, 0.46])          # the clone's best + typical
    vc = stepup.voice_id("x", 20000)
    assert not vc.passed and abs(vc.score - 0.51) < 1e-9 and vc.threshold == stepup.STRICT_THRESHOLD


def test_big_payment_owner_passes(monkeypatch, fake):
    _scores(monkeypatch, fake, [0.54, 0.68])          # Matthew's worst + typical
    vc = stepup.voice_id("x", 20000)
    assert vc.passed and " / " in vc.phrase


def test_big_payment_wrong_words_stops_early(monkeypatch, fake):
    monkeypatch.setenv("TIP_FAKE_VOICE", "wrong_words")
    calls = []
    monkeypatch.setattr(fake["voice"], "score", lambda wav: calls.append(1) or 0.9)
    vc = stepup.voice_id("x", 20000)
    assert not vc.passed and not vc.words_ok and len(calls) == 1
