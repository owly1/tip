"""Voice ID: the spoken challenge + words check + voice check. Owner: Matthew.

Modules come from contract.load() at call time so TIP_FAKE switching works.
"""
from __future__ import annotations

import secrets

import contract
from contract import VoiceCheck

ADJECTIVES = ["purple", "silver", "quiet", "golden", "rusty", "happy", "frozen", "brave",
              "sleepy", "orange", "windy", "gentle", "lucky", "velvet", "crimson", "hollow"]
ANIMALS = ["otter", "falcon", "badger", "walrus", "panda", "heron", "lizard", "beaver",
           "moose", "parrot", "turtle", "salmon", "rabbit", "camel", "gecko", "pelican"]
_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_TEENS = ["ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
          "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _number_words(n: int) -> str:
    if n < 20:
        return _TEENS[n - 10]
    t, o = divmod(n, 10)
    return _TENS[t] + (f" {_ONES[o]}" if o else "")


def random_phrase() -> str:
    """'purple otter seventeen': adjective + animal + a number word from 11 to 99."""
    return " ".join([secrets.choice(ADJECTIVES), secrets.choice(ANIMALS),
                     _number_words(11 + secrets.randbelow(89))])


def _say_then_record(text: str) -> bytes:
    """Play the prompt, beep, then record. Recording starts only after playback ends,
    so the mic never hears the laptop saying the phrase."""
    audio = contract.load("audio")
    eleven = contract.load("eleven")
    audio.play(eleven.speak(text))
    audio.beep()
    return audio.record(contract.RECORD_S)


def voice_id(prompt: str) -> VoiceCheck:
    eleven = contract.load("eleven")
    voice = contract.load("voice")

    captcha = eleven.captcha_text()
    if captcha:
        phrase, method = captcha, "captcha"
    else:
        phrase, method = random_phrase(), "ecapa"

    wav = _say_then_record(f"{prompt} After the beep, say: {phrase}")

    transcript = eleven.transcribe(wav) or ""
    words_ok = bool(transcript) and bool(eleven.phrase_match(transcript, phrase))

    threshold = float(voice.THRESHOLD)
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
    score_s = f"{score:.2f}" if score is not None else "n/a"
    print(f"[voice id] {method} | phrase: {phrase!r} | heard: {transcript!r} "
          f"| words {'ok' if words_ok else 'WRONG'} | score {score_s} vs {threshold:.2f} "
          f"| voice {'ok' if voice_ok else 'WRONG'} | {'PASS' if passed else 'FAIL'}"
          + (f" | {note}" if note else ""), flush=True)

    return VoiceCheck(passed=passed, words_ok=words_ok, voice_ok=voice_ok, score=score,
                      threshold=threshold, transcript=transcript, phrase=phrase, method=method)


def enroll(samples: int = 3) -> None:
    voice = contract.load("voice")
    wavs = []
    for i in range(1, samples + 1):
        wavs.append(_say_then_record(f"Sample {i} of {samples}. After the beep, read: {random_phrase()}"))
        print(f"[enroll] sample {i}/{samples} recorded", flush=True)
    voice.enroll(wavs)
    print("[enroll] voiceprint saved", flush=True)
