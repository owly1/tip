"""Mic + speakers. WAV 16 kHz mono 16-bit. Owner: Terry.

Signatures are fixed by contract.py (see the interfaces block there).
Until this is real, run with TIP_FAKE=1 (or TIP_FAKE=audio) to use the fake.
"""
from __future__ import annotations

from typing import Optional

from contract import HOME, LedgerEntry, Passport, PassportCheck, PassportRequest, Payment  # noqa: F401


def record(seconds: float) -> bytes:
    raise NotImplementedError("Terry: record")


def play(wav: bytes) -> None:
    raise NotImplementedError("Terry: play")


def beep() -> None:
    raise NotImplementedError("Terry: beep")
