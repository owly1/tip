import json, threading, urllib.request
import pytest
import contract, stepup, tipd


@pytest.fixture
def srv(monkeypatch):
    monkeypatch.setenv("TIP_FAKE", "1")
    monkeypatch.delenv("TIP_FAKE_VOICE", raising=False)
    s = tipd.make_server(port=0)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}"
    s.shutdown()


def _post(url, path, body):
    req = urllib.request.Request(url + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def test_decline_denies_without_charging(srv, monkeypatch):
    fe = contract.FAKES["eleven"]
    monkeypatch.setattr(fe, "transcribe", lambda wav: "Decline.")
    monkeypatch.setattr(fe, "phrase_match", lambda said, exp: False)
    charged = []
    monkeypatch.setattr(contract.FAKES["pay"], "charge", lambda *a, **k: charged.append(a))
    res = _post(srv, "/tip", {"to": "declinee", "amount_cents": 20000})
    assert res["status"] == "denied" and res["voice"]["declined"] and "declined" in res["message"]
    assert not charged
    assert contract.TipResult(status=res["status"]).exit_code == contract.Exit.DENIED


def test_decline_in_strict_round_stops(monkeypatch):
    monkeypatch.setenv("TIP_FAKE", "1")
    monkeypatch.setattr(stepup, "STRICT_OVER_CENTS", 5000)
    fe = contract.FAKES["eleven"]
    monkeypatch.setattr(fe, "transcribe", lambda wav: "no")
    monkeypatch.setattr(fe, "phrase_match", lambda said, exp: False)
    vc = stepup.voice_id("x", 20000)
    assert vc.declined and not vc.passed


def test_saying_the_phrase_is_not_a_decline():
    assert not stepup.is_decline("purple otter seventeen")
    assert stepup.is_decline("Decline.") and stepup.is_decline("no") and not stepup.is_decline("no otter")
