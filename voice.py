"""ECAPA voiceprint: enroll + score. Owner: Terry.

Signatures are fixed by contract.py (see the interfaces block there).
Until this is real, run with TIP_FAKE=1 (or TIP_FAKE=voice) to use the fake.

The model (speechbrain/spkrec-ecapa-voxceleb, ~80 MB) downloads into HOME/models on first
use. It starts loading in the background as soon as this module is imported, so it's warm
by the time the person has finished speaking.

Enrollment drops bad samples on its own: a sample with under half a second of speech
(missed the beep) or one that doesn't sound like the others (noise, someone else talking).

Standalone (no tipd or ElevenLabs needed):
  python voice.py enroll [n]          record n samples (default 5) from the mic and enroll
  python voice.py live <label>        record 4 s, print the score, log it
  python voice.py files <wav>...      score WAV files, labelled by name up to the first "_"
                                      (name them like clone_1.wav, alex_2.wav)
  python voice.py report              per-label min/mean/max + a suggested THRESHOLD
Every score is appended to HOME/calibration.csv.
"""
from __future__ import annotations

import csv
import io
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from contract import HOME, OWNER, RECORD_S, SAMPLE_RATE

THRESHOLD: float = float(os.environ.get("TIP_VOICE_THRESHOLD", "0.5"))  # set from calibration at 13:00

MODEL_SOURCE = "speechbrain/spkrec-ecapa-voxceleb"
VOICEPRINT = HOME / "voiceprint.npy"
ENROLL_DIR = HOME / "enroll"          # the kept enrollment WAVs, for calibration + the clone demo
CALIBRATION = HOME / "calibration.csv"
MIN_SAMPLES = SAMPLE_RATE // 10       # under 0.1 s of audio scores 0.0 instead of crashing
MIN_SPEECH_S = 0.5                    # enrollment samples with less speech than this are dropped
OUTLIER_GAP = 0.2                     # drop a sample this far below the others' median agreement

# ---------------------------------------------------------------- model
_model = None
_model_lock = threading.Lock()


def _get_model():
    """Load once. The background warm-up and score() share the lock, so score() waits for
    a load already in progress, and retries if the warm-up failed (e.g. no network)."""
    global _model
    with _model_lock:
        if _model is None:
            from speechbrain.inference.speaker import EncoderClassifier
            from speechbrain.utils.fetching import LocalStrategy
            _model = EncoderClassifier.from_hparams(
                source=MODEL_SOURCE, savedir=str(HOME / "models" / "ecapa"),
                run_opts={"device": "cpu"},
                local_strategy=LocalStrategy.COPY)  # symlinks fail on Windows without admin
        return _model


def _warm_up():
    try:
        _get_model()
    except Exception as e:  # score() will retry and raise the real error
        print(f"[voice] model warm-up failed, will retry on first use: {e}", flush=True)


threading.Thread(target=_warm_up, daemon=True, name="ecapa-warmup").start()


# ---------------------------------------------------------------- audio -> embedding
def _decode(wav: bytes) -> np.ndarray:
    """WAV bytes -> float32 mono at SAMPLE_RATE."""
    x, sr = sf.read(io.BytesIO(wav), dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    if sr != SAMPLE_RATE and x.size:
        import torch
        import torchaudio.functional as F
        x = F.resample(torch.from_numpy(x), sr, SAMPLE_RATE).numpy()
    return x


_HOP = SAMPLE_RATE // 100  # 10 ms frames


def _loud(x: np.ndarray) -> np.ndarray:
    """Per-10 ms frame: is this speech? Loud means within 20 dB of the loudest frame AND
    10 dB above the room's noise floor, so a steady fan or hum doesn't count as speech."""
    n = x.size // _HOP
    if n == 0:
        return np.zeros(0, dtype=bool)
    rms = np.sqrt(np.mean(x[: n * _HOP].reshape(n, _HOP) ** 2, axis=1))
    return rms > max(rms.max() * 0.1, np.percentile(rms, 10) * 3.16, 1e-4)


def _speech_seconds(x: np.ndarray) -> float:
    return float(_loud(x).sum()) * _HOP / SAMPLE_RATE


def _speech_span(x: np.ndarray) -> np.ndarray:
    """Cut the silence before and after the speech."""
    loud = np.flatnonzero(_loud(x))
    if loud.size == 0 or (loud[-1] - loud[0] + 1) * _HOP < SAMPLE_RATE // 2:
        return x  # can't find half a second of speech: keep everything
    pad = 10  # 100 ms either side
    n = x.size // _HOP
    return x[max(0, loud[0] - pad) * _HOP: min(n, loud[-1] + 1 + pad) * _HOP]


def _embed(x: np.ndarray) -> np.ndarray:
    import torch
    with torch.no_grad():
        e = _get_model().encode_batch(torch.from_numpy(_speech_span(x)).unsqueeze(0))
    e = e.squeeze().cpu().numpy().astype(np.float32)
    return e / (np.linalg.norm(e) + 1e-9)


def _unit(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) + 1e-9)


def _agreement(embs: np.ndarray) -> list[float]:
    """Each sample vs the mean of the others (same speaker is usually 0.6-0.85)."""
    if len(embs) < 2:
        return []
    return [float(embs[i] @ _unit(np.delete(embs, i, axis=0).mean(axis=0))) for i in range(len(embs))]


