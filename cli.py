"""tip CLI. Owner: Matthew.

Talks HTTP to tipd. If no tipd is running, `tip` runs tipd's server inside this one process
for this one command (one terminal, nothing left running afterwards); tipd's checks are the same.
`--dev` (or TIP_DEV=1) shows the full Voice ID screen, scores, thresholds and logs.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, fields
from decimal import Decimal, InvalidOperation
from typing import Optional

import contract
from contract import PassportRequest, TipRequest, TipResult

SUBCOMMANDS = ("passport", "enroll", "verify", "status", "doctor", "link")

EXIT_HELP = """\
exit codes: 0 paid, 1 error, 2 denied, 3 voice failed, 4 timed out

other commands:
  tip passport <agent> <name>... max-<dollars> [--expires 24h] [--for "PURPOSE"]
                            e.g. tip passport claude-code matthew sam max-50
  tip enroll [--samples 3]
  tip verify <id>
  tip status <id>
  tip doctor [--no-mic]     what's real, fake or missing (keys, mic, modules, tipd)
  tip link <name> [channel] send <name> a phone notification (ntfy app) each time you tip them
  tip --dev <who> <amount>  show the full Voice ID screen, scores and thresholds

A recipient literally named passport/enroll/verify/status/doctor/link must be written with @ (tip @verify 5).
In PowerShell, @name is fine quoted ('@sam') or just drop the @ (tip sam 5)."""


class _UsageError(Exception):
    pass


class _TipdDown(Exception):
    pass


class _TipdAbsent(_TipdDown):
    """Connection refused: nothing reached tipd, so running the command locally can't repeat it."""


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on bad usage, but 2 means 'denied' for tip. Usage errors are 1."""

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise _UsageError(message)


# ---------------------------------------------------------------- parsing helpers
def parse_amount(text: str) -> int:
    """'5', '5.5', '$5', '$1,200.00' -> cents. Raises ValueError."""
    s = text.strip().replace(",", "")
    if s.startswith("$"):
        s = s[1:]
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"not an amount: {text!r}")
    if not d.is_finite():
        raise ValueError(f"not an amount: {text!r}")
    if d <= 0:
        raise ValueError(f"amount must be more than 0: {text!r}")
    if d.as_tuple().exponent < -2:
        raise ValueError(f"at most 2 decimal places: {text!r}")
    return int(d * 100)


_DURATION = re.compile(r"^(\d+)\s*([smhd]?)$")
_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int:
    """'30m', '24h', '7d', '3600' -> seconds. Raises ValueError."""
    m = _DURATION.match(text.strip().lower())
    if not m or int(m.group(1)) <= 0:
        raise ValueError(f"not a duration (try 30m, 24h, 7d): {text!r}")
    return int(m.group(1)) * _UNIT[m.group(2)]


def detect_agent(env=None) -> Optional[str]:
    env = os.environ if env is None else env
    if env.get("CLAUDECODE") == "1":
        return "claude-code"
    if any(k.startswith("CODEX_") for k in env):
        return "codex"
    if env.get("CURSOR_AGENT") or env.get("CURSOR_TRACE_ID"):
        return "cursor"
    return None


def money(cents: Optional[int], currency: str = contract.CURRENCY) -> str:
    if cents is None:
        return "?"
    amt = f"{Decimal(cents) / 100:,.2f}"
    return f"${amt}" if currency.lower() == "usd" else f"{amt} {currency.upper()}"


# ---------------------------------------------------------------- HTTP
def tipd_url() -> str:
    return (_LOCAL_URL or os.environ.get("TIPD_URL") or contract.TIPD_URL).rstrip("/")


_DEV = False          # --dev / TIP_DEV=1
_QUIET = True         # local screen: just the phrase to speak (enrollment shows the full screen)
_LOCAL = None         # (server, thread) while this process runs the pipeline itself
_LOCAL_URL: Optional[str] = None


def _start_local() -> bool:
    """No tipd running: serve tipd's API from inside this process, for this one command."""
    global _LOCAL, _LOCAL_URL
    if _LOCAL is not None or os.environ.get("TIPD_URL"):
        return False  # an explicit TIPD_URL means that tipd, not a local one
    import screen
    screen.OUT = sys.stderr  # the human sees it; --json on stdout stays clean for agents
    screen.QUIET = _QUIET and not _DEV
    import tipd
    tipd.CANCEL.clear()
    srv = tipd.make_server("127.0.0.1", 0)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    _LOCAL, _LOCAL_URL = (srv, th), f"http://127.0.0.1:{srv.server_address[1]}"
    return True


