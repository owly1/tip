"""Stripe charge: refuses live keys offline; request shape against a stub client.

The real sandbox charge is run by hand, not here.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pay  # noqa: E402


@pytest.fixture(autouse=True)
def notified(monkeypatch):
    """Never touch the real contacts file or network; record what would be pushed."""
    calls = []
    monkeypatch.setattr(pay.contacts, "notify", lambda *a, **k: calls.append((a, k)) or True)
    # run the background notification inline so tests can see it
    monkeypatch.setattr(pay.threading, "Thread", lambda target, args=(), kwargs=None, **_:
                        types.SimpleNamespace(start=lambda: target(*args, **(kwargs or {}))))
    return calls


def stub_client(monkeypatch, status="succeeded", receipt="https://pay.stripe.com/receipts/x"):
    calls = []

    def create(params, options=None):
        calls.append((params, options))
        return types.SimpleNamespace(id="pi_123", status=status,
                                     latest_charge=types.SimpleNamespace(receipt_url=receipt))

    client = types.SimpleNamespace(v1=types.SimpleNamespace(payment_intents=types.SimpleNamespace(create=create)))
    monkeypatch.setattr(pay.stripe, "StripeClient", lambda key: client)
    return calls


@pytest.mark.parametrize("key", ["", "sk_live_abc", "rk_live_abc"])
def test_refuses_non_test_key_before_any_client(monkeypatch, key):
    monkeypatch.setenv("STRIPE_SECRET_KEY", key)
    monkeypatch.setattr(pay.stripe, "StripeClient", lambda k: pytest.fail("client built with a non-test key"))
    with pytest.raises(RuntimeError, match="test key"):
        pay.charge("sam", 500, "usd", "tip to sam")


def test_charge_request_shape(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    calls = stub_client(monkeypatch)
    p = pay.charge("sam", 500, "usd", "coffee")
    assert (p.id, p.receipt_url) == ("pi_123", "https://pay.stripe.com/receipts/x")
    params, options = calls[0]
    assert params["payment_method"] == "pm_card_visa" and params["confirm"] is True
    assert params["automatic_payment_methods"] == {"enabled": True, "allow_redirects": "never"}
    assert (params["amount"], params["currency"], params["metadata"]) == (500, "usd", {"to": "sam"})
    pay.charge("sam", 500, "usd", "coffee")
    assert options["idempotency_key"] != calls[1][1]["idempotency_key"]


@pytest.mark.parametrize("status,receipt", [("requires_action", "https://r"), ("succeeded", None)])
def test_unfinished_payment_raises(monkeypatch, notified, status, receipt):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    stub_client(monkeypatch, status=status, receipt=receipt)
    with pytest.raises(RuntimeError, match="not completed"):
        pay.charge("sam", 500, "usd", "coffee")
    assert notified == []  # nobody is told about money that didn't move


def test_recipient_is_notified_after_payment(monkeypatch, notified):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    monkeypatch.setattr(pay.contract, "OWNER", "matthew")
    stub_client(monkeypatch)
    pay.charge("sam", 550, "usd", "lunch")
    (to, title, message), kw = notified[0]
    assert (to, title, kw["click"]) == ("sam", "matthew sent you $5.50", "https://pay.stripe.com/receipts/x")
    assert message.startswith("lunch")
