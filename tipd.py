"""tipd: the local service that holds the keys, voiceprint and passport key. Owner: Matthew.

Routes are documented in contract.py. Stdlib only. Modules come from contract.load()
at call time so TIP_FAKE switching works.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import contract
import screen
import stepup
from contract import PassportCheck, PassportRequest, TipRequest, TipResult, normalize_to

try:  # keys live in .env; fine to run without it in fake mode
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

MIC = threading.Lock()  # one mic, one speaker: one Voice ID at a time


def dollars(cents: int) -> str:
    return f"${cents // 100}" if cents % 100 == 0 else f"${cents / 100:.2f}"


def new_id() -> str:
    return "tp_" + secrets.token_hex(5)


class TipTimeout(Exception):
    pass


# ---------------------------------------------------------------- the payment flow
class Job:
    """One /tip request. Runs in a worker thread so the handler can enforce the deadline."""

    def __init__(self, req: TipRequest, tip_id: str, deadline: float):
        self.req, self.id, self.deadline = req, tip_id, deadline
        self.timed_out = False      # set by the handler; a late finish must not charge
        self.result: Optional[TipResult] = None
        # the screen card is printed lazily (when Voice ID starts, or at the end) so two
        # requests waiting on the mic don't interleave their cards
        who = (req.agent or "").upper()
        amt = screen.money(req.amount_cents) if isinstance(req.amount_cents, int) else str(req.amount_cents)
        self.title = f"{who} WANTS TO PAY @{req.to} {amt}" if who else f"PAY @{req.to} {amt}"
        self.lines: list[str] = [f"for: {req.reason}"] if req.reason else []
        self.card_shown = False

    def show_card(self) -> None:
        if not self.card_shown:
            self.card_shown = True
            screen.card_open(self.title, self.lines)

    def left(self) -> float:
        return self.deadline - time.monotonic()

    def run(self) -> None:
        try:
            self.result = self._run()
        except TipTimeout as e:
            self.result = self._res("timeout", message=str(e))
        except Exception as e:  # never crash the server
            self.result = self._res("error", message=f"{type(e).__name__}: {e}")

    def _res(self, status, **kw) -> TipResult:
        return TipResult(status=status, id=self.id, to=self.req.to,
                         amount_cents=self.req.amount_cents, **kw)

    def _run(self) -> TipResult:
        r = self.req
        if not r.to:
            return self._res("denied", message="no recipient")
        if not isinstance(r.amount_cents, int) or isinstance(r.amount_cents, bool) or r.amount_cents <= 0:
            return self._res("denied", message="amount must be a positive number of cents")

        agent = r.agent
        check: Optional[PassportCheck] = None
        if r.passport:
            if not (r.reason or "").strip():
                return self._res("denied", message="agent payments need a reason: use --for \"<reason>\"")
            passport, ledger = contract.load("passport"), contract.load("ledger")
            check = passport.check(r.passport, r.to, r.amount_cents, 0)
            if check.result != "invalid" and check.passport is not None:
                spent = ledger.spent_today(check.passport.id)
                check = passport.check(r.passport, r.to, r.amount_cents, spent)
            pid = f"passport {check.passport.id[:7]}…" if check.passport is not None else "passport"
            if check.result == "invalid":
                self.lines.append(f"{pid} · rejected: {check.reason}")
                return self._res("denied", message=f"passport rejected: {check.reason}", passport=check)
            if check.passport is not None:
                agent = check.passport.agent
            if check.result == "ok":
                self.lines.append(f"{pid} · ok → paying")
                return self._pay(via="passport", approver=check.passport.id, agent=agent,
                                 voice=None, check=check)
            # out_of_scope -> fall through to Voice ID
            self.lines.append(f"{pid} · out of scope: {screen._cents_to_dollars(check.reason)} → voice ID")
        else:
            self.lines.append("no passport → voice ID")

        prompt = f"{contract.OWNER.title()}, {agent or 'someone'} wants to pay {r.to} {dollars(r.amount_cents)}."
        if r.reason:
            prompt += f" For: {r.reason}."
        if check is not None:
            prompt += f" That's outside its passport: {check.reason}."

        if not MIC.acquire(timeout=max(0.0, self.left())):
            raise TipTimeout("timed out waiting for the mic")
        try:
            if self.left() <= 0:
                raise TipTimeout("timed out before Voice ID")
            self.show_card()
            vc = stepup.voice_id(prompt)
        finally:
            MIC.release()
        if not vc.passed:
            why = "wrong words" if not vc.words_ok else "wrong voice"
            return self._res("voice_failed", via="voice", message=f"Voice ID failed: {why}",
                             voice=vc, passport=check)
        return self._pay(via="voice", approver=contract.OWNER, agent=agent, voice=vc, check=check)

    def _pay(self, via, approver, agent, voice, check) -> TipResult:
        r = self.req
        if self.timed_out or self.left() <= 0:
            raise TipTimeout("timed out before payment; nothing was charged")
        payment = contract.load("pay").charge(r.to, r.amount_cents, r.currency,
                                              r.reason or f"tip to {r.to}")
        contract.load("ledger").append({
            "id": self.id, "ts": time.time(), "to": r.to, "amount_cents": r.amount_cents,
            "currency": r.currency, "agent": agent, "reason": r.reason, "via": via,
            "approver": approver, "payment_id": payment.id, "receipt_url": payment.receipt_url,
            "voice_score": voice.score if voice else None,
        })
        return self._res("paid", via=via, receipt_url=payment.receipt_url,
                         message=f"paid {r.to} {dollars(r.amount_cents)} via {via}",
                         voice=voice, passport=check)


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server: "TipServer"

    def log_message(self, fmt, *args):  # we print our own one-liners
        pass

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> Optional[dict]:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            d = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send(400, {"status": "error", "message": "bad JSON"})
            return None
        if not isinstance(d, dict):
            self._send(400, {"status": "error", "message": "JSON body must be an object"})
            return None
        return d

    def _dispatch(self, method: str) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if method == "POST" and path == "/tip":
                return self._post_tip()
            if method == "GET" and path.startswith("/tip/"):
                return self._get_tip(path[len("/tip/"):])
            if method == "POST" and path == "/passport":
                return self._post_passport()
            if method == "POST" and path == "/enroll":
                return self._post_enroll()
            if method == "GET" and path.startswith("/verify/"):
                return self._get_verify(path[len("/verify/"):])
            if method == "GET" and path == "/health":
                return self._send(200, {"ok": True, "fake": sorted(n for n in contract.MODULES if contract.is_fake(n))})
            self._send(404, {"status": "error", "message": f"no route {method} {path}"})
        except Exception as e:
            print(f"[tipd] error on {method} {path}: {type(e).__name__}: {e}", flush=True)
            try:
                self._send(500, {"status": "error", "message": f"{type(e).__name__}: {e}"})
            except Exception:
                pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    # -- routes
    def _post_tip(self):
        d = self._body()
        if d is None:
            return
        deadline = time.monotonic() + float(contract.TIMEOUT_S)
        tip_id = new_id()
        try:
            amount = d.get("amount_cents")
            req = TipRequest(to=normalize_to(str(d.get("to") or "")),
                             amount_cents=amount,
                             currency=str(d.get("currency") or contract.CURRENCY),
                             reason=d.get("reason") or None, agent=d.get("agent") or None,
                             passport=d.get("passport") or None)
        except Exception as e:
            return self._send(400, {"status": "error", "message": f"bad request: {e}"})

        job = Job(req, tip_id, deadline)
        self.server.results[tip_id] = TipResult(status="pending", id=tip_id, to=req.to,
                                                amount_cents=req.amount_cents)
        t = threading.Thread(target=self._finish, args=(job,), daemon=True)
        t.start()
        t.join(max(0.0, job.left()))
        if job.result is None:
            job.timed_out = True
            res = TipResult(status="timeout", id=tip_id, to=req.to, amount_cents=req.amount_cents,
                            message=f"no answer within {contract.TIMEOUT_S}s; nothing was charged")
            self.server.results[tip_id] = res
        else:
            res = job.result
        _close_card(job, res)
        screen.plain(f"[tipd] tip {tip_id} -> {res.to} {req.amount_cents}c agent={req.agent} "
              f"passport={'yes' if req.passport else 'no'} -> {res.status}"
              + (f" via {res.via}" if res.via else "") + (f" ({res.message})" if res.message else ""))
        self._send(200, res.to_dict())

    def _finish(self, job: Job):
        job.run()
        # a late finish still lands under the id, so GET /tip/{id} tells the truth
        self.server.results[job.id] = job.result

    def _get_tip(self, tip_id: str):
        res = self.server.results.get(tip_id)
        if res is None:
            return self._send(404, {"status": "error", "message": f"no tip {tip_id}"})
        self._send(200, res.to_dict())

    def _post_passport(self):
        d = self._body()
        if d is None:
            return
        try:
            req = PassportRequest(agent=str(d["agent"]), to=[normalize_to(str(t)) for t in d["to"]],
                                  max_cents=int(d["max_cents"]), daily_cents=int(d["daily_cents"]),
                                  purpose=str(d.get("purpose") or ""),
                                  expires_s=int(d.get("expires_s") or 24 * 3600))
        except Exception as e:
            return self._send(400, {"status": "error", "message": f"bad passport request: {e}"})
        prompt = (f"{contract.OWNER.title()}, issue a passport for {req.agent}: up to "
                  f"{dollars(req.max_cents)} per payment, {dollars(req.daily_cents)} per day, "
                  f"to {', '.join(req.to)}.")
        if not MIC.acquire(timeout=float(contract.TIMEOUT_S)):
            res = TipResult(status="timeout", message="timed out waiting for the mic")
            return self._send(200, res.to_dict())
        try:
            screen.card_open(f"ISSUE PASSPORT FOR {req.agent.upper()}", [
                "to: " + ", ".join("@" + t for t in req.to),
                f"max {screen.money(req.max_cents)} per payment · {screen.money(req.daily_cents)} per day",
                *([f"purpose: {req.purpose}"] if req.purpose else []),
                f"expires in {_human_secs(req.expires_s)}"])
            vc = stepup.voice_id(prompt)
        except Exception:
            screen.card_close()
            raise
        finally:
            MIC.release()
        if not vc.passed:
            why = "wrong words" if not vc.words_ok else "wrong voice"
            screen.verdict(False, f"REFUSED: {why}")
            screen.card_close("no passport issued")
            screen.plain(f"[tipd] passport for {req.agent} -> voice_failed ({why})")
            return self._send(200, TipResult(status="voice_failed", via="voice", voice=vc,
                                             message=f"Voice ID failed: {why}").to_dict())
        token, p = contract.load("passport").issue(req)
        screen.verdict(True, "APPROVED")
        screen.card_close(f"passport {p.id} issued → {p.agent}")
        screen.plain(f"[tipd] passport {p.id} issued for {p.agent} -> {p.to} max={p.max_cents}c "
                     f"daily={p.daily_cents}c")
        self._send(200, {"status": "issued", "token": token, "passport": asdict(p)})

    def _post_enroll(self):
        d = self._body()
        if d is None:
            return
        n = int(d.get("samples") or 3)
        with MIC:
            stepup.enroll(n)
        screen.plain(f"[tipd] enrolled {n} samples")
        self._send(200, {"status": "enrolled"})

    def _get_verify(self, tip_id: str):
        ledger = contract.load("ledger")
        entry = ledger.get(tip_id)
        ok = bool(entry is not None and ledger.verify(tip_id))
        self._send(200, {"ok": ok, "entry": asdict(entry) if entry is not None else None})


def _human_secs(s: int) -> str:
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n and s % n == 0:
            return f"{s // n}{unit}"
    return f"{s}s"


def _close_card(job: Job, res: TipResult) -> None:
    """Verdict + result for one /tip. Screen only; never raises."""
    try:
        job.show_card()
        if res.status == "paid":
            screen.verdict(True, "APPROVED" if res.via == "voice" else "APPROVED BY PASSPORT")
            screen.card_close(f"paid {screen.money(res.amount_cents)} → @{res.to} · {res.id}",
                              res.receipt_url or "")
            return
        if res.status == "voice_failed":
            why = "wrong words" if res.voice is not None and not res.voice.words_ok else "wrong voice"
            screen.verdict(False, f"REFUSED: {why}")
        elif res.status == "timeout":
            screen.verdict(False, "TIMED OUT")
        elif res.status == "denied":
            screen.verdict(False, f"DENIED: {res.message}")
        else:
            screen.verdict(False, f"ERROR: {res.message}")
        screen.card_close("nothing was charged")
    except Exception:
        pass


class TipServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr):
        super().__init__(addr, Handler)
        self.results: dict[str, TipResult] = {}


def make_server(host: str = contract.TIPD_HOST, port: int = contract.TIPD_PORT) -> TipServer:
    return TipServer((host, port))


def main() -> None:
    srv = make_server()
    fake = sorted(n for n in contract.MODULES if contract.is_fake(n))
    host, port = srv.server_address[:2]
    screen.banner(f"http://{host}:{port}", fake)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
