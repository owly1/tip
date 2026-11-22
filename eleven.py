"""ElevenLabs: TTS, Scribe, PVC captcha, clone attack. Owner: Alex.

Signatures are fixed by contract.py (see the interfaces block there).
Until this is real, run with TIP_FAKE=1 (or TIP_FAKE=eleven) to use the fake.

Env (.env):
  ELEVENLABS_API_KEY    required
  TIP_TTS_VOICE         stock voice that speaks the prompts (default: first premade voice
                        in the account). Never Matthew's clone.
  TIP_CAPTCHA=1         turn on the PVC captcha path (off by default: tested no-go at 12:00)
  ELEVENLABS_VOICE_ID   Matthew's PVC voice, only used when TIP_CAPTCHA=1

Smoke check:  python eleven.py   (speaks a sentence -> out.wav -> transcribes it back)
"""
from __future__ import annotations

import difflib
import io
import os
import re
import sys
import wave
from pathlib import Path
from typing import Optional

from contract import HOME, LedgerEntry, Passport, PassportCheck, PassportRequest, Payment  # noqa: F401

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from elevenlabs import VoiceSettings
from elevenlabs.client import ElevenLabs
from elevenlabs.core.api_error import ApiError

TTS_MODEL = "eleven_flash_v2_5"          # prompts: lowest latency
CLONE_MODEL = "eleven_multilingual_v2"   # attacker: best likeness
STT_MODELS = ("scribe_v2", "scribe_v1")  # fall back to v1 if v2 is rejected
SAMPLE_RATE = 16000
TIMEOUT_S = 20                           # per call; tipd's whole-payment cap is 60
_OPTS = {"timeout_in_seconds": TIMEOUT_S, "max_retries": 1}

# One client for the whole process (keeps the HTTP connection warm).
_client: Optional[ElevenLabs] = (ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"], timeout=TIMEOUT_S)
                                 if os.environ.get("ELEVENLABS_API_KEY") else None)
_tts_voice: Optional[str] = None
last_debug = ""  # raw status of the last captcha call, for logs


def _api() -> ElevenLabs:
    if _client is None:
        raise RuntimeError("ELEVENLABS_API_KEY is not set (.env)")
    return _client


def _err(what: str, e: Exception) -> RuntimeError:
    if isinstance(e, ApiError):
        return RuntimeError(f"ElevenLabs {what} failed ({e.status_code}): {str(e.body)[:200]}")
    return RuntimeError(f"ElevenLabs {what} failed: {e}")


def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    """Raw 16-bit mono PCM -> WAV bytes (the contract's audio format)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _join(chunks) -> bytes:
    return chunks if isinstance(chunks, (bytes, bytearray)) else b"".join(chunks)


def _prompt_voice() -> str:
    """TIP_TTS_VOICE, else the first premade voice in the account (looked up once)."""
    global _tts_voice
    if _tts_voice:
        return _tts_voice
    v = os.environ.get("TIP_TTS_VOICE", "").strip()
    if not v:
        try:
            found = _api().voices.search(category="premade", page_size=1, request_options=_OPTS).voices
        except Exception as e:
            raise _err("voice lookup", e) from e
        if not found:
            raise RuntimeError("no premade voice in the account: set TIP_TTS_VOICE")
        v = found[0].voice_id
    matthew = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
    if matthew and v == matthew:
        raise RuntimeError("TIP_TTS_VOICE must not be Matthew's clone")
    _tts_voice = v
    return v


def _tts(voice_id: str, text: str, model_id: str, settings: Optional[VoiceSettings] = None) -> bytes:
    try:
        chunks = _api().text_to_speech.convert(voice_id=voice_id, text=text, model_id=model_id,
                                               output_format="pcm_16000", voice_settings=settings,
                                               request_options=_OPTS)
        return pcm_to_wav(_join(chunks))
    except RuntimeError:
        raise
    except Exception as e:
        raise _err("text-to-speech", e) from e


# ---------------------------------------------------------------- contract functions
def speak(text: str) -> bytes:
    return _tts(_prompt_voice(), text, TTS_MODEL)


def transcribe(wav: bytes) -> str:
    last: Optional[Exception] = None
    for model in STT_MODELS:
        try:
            res = _api().speech_to_text.convert(file=("clip.wav", io.BytesIO(wav), "audio/wav"),
                                                model_id=model, request_options=_OPTS)
            return (getattr(res, "text", "") or "").strip()
        except ApiError as e:
            last = e
            if e.status_code in (400, 404, 422) and "model" in str(e.body).lower():
                continue  # this model id was rejected, try the next
            break
        except Exception as e:
            last = e
            break
    raise _err("speech-to-text", last)


_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
         "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
         "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_FUZZ = 0.8  # per-word difflib ratio


def num_words(n: int) -> str:
    """17 -> 'seventeen', 73 -> 'seventy three'. Over 999: digit by digit."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _TENS[t] + (f" {_ONES[o]}" if o else "")
    if n < 1000:
        h, rest = divmod(n, 100)
        return f"{_ONES[h]} hundred" + (f" {num_words(rest)}" if rest else "")
    return " ".join(_ONES[int(d)] for d in str(n))


def normalize(s: str) -> list[str]:
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)  # Scribe glues words: "HollowOtter70"
    s = s.lower().replace("-", " ")
    s = re.sub(r"\d+", lambda m: f" {num_words(int(m.group()))} ", s)
    s = re.sub(r"[^a-z\s]", " ", s)
    return s.split()


_NUMBER_WORDS = set(_ONES) | set(_TENS[2:]) | {"hundred"}


