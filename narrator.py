"""Narrator: the voice that speaks the Voice ID prompts. Owner: Terry.

speak(text) -> WAV bytes, same as eleven.speak, so stepup swaps one call.

- A soothing, natural ElevenLabs voice (TIP_NARRATOR_VOICE: a name from VOICES or a voice
  id, default sarah) on the most natural model (TIP_NARRATOR_MODEL, default
  eleven_multilingual_v2), at 24 kHz instead of 16 kHz.
- The fixed parts (FIXED) are generated once and saved under HOME/narrator, so they cost
  nothing at payment time. Pre-generate them on the demo laptop with `python narrator.py warm`.
- The variable parts (names, amounts, the phrase) are generated live, in parallel, in the
  same voice, and saved too: a repeated amount or recipient is instant the second time.
- Any failure falls back to eleven.speak(text), so a prompt is never lost.

Try voices:  python narrator.py try sarah jessica lily
Your voice:  python narrator.py clone <name> [seconds]   (read SCRIPT aloud; prints the voice id
             to put in .env as TIP_NARRATOR_VOICE). Don't clone the person who approves payments.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import soundfile as sf

import contract
from contract import HOME

VOICES = {  # soothing, natural female voices from the account's premade set
    "sarah": "EXAVITQu4vr4xnSDxMaL",    # mature, reassuring
    "jessica": "cgSgspJ2msm6clMCkdW9",  # warm, conversational
    "lily": "pFZP5JQG7iQjIQuC4Bku",     # velvety, British
    "matilda": "XrExE9yKIg1WjnnlVkGX",  # calm, professional
}
DEFAULT_VOICE = "sarah"
DEFAULT_MODEL = "eleven_multilingual_v2"
RATE = 24000
FIXED = ("After the beep, say:", "After the beep, read:", "Or say decline.", "One more.")
CACHE = HOME / "narrator"
GAP_S = 0.3          # pause between sentences
GAP_BEFORE_PHRASE_S = 0.2

_SPLIT = re.compile("(" + "|".join(re.escape(f) for f in FIXED) + ")")


def _voice_id() -> str:
    v = os.environ.get("TIP_NARRATOR_VOICE", "").strip() or DEFAULT_VOICE
    vid = VOICES.get(v.lower(), v)
    if vid and vid == os.environ.get("ELEVENLABS_VOICE_ID", "").strip():
        raise RuntimeError("the narrator must not be the owner's cloned voice")
    return vid


def _model() -> str:
    return os.environ.get("TIP_NARRATOR_MODEL", "").strip() or DEFAULT_MODEL


def _for_speech(text: str) -> str:
    """'over the 2000c per-payment cap' -> 'over the $20 per-payment cap'."""
    def money(m: re.Match) -> str:
        c = int(m.group(1))
        return f"${c // 100}" if c % 100 == 0 else f"${c / 100:.2f}"
    return re.sub(r"\b(\d+)c\b", money, text)


def chunks(text: str) -> list[str]:
    """Split a prompt into fixed parts and the variable text between them."""
    return [c.strip() for c in _SPLIT.split(_for_speech(text)) if c.strip()]


def _settings():
    """Looser stability = more natural, human variation; high similarity keeps the clone's
    character; a little style adds warmth."""
    from elevenlabs import VoiceSettings
    kw = dict(stability=0.4, similarity_boost=0.85, style=0.15, use_speaker_boost=True)
    if "speed" in getattr(VoiceSettings, "model_fields", {}):
        kw["speed"] = 0.97
    return VoiceSettings(**kw)


def _mode() -> str:
    """whole (default): the prompt in one take, so intonation flows across sentences.
    splice: fixed parts cached separately (faster first byte, but audible joins)."""
    return os.environ.get("TIP_NARRATOR_MODE", "").strip() or "whole"


def _speakable(text: str) -> str:
    """Small read-aloud fixes that keep the wording: 'claude-code' -> 'claude code',
    'For: x' -> 'For x' (no robotic pause after 'For')."""
    text = _for_speech(text)
    text = re.sub(r"(?<=[A-Za-z])-(?=[A-Za-z])", " ", text)
    return text.replace("For: ", "For ")


def _trim(x: np.ndarray, thr: float = 0.01, pad_s: float = 0.03) -> np.ndarray:
    loud = np.flatnonzero(np.abs(x) > thr)
    if loud.size == 0:
        return x
    pad = int(pad_s * RATE)
    return x[max(0, loud[0] - pad): loud[-1] + 1 + pad]


def _synth(text: str, voice: str, model: str) -> np.ndarray:
    settings = _settings()
    key = hashlib.sha1(f"{voice}|{model}|{RATE}|{settings!r}|{text}".encode("utf-8")).hexdigest()[:20]
    path = CACHE / f"{key}.wav"
    if path.exists():
        x, _ = sf.read(path, dtype="float32")
        return x
    import eleven
    pcm = b"".join(eleven._api().text_to_speech.convert(
        voice_id=voice, text=text, model_id=model, output_format=f"pcm_{RATE}",
        voice_settings=settings, request_options={"timeout_in_seconds": 15, "max_retries": 1}))
    x = _trim(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0)
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = CACHE / f"{key}.tmp"
    sf.write(tmp, x, RATE, format="WAV", subtype="PCM_16")
    os.replace(tmp, path)
    return x


def _render(text: str, voice: str, model: str) -> bytes:
    if _mode() == "whole":
        buf = io.BytesIO()
        sf.write(buf, _synth(_speakable(text), voice, model), RATE, format="WAV", subtype="PCM_16")
        return buf.getvalue()
    parts = chunks(text)
    with ThreadPoolExecutor(max_workers=4) as pool:
        audio = list(pool.map(lambda c: _synth(c, voice, model), parts))
    out: list[np.ndarray] = []
    for i, x in enumerate(audio):
        if i:
            gap = GAP_BEFORE_PHRASE_S if parts[i - 1].endswith(":") else GAP_S
            out.append(np.zeros(int(gap * RATE), np.float32))
        out.append(x)
    buf = io.BytesIO()
    sf.write(buf, np.concatenate(out), RATE, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def speak(text: str) -> bytes:
    eleven = contract.load("eleven")
    if contract.is_fake("eleven"):
        return eleven.speak(text)
    try:
        return _render(text, _voice_id(), _model())
    except Exception as e:  # never lose a prompt: fall back to the plain voice
        print(f"[narrator] falling back to the default voice: {type(e).__name__}: {e}", flush=True)
        return eleven.speak(text)


def warm() -> None:
    """Generate and save the fixed parts now, so the first payment doesn't wait for them."""
    voice, model = _voice_id(), _model()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda c: _synth(c, voice, model), FIXED))
    print(f"[narrator] {len(FIXED)} fixed parts saved in {CACHE}", flush=True)


