"""Real ledger: hash chain, tamper detection, daily totals."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contract  # noqa: E402
import ledger  # noqa: E402


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(contract, "HOME", tmp_path)
    return tmp_path


def row(i, approver="matthew", ts=None, amount=500):
    return {"id": f"tp_{i:010x}", "ts": time.time() if ts is None else ts, "to": "sam",
            "amount_cents": amount, "currency": "usd", "agent": None, "reason": "coffee",
            "via": "voice", "approver": approver, "payment_id": f"pi_{i}",
            "receipt_url": f"https://pay.stripe.com/receipts/{i}", "voice_score": 0.8}


def test_empty_ledger():
    assert ledger.get("tp_x") is None and ledger.verify("tp_x") is False and ledger.spent_today("pp_x") == 0


def test_chain_verifies_and_matches_fake():
    fake = contract._fake_ledger()
    rows = [row(i) for i in range(3)]
    for r in rows:
        assert ledger.append(dict(r)) == fake.append(dict(r))
    assert all(ledger.verify(r["id"]) for r in rows)
    assert ledger.get(rows[1]["id"]).amount_cents == 500
    assert ledger.verify("tp_missing") is False


def test_editing_a_line_breaks_verify_from_there_on(home):
    rows = [row(i) for i in range(3)]
    for r in rows:
        ledger.append(r)
    path = home / "ledger.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    d = json.loads(lines[1])
    d["amount_cents"] = 50000
    lines[1] = json.dumps(d)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert [ledger.verify(r["id"]) for r in rows] == [True, False, False]
    assert ledger.get(rows[1]["id"]).amount_cents == 50000  # still readable, just not verified
    ledger.append(row(3))  # later tips still get logged, and stay unverified
    assert ledger.verify(row(3)["id"]) is False


def test_garbage_line_is_unverified_not_an_error(home):
    ledger.append(row(0))
    with open(home / "ledger.jsonl", "a", encoding="utf-8") as f:
        f.write("{not json\n")
    assert ledger.verify(row(0)["id"]) is True
    assert ledger.verify("tp_anything") is False and ledger.get("tp_anything") is None


def test_bad_entry_writes_nothing(home):
    with pytest.raises(TypeError):
        ledger.append({**row(0), "extra": 1})
    assert not (home / "ledger.jsonl").exists()


def test_spent_today_only_today_and_only_that_passport():
    ledger.append(row(0, approver="pp_a", amount=300))
    ledger.append(row(1, approver="pp_a", amount=200))
    ledger.append(row(2, approver="pp_b", amount=999))
    ledger.append(row(3, approver="pp_a", amount=700, ts=time.time() - 86400))
    assert ledger.spent_today("pp_a") == 500
