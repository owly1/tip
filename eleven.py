"""ElevenLabs: TTS, Scribe, PVC captcha, clone attack. Owner: Alex.

Signatures are fixed by contract.py (see the interfaces block there).
Until this is real, run with TIP_FAKE=1 (or TIP_FAKE=eleven) to use the fake.
"""
from __future__ import annotations

from typing import Optional

from contract import HOME, LedgerEntry, Passport, PassportCheck, PassportRequest, Payment  # noqa: F401


def speak(text: str) -> bytes:
    raise NotImplementedError("Alex: speak")


def transcribe(wav: bytes) -> str:
    raise NotImplementedError("Alex: transcribe")


def phrase_match(said: str, expected: str) -> bool:
    raise NotImplementedError("Alex: phrase_match")


def captcha_text() -> Optional[str]:
    raise NotImplementedError("Alex: captcha_text")


def captcha_check(wav: bytes) -> Optional[bool]:
    raise NotImplementedError("Alex: captcha_check")


def clone(name: str, wavs: list[bytes]) -> str:
    raise NotImplementedError("Alex: clone")


def synth(voice_id: str, text: str) -> bytes:
    raise NotImplementedError("Alex: synth")
