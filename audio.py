"""Mic + speakers. WAV 16 kHz mono 16-bit. Owner: Terry.

Signatures are fixed by contract.py (see the interfaces block there).
Until this is real, run with TIP_FAKE=1 (or TIP_FAKE=audio) to use the fake.

Pick devices with TIP_MIC / TIP_SPEAKER: an index or part of a name from
`python -m sounddevice` (e.g. TIP_MIC="USB" for a clip-on mic at the demo).

Quick check:  python audio.py   (beep, record 3 s, print the level, play it back)
"""
from __future__ import annotations

import io
import os
import time

import numpy as np
import sounddevice as sd
import soundfile as sf

from contract import SAMPLE_RATE

# An int16 peak below this means the mic delivered nothing at all, which on macOS
# almost always means the terminal running tipd has no microphone permission.
SILENT_PEAK = 8


def _device(var: str):
    v = os.environ.get(var, "").strip()
    if not v:
        return None
    return int(v) if v.isdigit() else v


def to_wav(samples: np.ndarray, sr: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, samples, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def from_wav(wav: bytes) -> tuple[np.ndarray, int]:
    """WAV bytes -> (float32 mono samples in [-1, 1], sample rate)."""
    data, sr = sf.read(io.BytesIO(wav), dtype="float32", always_2d=True)
    return data.mean(axis=1), sr


def record(seconds: float) -> bytes:
    frames = int(round(seconds * SAMPLE_RATE))
    data = sd.rec(frames, samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                  device=_device("TIP_MIC"), blocking=True)
    peak = int(np.abs(data.astype(np.int32)).max()) if data.size else 0
    if peak < SILENT_PEAK:
        raise RuntimeError(
            f"the mic recorded pure silence (peak {peak}): give the terminal running tipd "
            "microphone access (System Settings > Privacy & Security > Microphone), "
            "or set TIP_MIC to the right device")
    return to_wav(data[:, 0])


def play(wav: bytes) -> None:
    samples, sr = from_wav(wav)
    if samples.size == 0:
        return
    sd.play(samples, sr, device=_device("TIP_SPEAKER"), blocking=True)


def beep() -> None:
    t = np.arange(int(0.15 * SAMPLE_RATE)) / SAMPLE_RATE
    fade = np.minimum(1.0, np.minimum(t, t[-1] - t) / 0.01)  # 10 ms ramps, no click
    tone = (0.3 * np.sin(2 * np.pi * 880 * t) * fade).astype(np.float32)
    sd.play(tone, SAMPLE_RATE, device=_device("TIP_SPEAKER"), blocking=True)
    time.sleep(0.05)  # let the tail die out so the recording doesn't start on it


if __name__ == "__main__":
    print("after the beep, talk for 3 seconds...", flush=True)
    beep()
    wav = record(3.0)
    x, _ = from_wav(wav)
    print(f"recorded {x.size / SAMPLE_RATE:.1f}s  peak {np.abs(x).max():.3f}  "
          f"rms {np.sqrt(np.mean(x ** 2)):.4f}   (speech is usually peak > 0.1)", flush=True)
    print("playing it back...", flush=True)
    play(wav)
