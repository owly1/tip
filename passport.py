"""Agent passports: Alter, or local HMAC signed with a key under contract.HOME. Owner: Jasper.

Signatures are fixed by contract.py (see the interfaces block there).
Until this is real, run with TIP_FAKE=1 (or TIP_FAKE=passport) to use the fake.
"""
from __future__ import annotations

from typing import Optional

from contract import HOME, LedgerEntry, Passport, PassportCheck, PassportRequest, Payment  # noqa: F401


def issue(req: PassportRequest) -> tuple[str, Passport]:
    raise NotImplementedError("Jasper: issue")


def check(token: str, to: str, amount_cents: int, spent_today_cents: int) -> PassportCheck:
    raise NotImplementedError("Jasper: check")
