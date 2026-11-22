"""eleven.py tests. No network: the ElevenLabs client is replaced with fakes."""
from __future__ import annotations

import io
import types
import wave

import pytest

import eleven
from elevenlabs.core.api_error import ApiError


# ---------------------------------------------------------------- phrase_match
@pytest.mark.parametrize("said, expected", [
    ("purple otter seventeen", "purple otter seventeen"),
    ("Purple otter, 17.", "purple otter seventeen"),
    ("PURPLE OTTER SEVENTEEN!", "purple otter seventeen"),
    ("uh, purple otter seventeen", "purple otter seventeen"),
    ("okay so purple otter seventeen I think", "purple otter seventeen"),
    ("purple, otter... seventeen?", "purple otter seventeen"),
    ("brave moose seventy-three", "brave moose seventy three"),
    ("brave moose 73", "brave moose seventy three"),
    ("Brave moose, seventy three.", "brave moose seventy three"),
    ("silver falcon 40", "silver falcon forty"),
    ("silver falcon fourty", "silver falcon forty"),        # typo within tolerance
    ("purpel otter seventeen", "purple otter seventeen"),    # typo within tolerance
    ("golden pelican ninety nine", "golden pelican 99"),     # digits on the expected side
    ("rusty heron 11", "rusty heron eleven"),
    ("HollowOtter70", "hollow otter seventy"),               # Scribe glues short phrases
    ("hollowotterseventy", "hollow otter seventy"),
    ("HollowOtter70.", "hollow otter seventy"),
    ("BraveMoose73", "brave moose seventy three"),
])
def test_phrase_passes(said, expected):
    assert eleven.phrase_match(said, expected)


@pytest.mark.parametrize("said, expected", [
    ("purple otter", "purple otter seventeen"),                # missing word
    ("purple otter seventy", "purple otter seventeen"),        # wrong number
    ("purple otter seven", "purple otter seventeen"),
    ("purple otter 70", "purple otter seventeen"),
    ("otter purple seventeen", "purple otter seventeen"),      # wrong order
    ("silver otter seventeen", "purple otter seventeen"),      # wrong adjective
    ("purple falcon seventeen", "purple otter seventeen"),     # wrong animal
    ("", "purple otter seventeen"),                            # silence
    ("purple otter seventeen", ""),                            # nothing expected
    ("brave moose seventy", "brave moose seventy three"),      # half a number
    ("brave moose seventy four", "brave moose seventy three"),
    ("frozen gecko sixty", "frozen gecko fifty"),
    ("frozen gecko thirteen", "frozen gecko thirty"),
    ("frozen gecko eighteen", "frozen gecko eighty"),
    ("frozen gecko nineteen", "frozen gecko ninety"),
    ("frozen gecko sixteen", "frozen gecko sixty"),
    ("HollowOtter7", "hollow otter seventy"),                # glued, wrong number
    ("hollowotterseven", "hollow otter seventy"),
    ("HollowOtter71", "hollow otter seventy"),
    ("SilverOtter70", "hollow otter seventy"),
])
def test_phrase_fails(said, expected):
    assert not eleven.phrase_match(said, expected)


def test_every_challenge_number_round_trips():
    """stepup's challenges use 11..99 as words. The digits Scribe may print must match."""
    for n in range(11, 100):
        words = eleven.num_words(n)
        assert eleven.phrase_match(f"lucky camel {n}", f"lucky camel {words}"), n
        assert eleven.phrase_match(f"lucky camel {words}", f"lucky camel {words}"), n


def test_no_two_challenge_numbers_confused():
    for a in range(11, 100):
        for b in range(11, 100):
            if a != b and eleven.num_words(b).startswith(eleven.num_words(a) + " "):
                continue  # "seventy" is a prefix of "seventy three": a missing word, tested above
            if a != b:
                assert not eleven.phrase_match(eleven.num_words(a), eleven.num_words(b)), (a, b)


def test_num_words():
    assert eleven.num_words(17) == "seventeen"
    assert eleven.num_words(73) == "seventy three"
    assert eleven.num_words(40) == "forty"
    assert eleven.num_words(100) == "one hundred"


# ---------------------------------------------------------------- audio format
def _wav_info(b: bytes):
    with wave.open(io.BytesIO(b)) as w:
        return w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()


def test_pcm_to_wav_is_contract_format():
    assert _wav_info(eleven.pcm_to_wav(b"\x00\x00" * 1600)) == (16000, 1, 2, 1600)


