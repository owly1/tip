"""Recipient phone notifications via ntfy: linking, the message sent, and failures never raising."""
from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contacts  # noqa: E402
import contract  # noqa: E402


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(contract, "HOME", tmp_path)
    monkeypatch.delenv("TIP_NTFY_URL", raising=False)
    return tmp_path


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def urlopen(req, timeout=None):
        calls.append((req.full_url, json.loads(req.data.decode("utf-8")), timeout))
        return io.BytesIO(b"{}")
    monkeypatch.setattr(contacts.urllib.request, "urlopen", urlopen)
    return calls


def test_link_makes_an_unguessable_channel(home):
    ch = contacts.link("@Sam")
    assert re.fullmatch(r"tip-sam-[0-9a-f]{12}", ch)
    assert contacts.channel_for("SAM") == ch
    assert json.loads((home / "contacts.json").read_text(encoding="utf-8")) == {"sam": {"ntfy": ch}}
    assert contacts.link("sam") != ch  # relinking makes a fresh one


def test_link_own_channel_and_odd_names():
    assert contacts.link("sam", "sams-phone_42") == "sams-phone_42"
    assert re.fullmatch(r"tip-sam-x-com-[0-9a-f]{12}", contacts.link("sam@x.com"))


@pytest.mark.parametrize("channel", ["", "has space", "slash/no", "x" * 65, "ünïcode"])
def test_link_rejects_bad_channels(channel):
    with pytest.raises(ValueError):
        contacts.link("sam", channel)


def test_link_does_not_overwrite_a_damaged_file(home):
    (home / "contacts.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        contacts.link("sam")
    assert (home / "contacts.json").read_text(encoding="utf-8") == "{not json"
    assert contacts.channel_for("sam") is None


def test_notify_posts_one_json_message(sent):
    ch = contacts.link("sam")
    assert contacts.notify("@sam", "matthew sent you $5.00", "lunch", click="https://pay.stripe.com/r/x") is True
    url, body, timeout = sent[0]
    assert url == "https://ntfy.sh/" and timeout == 3
    assert body == {"topic": ch, "title": "matthew sent you $5.00", "message": "lunch",
                    "tags": ["moneybag"], "click": "https://pay.stripe.com/r/x"}


def test_notify_server_override(sent, monkeypatch):
    monkeypatch.setenv("TIP_NTFY_URL", "http://localhost:8080/")
    contacts.link("sam")
    contacts.notify("sam", "t", "m")
    assert sent[0][0] == "http://localhost:8080/"


def test_notify_unlinked_sends_nothing(sent):
    assert contacts.notify("alex", "t", "m") is False and sent == []


def test_notify_failure_never_raises(monkeypatch, capsys):
    contacts.link("sam")

    def down(req, timeout=None):
        raise OSError("network down")
    monkeypatch.setattr(contacts.urllib.request, "urlopen", down)
    assert contacts.notify("sam", "t", "m") is False
    assert "couldn't notify sam" in capsys.readouterr().err