def _finish_local() -> list:
    """Before exiting, let a payment, its ledger line and the recipient's notification finish.
    Returns the payments that went through (so Ctrl-C can report them)."""
    global _LOCAL, _LOCAL_URL
    if _LOCAL is None:
        return []
    srv, _ = _LOCAL
    import signal
    old = signal.signal(signal.SIGINT, signal.SIG_IGN)  # a 2nd Ctrl-C mid-charge would lose the ledger line
    try:
        deadline = time.monotonic() + 30
        with contextlib.redirect_stdout(sys.stderr):
            while time.monotonic() < deadline:
                left = [t for t in threading.enumerate() if t.name.startswith("tip-") and t is not threading.current_thread()]
                if not left:
                    break
                left[0].join(max(0.0, deadline - time.monotonic()))
        paid = [r for r in srv.results.values() if r.status == "paid"]
        srv.shutdown()
        srv.server_close()
    finally:
        signal.signal(signal.SIGINT, old)
    _LOCAL, _LOCAL_URL = None, None
    return paid


def _request(method: str, path: str, body: Optional[dict] = None, timeout: float = 15):
    """Returns (http_status, parsed_json). Raises _TipdDown or TimeoutError."""
    if _LOCAL is None:
        try:
            return _send(method, path, body, timeout)
        except _TipdAbsent:
            if not _start_local():
                raise
    # while tipd's code runs in this process, whatever it prints is for the human: keep --json stdout clean
    with contextlib.redirect_stdout(sys.stderr):
        return _send(method, path, body, timeout)