# ---------------------------------------------------------------- fake client
class FakeClient:
    def __init__(self, stt_reject=()):
        self.calls = []
        tts = types.SimpleNamespace(convert=self._tts)
        stt = types.SimpleNamespace(convert=self._stt)
        self.stt_reject = set(stt_reject)
        ivc = types.SimpleNamespace(create=self._ivc)
        captcha = types.SimpleNamespace(verify=self._verify)
        pvc = types.SimpleNamespace(verification=types.SimpleNamespace(captcha=captcha))
        voices = types.SimpleNamespace(ivc=ivc, pvc=pvc, search=self._search)
        self.text_to_speech, self.speech_to_text, self.voices = tts, stt, voices
        self.verify_result = "ok"

    def _tts(self, **kw):
        self.calls.append(("tts", kw))
        return iter([b"\x01\x00" * 100, b"\x02\x00" * 100])

    def _stt(self, **kw):
        self.calls.append(("stt", kw))
        if kw["model_id"] in self.stt_reject:
            raise ApiError(status_code=422, body={"detail": "invalid model_id"})
        return types.SimpleNamespace(text=" Purple otter 17. ")

    def _ivc(self, **kw):
        self.calls.append(("ivc", kw))
        return types.SimpleNamespace(voice_id="clone123")

    def _search(self, **kw):
        self.calls.append(("search", kw))
        return types.SimpleNamespace(voices=[types.SimpleNamespace(voice_id="premade1")])

    def _verify(self, **kw):
        self.calls.append(("verify", kw))
        if isinstance(self.verify_result, Exception):
            raise self.verify_result
        return types.SimpleNamespace(status=self.verify_result)


@pytest.fixture
def fake(monkeypatch):
    c = FakeClient()
    monkeypatch.setattr(eleven, "_client", c)
    monkeypatch.setattr(eleven, "_tts_voice", None)
    monkeypatch.delenv("TIP_TTS_VOICE", raising=False)
    monkeypatch.delenv("TIP_CAPTCHA", raising=False)
    return c


def test_speak_joins_chunks_into_wav(fake):
    wav = eleven.speak("hello")
    assert _wav_info(wav) == (16000, 1, 2, 200)
    kind, kw = fake.calls[-1]
    assert kind == "tts" and kw["model_id"] == "eleven_flash_v2_5" and kw["output_format"] == "pcm_16000"
    assert kw["voice_id"] == "premade1"


def test_speak_uses_env_voice_and_looks_up_once(fake, monkeypatch):
    monkeypatch.setenv("TIP_TTS_VOICE", "stock9")
    eleven.speak("a")
    eleven.speak("b")
    assert [kw["voice_id"] for k, kw in fake.calls if k == "tts"] == ["stock9", "stock9"]
    assert not [c for c in fake.calls if c[0] == "search"]


def test_speak_refuses_matthews_clone(fake, monkeypatch):
    monkeypatch.setenv("TIP_TTS_VOICE", "matt")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "matt")
    with pytest.raises(RuntimeError, match="Matthew"):
        eleven.speak("a")


def test_transcribe(fake):
    assert eleven.transcribe(b"wav") == "Purple otter 17."
    assert fake.calls[-1][1]["model_id"] == "scribe_v2"


def test_transcribe_falls_back_to_v1(fake):
    fake.stt_reject = {"scribe_v2"}
    assert eleven.transcribe(b"wav") == "Purple otter 17."
    assert [kw["model_id"] for k, kw in fake.calls if k == "stt"] == ["scribe_v2", "scribe_v1"]


def test_api_error_becomes_runtime_error(fake):
    fake.stt_reject = {"scribe_v2", "scribe_v1"}
    with pytest.raises(RuntimeError, match="speech-to-text"):
        eleven.transcribe(b"wav")


def test_no_key_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(eleven, "_client", None)
    monkeypatch.setattr(eleven, "_tts_voice", "x")
    with pytest.raises(RuntimeError, match="ELEVENLABS_API_KEY"):
        eleven.speak("a")


def test_captcha_off_by_default(fake):
    assert eleven.captcha_text() is None
    assert eleven.captcha_check(b"wav") is None
    assert not [c for c in fake.calls if c[0] == "verify"]


def test_captcha_check_when_on(fake, monkeypatch):
    monkeypatch.setenv("TIP_CAPTCHA", "1")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "pvc1")
    assert eleven.captcha_check(b"wav") is True
    fake.verify_result = ApiError(status_code=500, body="not verified")
    assert eleven.captcha_check(b"wav") is False
    fake.verify_result = ApiError(status_code=400, body="Time limit for voice verification exceeded.")
    assert eleven.captcha_check(b"wav") is None


def test_clone_and_synth(fake):
    assert eleven.clone("matthew-clone", [b"a", b"b"]) == "clone123"
    files = fake.calls[-1][1]["files"]
    assert len(files) == 2 and files[0][0].endswith(".wav")
    wav = eleven.synth("clone123", "purple otter seventeen")
    kind, kw = fake.calls[-1]
    assert kw["voice_id"] == "clone123" and kw["model_id"] == "eleven_multilingual_v2"
    assert _wav_info(wav)[0] == 16000