# ---------------------------------------------------------------- contract
def enroll(wavs: list[bytes]) -> None:
    if not wavs:
        raise ValueError("enroll needs at least one sample")
    xs = [_decode(w) for w in wavs]

    dropped: list[str] = []
    keep = []
    for i, x in enumerate(xs):
        s = _speech_seconds(x)
        if s >= MIN_SPEECH_S:
            keep.append(i)
        else:
            dropped.append(f"#{i + 1} no speech ({s:.1f}s)")
    embs = {i: _embed(xs[i]) for i in keep}

    if len(keep) >= 3:  # with 3+ samples an odd one out stands out against the median
        agree = dict(zip(keep, _agreement(np.stack([embs[i] for i in keep]))))
        cutoff = float(np.median(list(agree.values()))) - OUTLIER_GAP
        for i in list(keep):
            if agree[i] < cutoff:
                keep.remove(i)
                dropped.append(f"#{i + 1} didn't match the others ({agree[i]:.2f})")

    need = min(2, len(wavs))
    if len(keep) < need:
        raise ValueError(f"only {len(keep)} of {len(wavs)} samples were usable ({'; '.join(dropped)}): "
                         "speak clearly right after each beep and run `tip enroll` again")

    kept = np.stack([embs[i] for i in keep])
    HOME.mkdir(parents=True, exist_ok=True)
    np.save(VOICEPRINT, _unit(kept.mean(axis=0)))
    ENROLL_DIR.mkdir(parents=True, exist_ok=True)
    for old in ENROLL_DIR.glob("sample_*.wav"):
        old.unlink()
    for n, i in enumerate(keep, 1):
        (ENROLL_DIR / f"sample_{n}.wav").write_bytes(wavs[i])

    msg = f"[voice] voiceprint saved from {len(keep)} of {len(wavs)} samples"
    if dropped:
        msg += f" (dropped {'; '.join(dropped)})"
    agree = _agreement(kept)
    if agree:
        msg += f"; agreement {', '.join(f'{s:.2f}' for s in agree)}"
        if np.median(agree) < 0.5:
            msg += "  WARNING: samples disagree, try a quieter spot or move closer to the mic"
    print(msg, flush=True)


def is_enrolled() -> bool:
    return VOICEPRINT.exists()


def score(wav: bytes) -> float:
    voiceprint = np.load(VOICEPRINT)
    x = _decode(wav)
    if x.size < MIN_SAMPLES:
        return 0.0
    return float(_embed(x) @ voiceprint)


# ---------------------------------------------------------------- calibration CLI
def _log(label: str, s: float) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    new = not CALIBRATION.exists()
    with CALIBRATION.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "label", "score", "threshold"])
        w.writerow([time.strftime("%H:%M:%S"), label, f"{s:.4f}", f"{THRESHOLD:.2f}"])


def _show(label: str, s: float) -> None:
    print(f"{label:>16}  score {s:.3f}  vs {THRESHOLD:.2f}  ->  {'PASS' if s >= THRESHOLD else 'fail'}",
          flush=True)
    _log(label, s)


def _report() -> None:
    if not CALIBRATION.exists():
        print("no calibration data yet")
        return
    by: dict[str, list[float]] = {}
    with CALIBRATION.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            by.setdefault(row["label"], []).append(float(row["score"]))
    for label, ss in sorted(by.items()):
        print(f"{label:>16}  n={len(ss):<3} min {min(ss):.3f}  mean {np.mean(ss):.3f}  max {max(ss):.3f}")
    owner = by.get(OWNER, [])
    others = [s for label, ss in by.items() if label != OWNER for s in ss]
    if owner and others:
        lo, hi = min(owner), max(others)
        verdict = "clean gap" if lo > hi else "OVERLAP: someone else beat the owner's worst"
        print(f"\nowner '{OWNER}' worst {lo:.3f} | everyone else best {hi:.3f} | {verdict}")
        print(f"suggested THRESHOLD ~ {(lo + hi) / 2:.2f}")
    else:
        print(f"\nlabel the owner's samples '{OWNER}' (TIP_OWNER) to get a suggested THRESHOLD")


def _main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "enroll":
        import audio
        from stepup import random_phrase
        n = int(argv[1]) if len(argv) > 1 else 5
        wavs = []
        for i in range(1, n + 1):
            print(f"sample {i}/{n}: after the beep, say: {random_phrase()}", flush=True)
            audio.beep()
            wavs.append(audio.record(RECORD_S))
        enroll(wavs)
        return 0
    if cmd == "live":
        if not is_enrolled():
            print("no voiceprint yet: python voice.py enroll")
            return 1
        import audio
        from stepup import random_phrase
        print(f"after the beep, say: {random_phrase()}", flush=True)
        audio.beep()
        _show(argv[1].lower() if len(argv) > 1 else "unknown", score(audio.record(RECORD_S)))
        return 0
    if cmd == "files":
        if not is_enrolled():
            print("no voiceprint yet: python voice.py enroll")
            return 1
        for p in argv[1:]:  # label = file name up to the first "_": clone_3.wav -> "clone"
            _show(Path(p).stem.split("_")[0].lower(), score(Path(p).read_bytes()))
        return 0
    if cmd == "report":
        _report()
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