def _send(method: str, path: str, body: Optional[dict], timeout: float):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(tipd_url() + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, _json_body(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _json_body(e.read())
    except urllib.error.URLError as e:
        if isinstance(e.reason, (TimeoutError, socket.timeout)):
            raise TimeoutError() from e
        if isinstance(e.reason, ConnectionRefusedError):
            raise _TipdAbsent() from e
        raise _TipdDown() from e
    except (TimeoutError, socket.timeout):
        raise
    except ConnectionRefusedError as e:
        raise _TipdAbsent() from e
    except (ConnectionError, OSError) as e:  # e.g. dropped mid-request: it may have paid, never re-run
        raise _TipdDown() from e


def _json_body(raw: bytes) -> dict:
    try:
        v = json.loads(raw.decode("utf-8") or "{}")
        return v if isinstance(v, dict) else {"status": "error", "message": str(v)}
    except (ValueError, UnicodeDecodeError):
        return {"status": "error", "message": raw.decode("utf-8", "replace")[:300] or "bad response from tipd"}


_TIP_FIELDS = {f.name for f in fields(TipResult)}
_STATUSES = set(contract._EXIT_FOR)


def _as_result(body: dict) -> TipResult:
    """Any tipd body -> TipResult. Ignores unknown keys so tipd can add fields."""
    if body.get("status") not in _STATUSES:
        return TipResult(status="error", message=body.get("message") or f"unexpected reply: {body}")
    try:
        return contract.from_dict(TipResult, {k: v for k, v in body.items() if k in _TIP_FIELDS})
    except TypeError:
        return TipResult(status=body["status"], id=body.get("id"), message=body.get("message", ""))


# ---------------------------------------------------------------- output
def _voice_line(result: TipResult) -> Optional[str]:
    v = result.voice
    if v is None or v.score is None:
        return None
    op = ">=" if (v.threshold is None or v.score >= v.threshold) else "<"
    thr = f" {op} {v.threshold:.2f}" if v.threshold is not None else ""
    return f"voice score {v.score:.2f}{thr} ({v.method})"


def format_result(result: TipResult, currency: str = contract.CURRENCY) -> list[str]:
    who = f"@{result.to}" if result.to else "?"
    s = result.status
    if s == "paid":
        parts = [f"paid {money(result.amount_cents, currency)} to {who}"
                 + (f" via {result.via}" if result.via else "")]
        parts += [p for p in (result.id, result.receipt_url) if p]
        lines = [" · ".join(parts)]
    elif s == "voice_failed":
        v = result.voice
        why = result.message or "voice check failed"
        if v is not None:
            if not v.words_ok:
                why = "wrong words" + (f' (heard "{v.transcript}")' if v.transcript else " (heard nothing)")
            elif not v.voice_ok:
                why = "wrong voice"
        line = f"voice check failed: {why}"
        vl = _voice_line(result)
        if vl and why == "wrong voice":
            return [f"{line} ({vl.replace('voice score', 'score')})"]
        lines = [line]
    elif s == "denied":
        lines = [f"denied: {result.message or 'not allowed'}"]
    elif s == "timeout":
        lines = [result.message or f"timed out after {contract.TIMEOUT_S} s"]
        if not lines[0].startswith("timed out"):
            lines[0] = "timed out: " + lines[0]
    elif s == "pending":
        lines = [f"pending: {result.id or ''}".rstrip()]
    else:
        lines = [f"error: {result.message or 'unknown error'}"]
    if result.passport is not None and result.passport.result != "ok" and result.passport.reason \
            and s != "denied":
        lines.append(f"passport: {result.passport.reason}")
    vl = _voice_line(result)
    if vl and s != "voice_failed":
        lines.append(vl)
    return lines


def _styled() -> bool:
    """Colours and links only for a person at a terminal: never for agents, pipes or --json."""
    try:
        tty = sys.stdout.isatty()
    except Exception:
        return False
    return tty and not os.environ.get("NO_COLOR") and (os.name != "nt" or "WT_SESSION" in os.environ)


def _c(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if _styled() else text


_LINK_TERMINALS = {"iTerm.app", "vscode", "WezTerm", "ghostty", "Hyper", "Tabby"}


def _href(url: str, label: str) -> str:
    """A short clickable label where the terminal supports links (OSC 8), else the whole URL on its own."""
    if _styled() and (os.environ.get("TERM_PROGRAM") in _LINK_TERMINALS
                      or "WT_SESSION" in os.environ or "KITTY_WINDOW_ID" in os.environ):
        return f"\x1b]8;;{url}\x1b\\{_c(label, '4;36')}\x1b]8;;\x1b\\"
    return url


def short_result(result: TipResult, currency: str = contract.CURRENCY) -> list[str]:
    """The default one-liner; --dev shows format_result's details."""
    s, v = result.status, result.voice
    if s == "paid":
        lines = [_c("✓ transfer approved", "1;32") + f"  {_c(money(result.amount_cents, currency), '1')} → @{result.to}"]
        if result.receipt_url:
            lines.append(f"  {_c('receipt', '2')}  {_href(result.receipt_url, 'view receipt ↗')}")
        if result.id:
            lines.append(f"  {_c('tip id', '2')}   {result.id}")
        return lines
    if s == "voice_failed" and v is not None and v.score is None and v.method == "ecapa" and v.words_ok:
        return [_c("✗ no voiceprint yet: run tip enroll", "1;31")]
    if v is not None and getattr(v, "declined", False):
        return [_c("✗ declined", "1;31")]
    if s == "voice_failed":
        if v is not None and not v.words_ok:
            return [_c("✗ invalid words", "1;31")]
        return [_c("✗ invalid voice" if v is not None and not v.voice_ok else "✗ voice check failed", "1;31")]
    if s == "denied":
        return [_c("✗ denied", "1;31") + f": {result.message or 'not allowed'}"]
    if s == "timeout":
        return [_c("✗ timed out", "1;31")]
    if s == "pending":
        return [f"… pending {result.id or ''}".rstrip()]
    return [_c("✗ error", "1;31") + f": {result.message or 'unknown error'}"]


def _say(lines, stream=None):
    stream = stream or sys.stdout
    for line in lines:
        print(line, file=stream)


def _err(msg: str):
    print(msg, file=sys.stderr)


def _fix_streams():
    """Windows consoles redirected to a file can't encode '·'. Never crash on output."""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


# ---------------------------------------------------------------- commands
def _pay(argv: list[str]) -> int:
    p = _Parser(prog="tip", usage="tip <who> <amount> [--for REASON] [--json]",
                description="Pay someone. Agents pay under their passport; anything outside it "
                            "(and every payment you make yourself) asks for your Voice ID.",
                epilog=EXIT_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("who", help="recipient, @sam or sam")
    p.add_argument("amount", help="5, 5.50, $1,200.00")
    p.add_argument("--for", dest="reason", help="why (required for agents with a passport)")
    p.add_argument("--agent", help="agent name (default: detected from the environment)")
    p.add_argument("--passport", help="passport token (default: $TIP_PASSPORT)")
    p.add_argument("--currency", default=contract.CURRENCY)
    p.add_argument("--json", action="store_true", help="print the result as JSON only")
    a = p.parse_args(argv)

    to = contract.normalize_to(a.who)
    if not to:
        _err("tip: who are you paying? (tip @sam 5)")
        return contract.Exit.ERROR
    try:
        cents = parse_amount(a.amount)
    except ValueError as e:
        _err(f"tip: {e}")
        return contract.Exit.ERROR

    passport = a.passport or os.environ.get("TIP_PASSPORT") or None
    req = TipRequest(to=to, amount_cents=cents, currency=a.currency.lower(),
                     reason=a.reason, agent=a.agent or detect_agent(), passport=passport)

    if not a.json and _DEV:
        hint = ("checking passport... (may ask for voice approval)" if passport
                else "waiting for voice approval... speak when the laptop asks")
        _err(hint)
    try:
        _, body = _request("POST", "/tip", asdict(req), timeout=contract.TIMEOUT_S + 10)
    except TimeoutError:
        result = TipResult(status="timeout", to=to, amount_cents=cents,
                           message=f"timed out after {contract.TIMEOUT_S} s")
        return _emit(result, a.json, a.currency)
    result = _as_result(body)
    if result.to is None:
        result.to = to
    if result.amount_cents is None:
        result.amount_cents = cents
    return _emit(result, a.json, a.currency)


def _emit(result: TipResult, as_json: bool, currency: str = contract.CURRENCY) -> int:
    if as_json:
        print(json.dumps(result.to_dict()))
    else:
        _say(format_result(result, currency) if _DEV else short_result(result, currency))
    return result.exit_code


def _short_passport(argv: list[str]) -> list[str]:
    """tip passport claude-code matthew sam max-50 -> the long `issue` form.
    max is the agent's budget per day, and so also its cap on any one payment."""
    p = _Parser(prog="tip passport", usage='tip passport <agent> <name>... max-<dollars> [--expires 24h] [--for "PURPOSE"] [--json]',
                description="Let an agent pay these people, up to max dollars a day, with no Voice ID.\n"
                            "Anything over that, or to anyone else, still asks for your voice.\n"
                            "Issuing the passport needs your Voice ID once.",
                epilog="example: tip passport claude-code matthew sam max-50\n"
                       "long form: tip passport issue --agent NAME --to a,b --max 20 --daily 50 --for PURPOSE",
                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("words", nargs="+", metavar="agent name... max-<dollars>")
    p.add_argument("--expires", default="24h", help="30m, 24h, 7d or seconds (default 24h)")
    p.add_argument("--for", dest="purpose", default="tips", help="what the agent may pay for (default: tips)")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    words = a.words
    # options only after max-: an unquoted `--for team lunch` among the names would turn "lunch" into a payee
    if argv[:len(words)] != words:
        p.error('put --for, --expires and --json after max-<dollars>, and quote a reason with spaces: --for "team lunch"')
    if not words[-1].lower().startswith("max-"):
        p.error("end with the most the agent may spend per day, like max-50")
    names = words[1:-1]
    if not names or any(w.lower().startswith("max-") for w in words[:-1]):
        p.error("give the agent, then at least one name, then max-<dollars>: "
                "tip passport claude-code matthew sam max-50")
    amount = words[-1][4:]
    return (["issue", "--agent", words[0], "--to", ",".join(names), "--max", amount, "--daily", amount,
             "--for", a.purpose, "--expires", a.expires] + (["--json"] if a.json else []))


def _passport(argv: list[str]) -> int:
    if argv[:1] != ["issue"]:
        argv = _short_passport(argv)
    p = _Parser(prog="tip passport", description="Issue a passport for an agent (needs your Voice ID).",
                epilog=EXIT_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="action", required=True, parser_class=_Parser)
    i = sub.add_parser("issue", help="issue a passport")
    i.add_argument("--agent", required=True)
    i.add_argument("--to", required=True, help="comma-separated recipients: @weatherapi,@sam")
    i.add_argument("--max", required=True, help="max per payment, dollars")
    i.add_argument("--daily", required=True, help="max per day, dollars")
    i.add_argument("--for", dest="purpose", required=True, help="what the agent may pay for")
    i.add_argument("--expires", default="24h", help="30m, 24h, 7d or seconds (default 24h)")
    i.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    to = [contract.normalize_to(t) for t in a.to.split(",") if contract.normalize_to(t)]
    if not to:
        _err("tip passport: --to needs at least one recipient")
        return contract.Exit.ERROR
    try:
        req = PassportRequest(agent=a.agent, to=to, max_cents=parse_amount(a.max),
                              daily_cents=parse_amount(a.daily), purpose=a.purpose,
                              expires_s=parse_duration(a.expires))
    except ValueError as e:
        _err(f"tip passport: {e}")
        return contract.Exit.ERROR
    if req.daily_cents < req.max_cents:
        _err("tip passport: --daily is less than --max, so the per-payment cap can never be reached")

    if not a.json:
        _err("issuing a passport needs your voice... speak when the laptop asks")
    try:
        _, body = _request("POST", "/passport", asdict(req), timeout=contract.TIMEOUT_S + 10)
    except TimeoutError:
        return _emit(TipResult(status="timeout", message=f"timed out after {contract.TIMEOUT_S} s"), a.json)

    if body.get("status") != "issued" or not body.get("token"):
        return _emit(_as_result(body), a.json)

    if a.json:
        print(json.dumps(body))
        return contract.Exit.PAID
    tok = body["token"]
    pp = body.get("passport") or {}
    exp = pp.get("expires_at")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)) if exp else "?"
    _say([
        f"passport {pp.get('id', '')} for {pp.get('agent', req.agent)}: "
        f"{', '.join('@' + t for t in pp.get('to', req.to))}, up to {money(pp.get('max_cents', req.max_cents))} each, "
        f"{money(pp.get('daily_cents', req.daily_cents))}/day, expires {when}",
        f"token: {tok}",
        "give it to the agent:",
        f'  export TIP_PASSPORT="{tok}"        # mac / linux',
        f'  $env:TIP_PASSPORT="{tok}"          # PowerShell',
    ])
    return contract.Exit.PAID


_KEYS = (
    ("STRIPE_SECRET_KEY", "Stripe test secret key, sk_test_... (Stripe dashboard > Developers > API keys)"),
    ("ELEVENLABS_API_KEY", "ElevenLabs API key (elevenlabs.io > Developers > API keys)"),
)


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(contract.HOME / ".env")  # what `tip enroll` saved wins over a repo .env
    load_dotenv()


def _check_key(name: str, value: str) -> Optional[str]:
    """None if the key works; else what's wrong. Read-only calls that cost nothing."""
    if name == "STRIPE_SECRET_KEY":
        if not value.startswith(("sk_test_", "rk_test_")):
            return "that isn't a Stripe test key (it starts with sk_test_); tip never uses live keys"
        req = urllib.request.Request("https://api.stripe.com/v1/balance", headers={"Authorization": f"Bearer {value}"})
    else:
        if value.startswith(("sk_test_", "sk_live_", "rk_test_", "rk_live_")):
            return "that's a Stripe key, not an ElevenLabs key"
        req = urllib.request.Request("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": value})
    try:
        with urllib.request.urlopen(req, timeout=10):
            return None
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return "the service rejected that key (HTTP 401)"
        if e.code == 403:  # a restricted key that works but can't run this check: keep it
            _err("  (the key works but can't run this check; saving it anyway)")
        return None
    except Exception:
        _err("  (couldn't reach the service to check it; saving it anyway)")
        return None


def _save_key(name: str, value: str) -> None:
    path = contract.HOME / ".env"
    contract.HOME.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [l for l in lines if not l.startswith(name + "=")] + [f"{name}={value}"]
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # only you can read your keys
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def _setup_keys(force: bool = False) -> bool:
    """Ask for each missing key (typing is hidden), check it, save it to ~/.tip/.env."""
    import getpass
    _load_env()
    for name, what in _KEYS:
        if os.environ.get(name, "").strip() not in ("", "sk_test_") and not force:  # sk_test_ = .env.example
            continue
        for _ in range(3):
            try:
                value = getpass.getpass(f"{what} (Enter to skip): ").strip()
            except EOFError:
                value = ""
            if not value:
                _err(f"  skipped; add it later with: tip enroll --keys")
                break
            problem = _check_key(name, value)
            if problem is None:
                _save_key(name, value)
                os.environ[name] = value
                _err(f"  ✓ saved to {contract.HOME / '.env'}")
                break
            _err(f"  ✗ {problem}")
        else:
            return False
    return True


def _enroll(argv: list[str]) -> int:
    global _QUIET
    p = _Parser(prog="tip enroll", description="Set tip up: your keys (asked once), then your voiceprint.")
    p.add_argument("--samples", type=int, default=3)
    p.add_argument("--keys", action="store_true", help="enter your keys again even if they're already set")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if a.samples < 1:
        _err("tip enroll: --samples must be at least 1")
        return contract.Exit.ERROR
    if not a.json and sys.stdin.isatty() and not _setup_keys(force=a.keys):
        return contract.Exit.ERROR
    if a.keys:  # `tip enroll --keys` only updates keys; your voiceprint stays
        return contract.Exit.PAID
    _QUIET = False  # enrollment shows the full screen: which sample, the phrase, the countdown
    if not a.json:
        _err(f"recording {a.samples} samples: read each phrase after the beep. "
             "If your computer asks to use the microphone, click Allow.")
    timeout = a.samples * (contract.RECORD_S + 15) + 60
    try:
        _, body = _request("POST", "/enroll", {"samples": a.samples}, timeout=timeout)
    except TimeoutError:
        return _emit(TipResult(status="timeout", message="enrollment timed out"), a.json)
    if body.get("status") == "enrolled":
        if a.json:
            print(json.dumps(body))
        else:
            _say(["enrolled: your voiceprint is saved. You're set up.",
                  "  pay someone:              tip sam 5",
                  "  notify them on a phone:   tip link sam",
                  "  let an agent pay:         tip passport claude-code sam max-20",
                  "  see every check:          tip --dev sam 5"])
        return contract.Exit.PAID
    return _emit(_as_result(body), a.json)


def _verify(argv: list[str]) -> int:
    p = _Parser(prog="tip verify", description="Check a payment's ledger entry and hash chain (read-only).")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        code, body = _request("GET", "/verify/" + urllib.parse.quote(a.id, safe=""))
    except TimeoutError:
        _err("tip verify: tipd took too long")
        return contract.Exit.ERROR
    if body.get("status") == "error":
        if a.json:
            print(json.dumps(body))
        else:
            _err(f"error: {body.get('message', 'verify failed')}")
        return contract.Exit.ERROR
    ok = bool(body.get("ok"))
    if a.json:
        print(json.dumps(body))
        return contract.Exit.PAID if ok else contract.Exit.DENIED
    e = body.get("entry")
    if not e:
        _say([f"not ok: no ledger entry {a.id}"])
        return contract.Exit.DENIED
    detail = (f"{money(e.get('amount_cents'), e.get('currency') or contract.CURRENCY)} to @{e.get('to')} "
              f"via {e.get('via')}, approved by {e.get('approver')}")
    if e.get("agent"):
        detail += f", agent {e['agent']}"
    if e.get("reason"):
        detail += f', for "{e["reason"]}"'
    _say([f"{'ok' if ok else 'NOT ok'}: {a.id} {'chain intact' if ok else 'chain broken'} · {detail}"])
    return contract.Exit.PAID if ok else contract.Exit.DENIED


def _status(argv: list[str]) -> int:
    p = _Parser(prog="tip status", description="Look up a payment by id.")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        _, body = _request("GET", "/tip/" + urllib.parse.quote(a.id, safe=""))
    except TimeoutError:
        _err("tip status: tipd took too long")
        return contract.Exit.ERROR
    return _emit(_as_result(body), a.json)


def _doctor(argv: list[str]) -> int:
    import doctor  # local checks (keys, mic, modules); imported lazily so `tip` stays light
    return doctor.main(argv)


def _link(argv: list[str]) -> int:
    p = _Parser(prog="tip link", usage="tip link <name> [<ntfy channel>]",
                description="Send <name> a phone notification each time you tip them.\n"
                            "They install the free ntfy app and subscribe to the channel this prints.\n"
                            "Give their own channel name instead if they already have one.",
                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("who")
    p.add_argument("channel", nargs="?")
    a = p.parse_args(argv)
    import contacts  # local file only, stdlib only
    try:
        ch = contacts.link(a.who, a.channel)
    except (ValueError, OSError) as e:
        _err(f"tip link: {e}")
        return contract.Exit.ERROR
    who = contract.normalize_to(a.who)
    server = os.environ.get("TIP_NTFY_URL", "https://ntfy.sh").rstrip("/")
    _say([f"linked @{who}: every tip to them now pings ntfy channel {ch}",
          f"ask {who} to install the ntfy app and subscribe to:  {ch}",
          f"  (or open {server}/{ch} in a browser)"])
    return contract.Exit.PAID


_DISPATCH = {"passport": _passport, "enroll": _enroll, "verify": _verify, "status": _status,
             "doctor": _doctor, "link": _link}


def main(argv: Optional[list[str]] = None) -> int:
    global _DEV, _QUIET
    _fix_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    _DEV = "--dev" in argv or os.environ.get("TIP_DEV") == "1"
    if _DEV:
        os.environ["TIP_DEV"] = "1"  # so tipd's modules (narrator, ...) show their details too
    _QUIET = True
    argv = [x for x in argv if x != "--dev"]
    try:
        if argv and argv[0] in _DISPATCH:
            return _DISPATCH[argv[0]](argv[1:])
        return _pay(argv)
    except _UsageError:
        return contract.Exit.ERROR
    except SystemExit as e:  # --help
        return e.code if isinstance(e.code, int) else contract.Exit.ERROR
    except _TipdDown:
        _err("tipd isn't running. Start it with: tipd")
        return contract.Exit.ERROR
    except (KeyboardInterrupt, EOFError):
        if _LOCAL is not None:
            import tipd
            tipd.CANCEL.set()  # stop before charging if it hasn't started
            try:  # and stop talking/listening now, so a new `tip` never plays over this one
                import sounddevice
                sounddevice.stop()
            except Exception:
                pass
            paid = _finish_local()
            if paid:  # too late: it was already being charged. Say so, don't claim "cancelled"
                _say(short_result(paid[-1]))
                _err("(it was already being charged when you pressed Ctrl-C)")
                return contract.Exit.PAID
        _err("cancelled")
        return contract.Exit.ERROR
    finally:
        _finish_local()


def entry() -> None:
    sys.exit(main())


if __name__ == "__main__":
    sys.exit(main())
