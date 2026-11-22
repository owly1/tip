"""Append-only, hash-chained ledger under contract.HOME. Owner: Jasper.

Signatures are fixed by contract.py (see the interfaces block there).
Until this is real, run with TIP_FAKE=1 (or TIP_FAKE=ledger) to use the fake.
"""
from __future__ import annotations

from typing import Optional

from contract import HOME, LedgerEntry, Passport, PassportCheck, PassportRequest, Payment  # noqa: F401


def append(entry: dict) -> LedgerEntry:
    raise NotImplementedError("Jasper: append")


def get(tip_id: str) -> Optional[LedgerEntry]:
    raise NotImplementedError("Jasper: get")


def verify(tip_id: str) -> bool:
    raise NotImplementedError("Jasper: verify")


def spent_today(passport_id: str) -> int:
    raise NotImplementedError("Jasper: spent_today")
