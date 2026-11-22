"""One terminal: with no tipd running, `tip` runs the pipeline itself; short output by default, --dev for detail."""
from __future__ import annotations

import json
import os
import socket
import stat
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cli  # noqa: E402
import contract  # noqa: E402
import screen  # noqa: E402


@pytest.fixture
def local(monkeypatch, tmp_path):
    """No tipd anywhere: a closed port, no TIPD_URL, fakes on, a throwaway HOME, the default short output."""
    for k in ("TIPD_URL", "TIP_DEV", "TIP_FAKE_VOICE", "TIP_PASSPORT", "CLAUDECODE", "CURSOR_AGENT", "CURSOR_TRACE_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TIP_FAKE", "1")
    monkeypatch.setattr(contract, "HOME", tmp_path)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setattr(contract, "TIPD_URL", f"http://127.0.0.1:{port}")
    yield tmp_path
    screen.OUT, screen.QUIET = None, False


def test_pays_in_this_process_and_json_stays_clean(local, capsys):
    assert cli.main(["sam", "5", "--json"]) == 0
    out, err = capsys.readouterr()
    d = json.loads(out)  # stdout is exactly one JSON object; the screen went to stderr
    assert d["status"] == "paid" and d["via"] == "voice"
    assert "Verification required: speak" in err
    assert cli._LOCAL is None  # nothing left running
    assert not [t for t in threading.enumerate() if t.name.startswith("tip-")]


def test_short_output_by_default(local, capsys):
    assert cli.main(["sam", "5"]) == 0
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert lines[0] == "✓ transfer approved  $5.00 → @sam"  # plain when not a terminal
    assert lines[1].split() == ["receipt", lines[1].split()[1]] and lines[1].split()[1].startswith("https://")
    assert lines[2].split()[:2] == ["tip", "id"] and lines[2].split()[2].startswith("tp_")
    assert err.strip().startswith("Verification required: speak ")
    assert "score" not in out + err  # details are --dev only


@pytest.mark.parametrize("knob,line", [("wrong_voice", "✗ invalid voice"), ("wrong_words", "✗ invalid words"),
                                       ("silent", "✗ invalid words")])
def test_short_failures(local, monkeypatch, capsys, knob, line):
    monkeypatch.setenv("TIP_FAKE_VOICE", knob)
    assert cli.main(["sam", "5"]) == 3
    assert capsys.readouterr().out.strip() == line


def test_dev_shows_details(local, capsys):
    assert cli.main(["--dev", "sam", "5"]) == 0
    out = capsys.readouterr().out
    assert "paid $5.00 to @sam via voice" in out and "voice score 0.83 >= 0.50" in out


def test_status_from_another_tip_process_reads_the_ledger(local, capsys):
    cli.main(["sam", "5", "--json"])
    tip_id = json.loads(capsys.readouterr().out)["id"]
    assert cli.main(["status", tip_id, "--json"]) == 0  # a fresh local server: not in memory, found in the ledger
    d = json.loads(capsys.readouterr().out)
    assert d["status"] == "paid" and d["id"] == tip_id


def test_agent_passport_in_one_terminal(local, monkeypatch, capsys):
    assert cli.main(["passport", "claude-code", "sam", "max-20", "--json"]) == 0
    token = json.loads(capsys.readouterr().out)["token"]
    monkeypatch.setenv("TIP_PASSPORT", token)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert cli.main(["sam", "5", "--for", "lunch", "--json"]) == 0
    out, err = capsys.readouterr()
    d = json.loads(out)
    assert d["via"] == "passport" and "Verification required" not in err  # in scope: no voice


def test_explicit_tipd_url_is_not_replaced_by_a_local_one(local, monkeypatch, capsys):
    monkeypatch.setenv("TIPD_URL", contract.TIPD_URL)
    assert cli.main(["sam", "5"]) == 1
    assert "tipd isn't running" in capsys.readouterr().err


# ---------------------------------------------------------------- tip enroll: keys, then voice
def test_enroll_asks_for_keys_checks_and_saves_them(local, monkeypatch, capsys):
    answers = iter(["sk_live_nope", "sk_test_good", "el_key_123"])
    asked = []
    monkeypatch.setattr("getpass.getpass", lambda prompt: asked.append(prompt) or next(answers))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_load_env", lambda: None)
    monkeypatch.setattr(cli, "_check_key", lambda name, v: None if v != "sk_live_nope" else "not a test key")
    for k in ("STRIPE_SECRET_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert cli.main(["enroll"]) == 0
    env_file = local / ".env"
    assert env_file.read_text(encoding="utf-8").splitlines() == ["STRIPE_SECRET_KEY=sk_test_good",
                                                                  "ELEVENLABS_API_KEY=el_key_123"]
    if os.name != "nt":
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert len(asked) == 3  # the live key was refused and asked again
    out, err = capsys.readouterr()
    assert "not a test key" in err and "tip sam 5" in out


def test_enroll_skips_keys_already_set_and_never_prompts_agents(local, monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda prompt: pytest.fail("should not prompt"))
    monkeypatch.setattr(cli, "_load_env", lambda: None)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    assert cli.main(["enroll"]) == 0
    monkeypatch.delenv("STRIPE_SECRET_KEY")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)  # an agent or a pipe
    assert cli.main(["enroll", "--json"]) == 0


def test_save_key_replaces_the_old_value(local):
    cli._save_key("STRIPE_SECRET_KEY", "sk_test_old")
    cli._save_key("OTHER", "x")
    cli._save_key("STRIPE_SECRET_KEY", "sk_test_new")
    assert (local / ".env").read_text(encoding="utf-8").splitlines() == ["OTHER=x", "STRIPE_SECRET_KEY=sk_test_new"]


def test_a_dropped_connection_is_never_rerun_locally(local, monkeypatch, capsys):
    """tipd took the request then dropped the line: it may have paid, so tip must not pay again here."""
    import urllib.request
    calls = []

    def dropped(req, timeout=None):
        calls.append(req.full_url)
        raise urllib.error.URLError(ConnectionResetError("reset by peer"))
    monkeypatch.setattr(urllib.request, "urlopen", dropped)
    assert cli.main(["sam", "5"]) == 1
    assert len(calls) == 1 and cli._LOCAL is None


def test_prints_from_the_pipeline_never_reach_json_stdout(local, monkeypatch, capsys):
    import contract as c
    real = c.FAKES["audio"].record
    monkeypatch.setattr(c.FAKES["audio"], "record", lambda s: print("[audio] mic: Test Mic") or real(s))
    assert cli.main(["sam", "5", "--json"]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["status"] == "paid" and "[audio] mic" in err


@pytest.mark.skipif(os.name == "nt", reason="flock")
def test_only_one_voice_id_at_a_time_across_processes(local):
    """Two one-terminal tips must never talk and listen over each other."""
    import fcntl
    import stepup
    other = open(local / "mic.lock", "a+b")  # another tip process holding the mic
    fcntl.flock(other, fcntl.LOCK_EX)
    with pytest.raises(TimeoutError, match="another tip is using the mic"):
        with stepup._mic(timeout=0.3):
            pass
    other.close()  # it finished
    with stepup._mic(timeout=0.3):
        pass