SAMPLE = ("Matthew, claude-code wants to pay weatherapi $5. "
          "After the beep, say: lucky badger ninety one. Or say decline.")


def clone_from_mic(name: str, seconds: float = 65.0) -> str:
    """Record `seconds` of the narrator reading at the mic's native rate (better clone
    quality than 16 kHz), make an ElevenLabs instant clone, return its voice id."""
    import sounddevice as sd
    import audio
    import eleven
    dev = audio._device("TIP_MIC")
    sr = int(sd.query_devices(dev, kind="input")["default_samplerate"])
    audio.beep()
    x = sd.rec(int(seconds * sr), samplerate=sr, channels=1, dtype="float32", device=dev,
               blocking=True)[:, 0]
    audio.beep()
    if np.abs(x).max() < 0.02:
        raise RuntimeError("the recording is nearly silent: check the mic and try again")
    buf = io.BytesIO()
    sf.write(buf, x, sr, format="WAV", subtype="PCM_16")
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / f"clone_source_{name}.wav").write_bytes(buf.getvalue())
    return eleven.clone(f"tip narrator ({name})", [buf.getvalue()])

if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    args = sys.argv[1:]
    if args[:1] == ["warm"]:
        warm()
    elif args[:1] == ["try"]:
        import time
        import audio
        for name in args[1:] or [DEFAULT_VOICE]:
            os.environ["TIP_NARRATOR_VOICE"] = name
            t = time.perf_counter()
            wav = _render(SAMPLE, _voice_id(), _model())
            print(f"{name}: generated in {time.perf_counter() - t:.1f}s", flush=True)
            audio.play(wav)
            time.sleep(1.0)
    elif args[:1] == ["clone"]:
        import audio
        name = args[1] if len(args) > 1 else "narrator"
        vid = clone_from_mic(name, float(args[2]) if len(args) > 2 else 65.0)
        print(f"narrator voice id: {vid}", flush=True)
        audio.play(_render(SAMPLE, vid, _model()))
    else:
        print(__doc__)
