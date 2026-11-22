"""Live clone-attack demo: tipd's GET /challenge + attack.py's loop, in TIP_FAKE=1 mode."""
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

import attack  # noqa: E402
import contract  # noqa: E402
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


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def post_tip(url, to):
    body = json.dumps({"to": to, "amount_cents": 5000}).encode("utf-8")
    req = urllib.request.Request(url + "/tip", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


@pytest.fixture
def slow_audio(monkeypatch):
    """Fake play/record that take real time, so 'speaking' and 'listening' are observable."""
    audio = contract.FAKES["audio"]
    calls = []
    monkeypatch.setattr(audio, "record", lambda s: (time.sleep(0.4), contract._SILENT_WAV)[1])
    orig_play = audio.play
    monkeypatch.setattr(audio, "play", lambda wav: (calls.append(wav), time.sleep(0.3))[0])
    return calls, orig_play


def test_challenge_404_without_env(base, monkeypatch):
    monkeypatch.delenv("TIP_DEMO_ATTACK", raising=False)
    code, d = get(base + "/challenge")
    assert code == 404
    with pytest.raises(attack.NoEndpoint):
        attack.fetch_challenge(base)


def test_challenge_phases(base, monkeypatch, slow_audio):
    monkeypatch.setenv("TIP_DEMO_ATTACK", "1")
    code, idle = get(base + "/challenge")
    assert code == 200 and idle["active"] is False and idle["phase"] is None

    seen = []
    t = threading.Thread(target=post_tip, args=(base, "u" + uuid.uuid4().hex[:8]))
    t.start()
    end = time.time() + 10
    while t.is_alive() and time.time() < end:
        d = get(base + "/challenge")[1]
        if d["phase"] and (not seen or seen[-1][0] != d["phase"]):
            seen.append((d["phase"], d["phrase"], d["id"]))
        time.sleep(0.02)
    t.join()

    phases = [p for p, _, _ in seen]
    assert phases == ["speaking", "listening"], seen
    assert seen[0][1] and seen[0][1] == seen[1][1]  # same phrase throughout
    assert seen[0][2] == seen[1][2] > idle["id"]
    after = get(base + "/challenge")[1]
    assert after["active"] is False and after["phrase"] == ""


def test_attack_loop_synths_on_speaking_and_plays_once_on_listening(monkeypatch):
    monkeypatch.setenv("TIP_FAKE", "1")
    script = [
        {"active": False, "phase": None, "phrase": "", "id": 0},
        {"active": True, "phase": "speaking", "phrase": "purple otter seventeen", "id": 1},
        {"active": True, "phase": "speaking", "phrase": "purple otter seventeen", "id": 1},
        {"active": True, "phase": "speaking", "phrase": "purple otter seventeen", "id": 1},
        {"active": True, "phase": "listening", "phrase": "purple otter seventeen", "id": 1},
        {"active": True, "phase": "listening", "phrase": "purple otter seventeen", "id": 1},
        {"active": False, "phase": None, "phrase": "", "id": 1},
    ]
    i = {"n": 0}

    def fetch(url):
        d = script[min(i["n"], len(script) - 1)]
        i["n"] += 1
        return d

    synth_calls, play_calls = [], []
    monkeypatch.setattr(contract.FAKES["audio"], "play", lambda wav: play_calls.append(wav))

    def make_wav(phrase):
        synth_calls.append(phrase)
        return b"CLONE:" + phrase.encode()

    played = attack.run("http://x", make_wav, loop=True, fetch=fetch, sleep=lambda s: None,
                        max_polls=20)
    assert synth_calls == ["purple otter seventeen"]
    assert play_calls == [b"CLONE:purple otter seventeen"]
    assert played == 1


def test_attack_end_to_end_against_tipd(base, monkeypatch, slow_audio):
    """attack.run against a live tipd: plays the clone during the recording window."""
    monkeypatch.setenv("TIP_DEMO_ATTACK", "1")
    calls, _ = slow_audio
    synth = []

    def make_wav(phrase):
        synth.append(phrase)
        return b"CLONE"

    result = {}
    atk = threading.Thread(target=lambda: result.setdefault("n", attack.run(base, make_wav, max_polls=200)))
    atk.start()
    time.sleep(0.15)
    res = post_tip(base, "u" + uuid.uuid4().hex[:8])
    atk.join(timeout=15)
    assert result.get("n") == 1
    assert len(synth) == 1 and synth[0] == res["voice"]["phrase"]
    assert b"CLONE" in calls  # the clone was played (alongside tipd's own prompt audio)
