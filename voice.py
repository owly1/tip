"""ECAPA voiceprint: enroll + score. Owner: Terry.

Signatures are fixed by contract.py (see the interfaces block there).
Until this is real, run with TIP_FAKE=1 (or TIP_FAKE=voice) to use the fake.
"""
from __future__ import annotations

from typing import Optional

from contract import HOME, LedgerEntry, Passport, PassportCheck, PassportRequest, Payment  # noqa: F401

THRESHOLD: float = 0.5  # replace with the calibrated number at 13:00


def enroll(wavs: list[bytes]) -> None:
    raise NotImplementedError("Terry: enroll")


def is_enrolled() -> bool:
    raise NotImplementedError("Terry: is_enrolled")


def score(wav: bytes) -> float:
    raise NotImplementedError("Terry: score")
