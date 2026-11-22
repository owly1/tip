"""Stripe test payments. Owner: Jasper.

Signatures are fixed by contract.py (see the interfaces block there).
Until this is real, run with TIP_FAKE=1 (or TIP_FAKE=pay) to use the fake.
"""
from __future__ import annotations

from typing import Optional

from contract import HOME, LedgerEntry, Passport, PassportCheck, PassportRequest, Payment  # noqa: F401


def charge(to: str, amount_cents: int, currency: str, description: str) -> Payment:
    raise NotImplementedError("Jasper: charge")
