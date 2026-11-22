"""Voice ID: the spoken challenge + words check + voice check. Owner: Matthew.

Modules come from contract.load() at call time so TIP_FAKE switching works.
"""
from __future__ import annotations

import os
import secrets
import threading

import contract
import screen
from typing import Optional

from contract import VoiceCheck

ADJECTIVES = ["purple", "silver", "quiet", "golden", "rusty", "happy", "frozen", "brave",
              "sleepy", "orange", "windy", "gentle", "lucky", "velvet", "crimson", "hollow"]
ANIMALS = ["otter", "falcon", "badger", "walrus", "panda", "heron", "lizard", "beaver",
           "moose", "parrot", "turtle", "salmon", "rabbit", "camel", "gecko", "pelican"]
_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_TEENS = ["ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
          "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


# Current challenge, read by tipd's demo-only GET /challenge (see attack.py).
_CH_LOCK = threading.Lock()
_CH = {"active": False, "phase": None, "phrase": "", "id": 0}


def _set_challenge(phase, phrase: str = "", new: bool = False) -> None:
    with _CH_LOCK:
        if new:
            _CH["id"] += 1
        _CH["active"] = phase is not None
        _CH["phase"] = phase
        _CH["phrase"] = phrase if phase is not None else ""


def current_challenge() -> dict:
    with _CH_LOCK:
        return dict(_CH)


def _number_words(n: int) -> str:
    if n < 20:
        return _TEENS[n - 10]
    t, o = divmod(n, 10)
    return _TENS[t] + (f" {_ONES[o]}" if o else "")


def random_phrase() -> str:
    """'purple otter seventeen': adjective + animal + a number word from 11 to 99."""
    return " ".join([secrets.choice(ADJECTIVES), secrets.choice(ANIMALS),
                     _number_words(11 + secrets.randbelow(89))])


DECLINE_WORDS = {"decline", "declined", "deny", "denied", "reject", "cancel", "refuse", "stop"}


def is_decline(transcript: str) -> bool:
    """'Decline.', 'decline it', 'No, cancel' -> True. None of these words are in the phrase lists."""
    words = "".join(c if c.isalpha() else " " for c in transcript.lower()).split()
    return bool(words) and (any(w in DECLINE_WORDS for w in words) or words in (["no"], ["nope"]))


def _say_then_record(text: str, phrase: str) -> bytes:
    """Play the prompt, beep, then record. Recording starts only after playback ends,
    so the mic never hears the laptop saying the phrase."""
    audio = contract.load("audio")
    eleven = contract.load("eleven")
    try:
        _set_challenge("speaking", phrase, new=True)
        screen.speaking()
        screen.say(phrase)
        audio.play(eleven.speak(text))
        audio.beep()
        _set_challenge("listening", phrase)
        screen.listening(contract.RECORD_S)
        try:
            return audio.record(contract.RECORD_S)
        finally:
            screen.done_listening()
    finally:
        _set_challenge(None)


# Big payments get a stricter check. Calibration (13:10): Matthew 0.54-0.70, his ElevenLabs
# clone 0.43-0.56, teammates 0.10-0.25. One phrase at 0.50 let the clone through 1 time in 4,
# so over STRICT_OVER_CENTS it takes STRICT_ROUNDS phrases and their average must clear
# STRICT_THRESHOLD: the clone has to get lucky on every phrase at once.
STRICT_OVER_CENTS = int(os.environ.get("TIP_STRICT_OVER_CENTS", "5000"))
STRICT_ROUNDS = int(os.environ.get("TIP_STRICT_ROUNDS", "2"))
STRICT_THRESHOLD = float(os.environ.get("TIP_STRICT_THRESHOLD", "0.53"))


def voice_id(prompt: str, amount_cents: Optional[int] = None) -> VoiceCheck:
    rounds = STRICT_ROUNDS if (amount_cents or 0) >= STRICT_OVER_CENTS and STRICT_ROUNDS > 1 else 1
    if rounds == 1:
        return _one_round(prompt)
    screen.line(f"large payment: {rounds} phrases, average score must reach {STRICT_THRESHOLD:.2f}")
    checks: list[VoiceCheck] = []
    for i in range(rounds):
        text = prompt if i == 0 else "One more."
        vc = _one_round(text, threshold=STRICT_THRESHOLD, label=f"phrase {i + 1}/{rounds}")
        if vc.method == "captcha":
            return vc  # ElevenLabs decided the voice; the strict rounds are for ECAPA
        if vc.declined:
            return vc
        checks.append(vc)
        if not vc.words_ok or vc.score is None or vc.method != "ecapa":
            break  # wrong words (or no voiceprint) fails right away
    scores = [c.score for c in checks if c.score is not None]
    avg = sum(scores) / len(scores) if scores else None
    words_ok = len(checks) == rounds and all(c.words_ok for c in checks)
    voice_ok = words_ok and avg is not None and avg >= STRICT_THRESHOLD
    if words_ok and avg is not None:
        screen.line(screen.bold(f"average {avg:.3f} vs {STRICT_THRESHOLD:.2f} → "
                                f"{'voice ok' if voice_ok else 'wrong voice'}"))
    screen.plain(f"[voice id] strict x{rounds} | scores {[round(s, 2) for s in scores]} "
                 f"| avg {avg if avg is None else round(avg, 2)} vs {STRICT_THRESHOLD:.2f} "
                 f"| {'PASS' if voice_ok else 'FAIL'}")
    return VoiceCheck(passed=voice_ok, words_ok=words_ok, voice_ok=voice_ok, score=avg,
                      threshold=STRICT_THRESHOLD,
                      transcript=" / ".join(c.transcript for c in checks),
                      phrase=" / ".join(c.phrase for c in checks), method="ecapa")


def _one_round(prompt: str, threshold: Optional[float] = None, label: str = "") -> VoiceCheck:
    eleven = contract.load("eleven")
    voice = contract.load("voice")

    captcha = eleven.captcha_text()
    if captcha:
        phrase, method = captcha, "captcha"
    else:
        phrase, method = random_phrase(), "ecapa"

    if label:
        screen.line(screen.bold(label))
    wav = _say_then_record(f"{prompt} After the beep, say: {phrase}. Or say decline.", phrase)

    transcript = eleven.transcribe(wav) or ""
    words_ok = bool(transcript) and bool(eleven.phrase_match(transcript, phrase))
    if not words_ok and is_decline(transcript):
        # Declining is always safe, so it doesn't need the owner's voice: anyone can stop a payment.
        screen.line(f"heard: “{transcript}”  → declined")
        screen.plain(f"[voice id] declined | heard: {transcript!r}")
        return VoiceCheck(passed=False, words_ok=False, voice_ok=False, transcript=transcript,
                          phrase=phrase, method=method, declined=True)
    screen.heard(transcript, words_ok)

    threshold = float(voice.THRESHOLD) if threshold is None else threshold
    score = None
    enrolled = voice.is_enrolled()
    if enrolled:
        score = float(voice.score(wav))  # always shown on screen, even on the captcha path

    note = ""
    voice_ok = False
    captcha_result = eleven.captcha_check(wav) if method == "captcha" else None
    if captcha_result is not None:
        voice_ok = bool(captcha_result)
    else:
        if method == "captcha":
            method = "ecapa"  # captcha unavailable for checking, fall back
        if not enrolled:
            note = "no voiceprint: run `tip enroll` first"
        else:
            voice_ok = score is not None and score >= threshold

    passed = words_ok and voice_ok
    screen.score_bar(score, threshold, voice_ok, method, captcha_result)
    if note:
        screen.line(note)
    score_s = f"{score:.2f}" if score is not None else "n/a"
    screen.plain(f"[voice id] {method} | phrase: {phrase!r} | heard: {transcript!r} "
          f"| words {'ok' if words_ok else 'WRONG'} | score {score_s} vs {threshold:.2f} "
          f"| voice {'ok' if voice_ok else 'WRONG'} | {'PASS' if passed else 'FAIL'}"
          + (f" | {note}" if note else ""))

    return VoiceCheck(passed=passed, words_ok=words_ok, voice_ok=voice_ok, score=score,
                      threshold=threshold, transcript=transcript, phrase=phrase, method=method)


def enroll(samples: int = 3) -> None:
    voice = contract.load("voice")
    wavs = []
    screen.card_open("ENROLL VOICEPRINT", [f"{samples} samples · read each phrase after the beep"])
    for i in range(1, samples + 1):
        phrase = random_phrase()
        screen.line(screen.bold(f"sample {i}/{samples}"))
        wavs.append(_say_then_record(f"Sample {i} of {samples}. After the beep, read: {phrase}", phrase))
        screen.plain(f"[enroll] sample {i}/{samples} recorded")
    voice.enroll(wavs)
    screen.verdict(True, "VOICEPRINT SAVED")
    screen.card_close()
    screen.plain("[enroll] voiceprint saved")
