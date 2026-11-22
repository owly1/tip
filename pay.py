"""Stripe test payments. Owner: Jasper.

Signatures are fixed by contract.py (see the interfaces block there).
Sandbox only: refuses any key that isn't a test key.
"""
from __future__ import annotations

import os
import threading
import uuid

import stripe

import contacts
import contract
from contract import Payment

TEST_CARD = "pm_card_visa"  # Stripe's test card; never taken from input


def charge(to: str, amount_cents: int, currency: str, description: str) -> Payment:
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if "_test_" not in key:
        raise RuntimeError("STRIPE_SECRET_KEY must be a Stripe test key (sk_test_...); refusing to charge")
    client = stripe.StripeClient(key)
    pi = client.v1.payment_intents.create(
        params={
            "amount": amount_cents,
            "currency": currency,
            "confirm": True,
            "payment_method": TEST_CARD,
            "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},  # no return_url needed
            "description": description,
            "metadata": {"to": to},
            "expand": ["latest_charge"],
        },
        # one key per call: the library's own network retries can't double-charge
        options={"idempotency_key": str(uuid.uuid4())},
    )
    receipt = getattr(pi.latest_charge, "receipt_url", None)
    if pi.status != "succeeded" or not receipt:
        raise RuntimeError(f"Stripe payment {pi.id} not completed (status {pi.status}); not retrying")
    amount = f"${amount_cents / 100:.2f}" if currency.lower() == "usd" else f"{amount_cents / 100:.2f} {currency.upper()}"
    # in the background: a slow ntfy must not push tipd past its deadline after the money has moved.
    # daemon is fine because tipd outlives it; a short-lived process would have to join() it
    threading.Thread(target=contacts.notify, daemon=True,
                     args=(to, f"{contract.OWNER} sent you {amount}", f"{description}\nTest payment · tap for the receipt"),
                     kwargs={"click": receipt}).start()
    return Payment(id=pi.id, receipt_url=receipt)
