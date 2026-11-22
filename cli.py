"""tip CLI. Owner: Matthew.

Talks HTTP to tipd and nothing else: no keys, no audio, no Stripe here, so an agent
running `tip` can't get around the checks tipd does.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, fields
from decimal import Decimal, InvalidOperation
from typing import Optional

import contract
from contract import PassportRequest, TipRequest, TipResult

SUBCOMMANDS = ("passport", "enroll", "verify", "status")

EXIT_HELP = """\
exit codes: 0 paid, 1 error, 2 denied, 3 voice failed, 4 timed out

other commands:
  tip passport issue --agent NAME --to a,b --max 20 --daily 50 --for PURPOSE [--expires 24h]
  tip enroll [--samples 3]
  tip verify <id>
  tip status <id>

A recipient literally named passport/enroll/verify/status must be written with @ (tip @verify 5).
In PowerShell, @name is fine quoted ('@sam') or just drop the @ (tip sam 5)."""


class _UsageError(Exception):
    pass


class _TipdDown(Exception):
    pass


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
    return (os.environ.get("TIPD_URL") or contract.TIPD_URL).rstrip("/")


def _request(method: str, path: str, body: Optional[dict] = None, timeout: float = 15):
    """Returns (http_status, parsed_json). Raises _TipdDown or TimeoutError."""
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
        raise _TipdDown() from e
    except (TimeoutError, socket.timeout):
        raise
    except (ConnectionError, OSError) as e:
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

    if not a.json:
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
        _say(format_result(result, currency))
    return result.exit_code


def _passport(argv: list[str]) -> int:
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


def _enroll(argv: list[str]) -> int:
    p = _Parser(prog="tip enroll", description="Record your voiceprint (tipd owns the mic).")
    p.add_argument("--samples", type=int, default=3)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if a.samples < 1:
        _err("tip enroll: --samples must be at least 1")
        return contract.Exit.ERROR
    if not a.json:
        _err(f"recording {a.samples} samples... follow the prompts from the speakers")
    timeout = a.samples * (contract.RECORD_S + 15) + 60
    try:
        _, body = _request("POST", "/enroll", {"samples": a.samples}, timeout=timeout)
    except TimeoutError:
        return _emit(TipResult(status="timeout", message="enrollment timed out"), a.json)
    if body.get("status") == "enrolled":
        print(json.dumps(body) if a.json else "enrolled: your voiceprint is saved")
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


_DISPATCH = {"passport": _passport, "enroll": _enroll, "verify": _verify, "status": _status}


def main(argv: Optional[list[str]] = None) -> int:
    _fix_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
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
    except KeyboardInterrupt:
        _err("cancelled")
        return contract.Exit.ERROR


def entry() -> None:
    sys.exit(main())


if __name__ == "__main__":
    sys.exit(main())