def _same(a: str, b: str) -> bool:
    if a == b:
        return True
    if a in _NUMBER_WORDS and b in _NUMBER_WORDS:
        return False  # "seven" is never "seventy": no typo tolerance between real numbers
    return difflib.SequenceMatcher(a=a, b=b).ratio() >= _FUZZ


def phrase_match(said: str, expected: str) -> bool:
    """Every expected word must appear in `said`, in order. Extra words are fine,
    and each word gets a small typo allowance (difflib ratio >= 0.8)."""
    want, got = normalize(expected), normalize(said)
    if not want:
        return False
    i = 0
    for j, w in enumerate(got):
        if _same(w, want[i]):
            # "twenty" must not match "twenty one": a tens word followed by a stray ones digit
            nxt_want = want[i + 1] if i + 1 < len(want) else None
            nxt_got = got[j + 1] if j + 1 < len(got) else None
            if (want[i] in _TENS[2:] and nxt_got in _ONES[1:10] and nxt_got != nxt_want):
                return False
            i += 1
            if i == len(want):
                return True
    return "".join(got) == "".join(want)  # all glued, no capitals: "hollowotterseventy"


# ---------------------------------------------------------------- PVC captcha (off by default)
def _captcha_on() -> Optional[str]:
    if os.environ.get("TIP_CAPTCHA", "").strip() != "1":
        return None
    return os.environ.get("ELEVENLABS_VOICE_ID", "").strip() or None


def captcha_text() -> Optional[str]:
    """The captcha the user must read. ElevenLabs serves it as a PNG image, not text, so
    this saves the image to HOME/captcha.png and returns None (can't be spoken by TTS)
    unless ElevenLabs ever returns plain text. None -> tipd uses ECAPA."""
    global last_debug
    vid = _captcha_on()
    if not vid:
        return None
    import requests  # the SDK's captcha.get() drops the response body
    try:
        r = requests.get(f"https://api.elevenlabs.io/v1/voices/pvc/{vid}/captcha",
                         headers={"xi-api-key": os.environ.get("ELEVENLABS_API_KEY", "")},
                         timeout=TIMEOUT_S)
    except requests.RequestException as e:
        last_debug = f"captcha GET error: {e}"
        return None
    ctype = r.headers.get("content-type", "")
    last_debug = f"captcha GET {r.status_code} {ctype} {len(r.content)} bytes"
    if r.status_code != 200:
        return None
    if ctype.startswith("image/"):
        try:
            HOME.mkdir(parents=True, exist_ok=True)
            (HOME / "captcha.png").write_bytes(r.content)
            last_debug += f" -> {HOME / 'captcha.png'}"
        except OSError:
            pass
        return None
    if ctype.startswith("text/"):
        return r.text.strip() or None
    if "json" in ctype:
        try:
            data = r.json()
        except ValueError:
            return None
        if isinstance(data, dict):
            for k in ("text", "captcha", "captcha_text", "challenge"):
                if isinstance(data.get(k), str) and data[k].strip():
                    return data[k].strip()
    return None


def captcha_check(wav: bytes) -> Optional[bool]:
    """True = ElevenLabs accepts the recording as the PVC voice, False = rejected,
    None = couldn't ask (captcha off, auth, expired, network) -> tipd uses ECAPA."""
    global last_debug
    vid = _captcha_on()
    if not vid:
        return None
    try:
        res = _api().voices.pvc.verification.captcha.verify(
            voice_id=vid, recording=("clip.wav", io.BytesIO(wav), "audio/wav"), request_options=_OPTS)
        last_debug = f"captcha verify: {getattr(res, 'status', res)}"
        return getattr(res, "status", "") == "ok"
    except ApiError as e:
        last_debug = f"captcha verify {e.status_code}: {str(e.body)[:200]}"
        return False if e.status_code == 500 else None  # docs: rejection is a 500
    except Exception as e:
        last_debug = f"captcha verify error: {e}"
        return None


# ---------------------------------------------------------------- clone attack
def clone(name: str, wavs: list[bytes]) -> str:
    try:
        files = [(f"sample{i}.wav", io.BytesIO(w), "audio/wav") for i, w in enumerate(wavs)]
        # a non-empty labels dict: without it the API answers 400 "invalid_labels"
        return _api().voices.ivc.create(name=name, files=files, labels={"source": "tip"},
                                        request_options={"timeout_in_seconds": 60, "max_retries": 1}).voice_id
    except RuntimeError:
        raise
    except Exception as e:
        raise _err("instant clone", e) from e


def synth(voice_id: str, text: str) -> bytes:
    """The attacker: speech in the cloned voice, tuned for likeness over expressiveness."""
    return _tts(voice_id, text, CLONE_MODEL,
                VoiceSettings(stability=0.5, similarity_boost=1.0, style=0.0, use_speaker_boost=True))


# ---------------------------------------------------------------- smoke check
if __name__ == "__main__":
    import time
    sentence = sys.argv[1] if len(sys.argv) > 1 else "Matthew, your agent wants to pay five dollars. After the beep, say: purple otter seventeen."
    t = time.perf_counter()
    wav = speak(sentence)
    Path("out.wav").write_bytes(wav)
    t_tts = time.perf_counter() - t
    t = time.perf_counter()
    heard = transcribe(wav)
    t_stt = time.perf_counter() - t
    print(f"voice:  {_prompt_voice()}")
    print(f"said:   {sentence!r}  ({t_tts:.1f}s, out.wav {len(wav)} bytes)")
    print(f"heard:  {heard!r}  ({t_stt:.1f}s)")
    print(f"match:  {phrase_match(heard, 'purple otter seventeen')}")
