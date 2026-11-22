"""cli.py tests against a stub tipd (no real tipd, no network beyond localhost)."""
from __future__ import annotations

import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import cli
import contract

AGENT_ENV = ("CLAUDECODE", "CURSOR_AGENT", "CURSOR_TRACE_ID", "TIP_PASSPORT")

PAID = {"status": "paid", "id": "tp_ab12", "via": "passport", "to": "sam", "amount_cents": 500,
        "receipt_url": "https://pay.stripe.com/receipts/x", "message": "", "voice": None, "passport": None}
VOICE = {"passed": False, "words_ok": True, "voice_ok": False, "score": 0.21, "threshold": 0.5,
         "transcript": "purple otter seventeen", "phrase": "purple otter seventeen", "method": "ecapa", "declined": False}


class Stub:
    def __init__(self):
        self.routes: dict[tuple[str, str], tuple[int, dict]] = {}
        self.requests: list[tuple[str, str, object]] = []

    def on(self, method, path, body, code=200):
        self.routes[(method, path)] = (code, body)

    @property
    def last(self):
        return self.requests[-1]


@pytest.fixture
def stub(monkeypatch):
    s = Stub()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _go(self, method):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            s.requests.append((method, self.path, json.loads(raw) if raw else None))
            code, body = s.routes.get((method, self.path), (404, {"status": "error", "message": "not found"}))
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._go("GET")

        def do_POST(self):
            self._go("POST")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    monkeypatch.setenv("TIPD_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    for k in AGENT_ENV:
        monkeypatch.delenv(k, raising=False)
    for k in list(os.environ):
        if k.startswith("CODEX_"):
            monkeypatch.delenv(k, raising=False)
    yield s
    srv.shutdown()
    srv.server_close()


# ---------------------------------------------------------------- amounts
@pytest.mark.parametrize("text,cents", [("5", 500), ("5.5", 550), ("5.50", 550), ("$5", 500),
                                        ("$1,200.00", 120000), ("0.01", 1), (" 20 ", 2000)])
def test_amount_good(text, cents):
    assert cli.parse_amount(text) == cents


@pytest.mark.parametrize("text", ["0", "-5", "abc", "NaN", "inf", "5.555", "", "$", "0.00"])
def test_amount_bad(text):
    with pytest.raises(ValueError):
        cli.parse_amount(text)


def test_bad_amount_exits_1_without_request(stub, capsys):
    assert cli.main(["@sam", "5.555"]) == 1
    assert stub.requests == []
    assert "decimal" in capsys.readouterr().err


@pytest.mark.parametrize("text,secs", [("30m", 1800), ("24h", 86400), ("7d", 604800), ("3600", 3600)])
def test_duration(text, secs):
    assert cli.parse_duration(text) == secs


# ---------------------------------------------------------------- request shape
@pytest.mark.parametrize("who", ["@Sam", "sam", "@sam"])
def test_at_stripping(stub, who):
    stub.on("POST", "/tip", PAID)
    assert cli.main([who, "5"]) == 0
    method, path, body = stub.last
    assert (method, path) == ("POST", "/tip")
    assert body["to"] == "sam" and body["amount_cents"] == 500 and body["currency"] == "usd"


@pytest.mark.parametrize("env,agent", [({"CLAUDECODE": "1"}, "claude-code"),
                                       ({"CODEX_SANDBOX": "seatbelt"}, "codex"),
                                       ({"CURSOR_AGENT": "1"}, "cursor"),
                                       ({"CURSOR_TRACE_ID": "abc"}, "cursor"),
                                       ({}, None)])
def test_agent_detection(stub, monkeypatch, env, agent):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    stub.on("POST", "/tip", PAID)
    cli.main(["sam", "5"])
    assert stub.last[2]["agent"] == agent


def test_agent_flag_overrides(stub, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    stub.on("POST", "/tip", PAID)
    cli.main(["sam", "5", "--agent", "my-script"])
    assert stub.last[2]["agent"] == "my-script"


def test_passport_from_env_and_flag(stub, monkeypatch):
    stub.on("POST", "/tip", PAID)
    monkeypatch.setenv("TIP_PASSPORT", "envtok")
    cli.main(["sam", "5", "--for", "api credits"])
    assert stub.last[2]["passport"] == "envtok" and stub.last[2]["reason"] == "api credits"
    cli.main(["sam", "5", "--passport", "flagtok"])
    assert stub.last[2]["passport"] == "flagtok"
    monkeypatch.delenv("TIP_PASSPORT")
    cli.main(["sam", "5"])
    assert stub.last[2]["passport"] is None


# ---------------------------------------------------------------- results
@pytest.mark.parametrize("body,code,text", [
    (PAID, 0, "paid $5.00 to @sam via passport"),
    ({"status": "denied", "message": "passport expired"}, 2, "denied: passport expired"),
    ({"status": "voice_failed", "voice": VOICE}, 3, "voice check failed: wrong voice (score 0.21 < 0.50"),
    ({"status": "timeout"}, 4, "timed out after 60 s"),
])
def test_exit_codes(stub, capsys, body, code, text):
    stub.on("POST", "/tip", body)
    assert cli.main(["@sam", "5"]) == code
    assert text in capsys.readouterr().out


def test_paid_shows_voice_score(stub, capsys):
    stub.on("POST", "/tip", {**PAID, "via": "voice",
                             "voice": {**VOICE, "passed": True, "voice_ok": True, "score": 0.83}})
    assert cli.main(["sam", "200"]) == 0
    out = capsys.readouterr().out
    assert "via voice" in out and "voice score 0.83 >= 0.50" in out


def test_wrong_words(stub, capsys):
    stub.on("POST", "/tip", {"status": "voice_failed",
                             "voice": {**VOICE, "words_ok": False, "voice_ok": True, "transcript": "hello"}})
    assert cli.main(["sam", "200"]) == 3
    assert 'wrong words (heard "hello")' in capsys.readouterr().out


def test_json_stdout_is_only_json(stub, capsys):
    body = {**PAID, "voice": {**VOICE, "passed": True, "voice_ok": True}}
    stub.on("POST", "/tip", body)
    assert cli.main(["sam", "5", "--json"]) == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out) == body
    assert cap.out.count("\n") == 1
    assert cap.err == ""


def test_http_error_body(stub, capsys):
    stub.on("POST", "/tip", {"status": "error", "message": "stripe is down"}, code=500)
    assert cli.main(["sam", "5"]) == 1
    assert "error: stripe is down" in capsys.readouterr().out
    stub.on("POST", "/tip", {"status": "denied", "message": "unknown recipient"}, code=403)
    assert cli.main(["sam", "5"]) == 2


def test_tipd_down(monkeypatch, capsys):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setenv("TIPD_URL", f"http://127.0.0.1:{port}")
    assert cli.main(["sam", "5"]) == 1
    assert "tipd isn't running. Start it with: tipd" in capsys.readouterr().err


def test_usage_error_is_1_not_2(stub):
    assert cli.main(["sam"]) == 1
    assert stub.requests == []


def test_help(capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "0 paid, 1 error, 2 denied, 3 voice failed, 4 timed out" in out and "@verify" in out


# ---------------------------------------------------------------- passport
def test_passport_issue(stub, capsys):
    stub.on("POST", "/passport", {"status": "issued", "token": "TOK123",
                                  "passport": {"id": "pp_1", "agent": "claude-code", "owner": "matthew",
                                               "to": ["weatherapi", "sam"], "max_cents": 2000,
                                               "daily_cents": 5000, "purpose": "apis",
                                               "expires_at": 1_900_000_000}})
    code = cli.main(["passport", "issue", "--agent", "claude-code", "--to", "@WeatherAPI, sam",
                     "--max", "20", "--daily", "50", "--for", "apis"])
    assert code == 0
    method, path, body = stub.last
    assert (method, path) == ("POST", "/passport")
    assert body == {"agent": "claude-code", "to": ["weatherapi", "sam"], "max_cents": 2000,
                    "daily_cents": 5000, "purpose": "apis", "expires_s": 86400}
    out = capsys.readouterr().out
    assert 'export TIP_PASSPORT="TOK123"' in out and '$env:TIP_PASSPORT="TOK123"' in out


def test_passport_expires_flag(stub):
    stub.on("POST", "/passport", {"status": "issued", "token": "T", "passport": {}})
    cli.main(["passport", "issue", "--agent", "a", "--to", "b", "--max", "1", "--daily", "1",
              "--for", "x", "--expires", "30m"])
    assert stub.last[2]["expires_s"] == 1800


def test_passport_voice_failed(stub):
    stub.on("POST", "/passport", {"status": "voice_failed", "voice": VOICE})
    assert cli.main(["passport", "issue", "--agent", "a", "--to", "b", "--max", "1",
                     "--daily", "1", "--for", "x"]) == 3


def test_passport_short_form(stub):
    stub.on("POST", "/passport", {"status": "issued", "token": "T", "passport": {}})
    assert cli.main(["passport", "claude-code", "matthew", "@Sam", "max-50"]) == 0
    assert stub.last[2] == {"agent": "claude-code", "to": ["matthew", "sam"], "max_cents": 5000,
                            "daily_cents": 5000, "purpose": "tips", "expires_s": 86400}


def test_passport_short_form_options(stub):
    stub.on("POST", "/passport", {"status": "issued", "token": "T", "passport": {}})
    cli.main(["passport", "claude-code", "matthew", "MAX-$7.50", "--expires", "7d", "--for", "team lunch"])
    body = stub.last[2]
    assert (body["max_cents"], body["daily_cents"], body["expires_s"], body["purpose"]) == (750, 750, 7 * 86400, "team lunch")


@pytest.mark.parametrize("argv", [
    [],
    ["claude-code", "max-50"],                  # no names
    ["claude-code", "matthew", "sam"],          # no max
    ["claude-code", "matthew", "max-abc"],      # not an amount
    ["claude-code", "max-5", "sam", "max-50"],  # max in the middle
    # an unquoted reason or split value must never turn stray words into payees or the agent
    ["claude-code", "sam", "--for", "team", "lunch", "max-5"],
    ["claude-code", "matthew", "--for", "lunch", "with", "sam", "max-50"],
    ["--for", "team", "lunch", "claude-code", "matthew", "max-50"],
    ["claude-code", "sam", "--expires", "7", "d", "max-5"],
    ["claude-code", "sam", "max-5", "--for", "team", "lunch"],
])
def test_passport_short_form_errors(stub, argv):
    assert cli.main(["passport"] + argv) == 1
    assert stub.requests == []


# ---------------------------------------------------------------- verify / status / enroll / dispatch
def test_verify(stub, capsys):
    entry = {"id": "tp_1", "to": "sam", "amount_cents": 500, "currency": "usd", "via": "voice",
             "approver": "matthew", "agent": None, "reason": None}
    stub.on("GET", "/verify/tp_1", {"ok": True, "entry": entry})
    assert cli.main(["verify", "tp_1"]) == 0
    assert "ok: tp_1 chain intact" in capsys.readouterr().out
    stub.on("GET", "/verify/tp_1", {"ok": False, "entry": entry})
    assert cli.main(["verify", "tp_1"]) == 2
    stub.on("GET", "/verify/tp_2", {"ok": False, "entry": None})
    assert cli.main(["verify", "tp_2", "--json"]) == 2


def test_status(stub, capsys):
    stub.on("GET", "/tip/tp_ab12", PAID)
    assert cli.main(["status", "tp_ab12"]) == 0
    assert "tp_ab12" in capsys.readouterr().out


def test_enroll(stub):
    stub.on("POST", "/enroll", {"status": "enrolled"})
    assert cli.main(["enroll"]) == 0
    assert stub.last == ("POST", "/enroll", {"samples": 3})


def test_dispatch_vs_at_recipient(stub):
    stub.on("GET", "/verify/tp_1", {"ok": True, "entry": None})
    stub.on("POST", "/tip", PAID)
    cli.main(["verify", "tp_1"])
    assert stub.last[:2] == ("GET", "/verify/tp_1")
    cli.main(["@verify", "5"])
    assert stub.last[:2] == ("POST", "/tip") and stub.last[2]["to"] == "verify"


# ---------------------------------------------------------------- link (phone notifications)
def test_link(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(contract, "HOME", tmp_path)
    assert cli.main(["link", "@Sam"]) == 0
    out = capsys.readouterr().out
    import contacts
    ch = contacts.channel_for("sam")
    assert ch and ch.startswith("tip-sam-") and f"subscribe to:  {ch}" in out
    assert cli.main(["link", "sam", "my-own-channel"]) == 0 and contacts.channel_for("sam") == "my-own-channel"


def test_link_bad_channel(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(contract, "HOME", tmp_path)
    assert cli.main(["link", "sam", "no spaces allowed"]) == 1
    assert "not an ntfy channel name" in capsys.readouterr().err
