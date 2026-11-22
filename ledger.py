"""Append-only, hash-chained ledger under contract.HOME. Owner: Jasper.

Signatures are fixed by contract.py (see the interfaces block there).
Tamper-evident, not tamper-proof: editing a line makes verify() false from that line on.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from typing import Optional

import contract
from contract import LedgerEntry

GENESIS = "0" * 64
_LOCK = threading.Lock()  # tipd's threads; _file_lock covers separate `tip` processes
_DAMAGED = (ValueError, TypeError, AttributeError)  # bad JSON, unknown field, non-object line


def _path():
    return contract.HOME / "ledger.jsonl"


def _lines() -> list[str]:
    p = _path()
    return [line for line in p.read_text(encoding="utf-8").splitlines() if line.strip()] if p.exists() else []


def _rows():
    return (json.loads(line) for line in _lines())  # lazy: a damaged line only affects readers that reach it


def _hash(prev: str, entry: dict) -> str:
    return hashlib.sha256((prev + json.dumps(entry, sort_keys=True)).encode("utf-8")).hexdigest()


@contextmanager
def _file_lock():
    contract.HOME.mkdir(parents=True, exist_ok=True)
    with open(contract.HOME / "ledger.lock", "a+b") as f:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # retries for ~10 s, then raises
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


# shortcut: an unreadable last line (crash mid-write) makes append raise after tipd has charged;
# pass the tip id into charge() so Stripe metadata can reconcile, if that ever happens
def append(entry: dict) -> LedgerEntry:
    with _LOCK, _file_lock():
        lines = _lines()
        prev = json.loads(lines[-1])["hash"] if lines else GENESIS  # last line only: a tampered middle still appends
        e = LedgerEntry(**entry, prev_hash=prev, hash=_hash(prev, entry))  # bad fields raise before writing
        contract.HOME.mkdir(parents=True, exist_ok=True)
        with open(_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(e)) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return e


def get(tip_id: str) -> Optional[LedgerEntry]:
    try:
        return next((LedgerEntry(**r) for r in _rows() if r.get("id") == tip_id), None)
    except _DAMAGED:  # verify() reports it
        return None


def verify(tip_id: str) -> bool:
    prev = GENESIS
    try:
        for r in _rows():
            entry = {k: v for k, v in r.items() if k not in ("prev_hash", "hash")}
            if r.get("prev_hash") != prev or r.get("hash") != _hash(prev, entry):
                return False
            if r.get("id") == tip_id:
                return True
            prev = r["hash"]
    except _DAMAGED:
        return False
    return False


def spent_today(passport_id: str) -> int:
    # raises on a damaged ledger, so tipd errors instead of approving against an unknown total
    day = int(time.time() // 86400)  # UTC day, same as the fake
    return sum(r["amount_cents"] for r in _rows()
               if r["approver"] == passport_id and int(r["ts"] // 86400) == day)
