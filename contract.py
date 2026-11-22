"""tip shared contract. Owner: Matthew. Tell the group before changing anything here.

Everyone codes against this file. `TIP_FAKE=1` swaps every module for the fakes at
the bottom, so the full flow runs before any real code exists. `TIP_FAKE=eleven,audio`
fakes only the modules named, for mixing real and fake parts during integration.

Owners:  contract/tipd/cli/stepup: Matthew
         eleven: Alex   audio/voice: Terry   pay/ledger/passport: Jasper

Audio everywhere is WAV bytes, 16 kHz, mono, 16-bit PCM.
Money everywhere is integer cents.

tipd HTTP API (JSON in, JSON out, default http://127.0.0.1:7777):
  POST /tip            TipRequest            -> TipResult   (blocks up to TIMEOUT_S)
  GET  /tip/{id}                             -> TipResult
  POST /passport       PassportRequest       -> {"status": "issued", "token": str, "passport": Passport}
                                                or a TipResult-shaped failure (Voice ID first)
  POST /enroll         {"samples": 3}        -> {"status": "enrolled"} (tipd owns the mic)
  GET  /verify/{id}                          -> {"ok": bool, "entry": LedgerEntry | None}
  GET  /health                               -> {"ok": true, "fake": [module names]}
  Errors: {"status": "error", "message": str}
"""
from __future__ import annotations

import importlib
import os
import time
import types
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, Optional

# ---------------------------------------------------------------- config
TIPD_HOST = os.environ.get("TIPD_HOST", "127.0.0.1")
TIPD_PORT = int(os.environ.get("TIPD_PORT", "7777"))
TIPD_URL = f"http://{TIPD_HOST}:{TIPD_PORT}"
TIMEOUT_S = 60          # whole payment, including Voice ID
RECORD_S = 4.0          # Voice ID recording length
SAMPLE_RATE = 16000
CURRENCY = "usd"
def _me() -> str:
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return "you"


OWNER = os.environ.get("TIP_OWNER") or _me()  # whoever runs tip; TIP_OWNER to show another name
HOME = Path(os.environ.get("TIP_HOME", str(Path.home() / ".tip")))  # voiceprint, ledger, keys


class Exit:
    PAID = 0
    ERROR = 1
    DENIED = 2         # passport forged/expired, unknown recipient, bad request
    VOICE_FAILED = 3   # wrong words or wrong voice
    TIMEOUT = 4        # nobody answered within TIMEOUT_S


Status = Literal["paid", "denied", "voice_failed", "timeout", "error", "pending"]
Via = Literal["passport", "voice"]

_EXIT_FOR = {"paid": Exit.PAID, "denied": Exit.DENIED, "voice_failed": Exit.VOICE_FAILED,
             "timeout": Exit.TIMEOUT, "error": Exit.ERROR, "pending": Exit.ERROR}


def normalize_to(who: str) -> str:
    """'@Sam' -> 'sam'. The CLI accepts both forms (PowerShell eats a bare @sam)."""
    return who.strip().lstrip("@").lower()


# ---------------------------------------------------------------- types
@dataclass
class TipRequest:
    to: str                         # normalized, no @
    amount_cents: int
    currency: str = CURRENCY
    reason: Optional[str] = None    # --for; required when a passport is attached
    agent: Optional[str] = None     # detected or --agent; informational, the passport is the proof
    passport: Optional[str] = None  # token from TIP_PASSPORT


@dataclass
class VoiceCheck:
    passed: bool
    words_ok: bool
    voice_ok: bool
    score: Optional[float] = None       # ECAPA cosine, shown on screen always
    threshold: Optional[float] = None
    transcript: str = ""
    phrase: str = ""
    method: Literal["captcha", "ecapa"] = "ecapa"
    declined: bool = False              # the human said "decline" instead of the phrase


@dataclass
class Passport:
    id: str
    agent: str
    owner: str
    to: list[str]               # normalized recipients
    max_cents: int              # per payment
    daily_cents: int            # per UTC day
    purpose: str
    expires_at: float           # unix seconds


@dataclass
class PassportCheck:
    result: Literal["ok", "out_of_scope", "invalid"]  # out_of_scope -> Voice ID, invalid -> deny
    reason: str = ""
    passport: Optional[Passport] = None


@dataclass
class PassportRequest:
    agent: str
    to: list[str]
    max_cents: int
    daily_cents: int
    purpose: str
    expires_s: int = 24 * 3600


@dataclass
class Payment:
    id: str              # Stripe PaymentIntent id
    receipt_url: str


@dataclass
class LedgerEntry:
    id: str              # tip id, "tp_..."
    ts: float
    to: str
    amount_cents: int
    currency: str
    agent: Optional[str]
    reason: Optional[str]
    via: Via
    approver: str        # OWNER for Voice ID, passport id for passport payments
    payment_id: str
    receipt_url: str
    voice_score: Optional[float]
    prev_hash: str
    hash: str


@dataclass
class TipResult:
    status: Status
    id: Optional[str] = None
    via: Optional[Via] = None
    to: Optional[str] = None
    amount_cents: Optional[int] = None
    receipt_url: Optional[str] = None
    message: str = ""
    voice: Optional[VoiceCheck] = None
    passport: Optional[PassportCheck] = None

    @property
    def exit_code(self) -> int:
        return _EXIT_FOR[self.status]

    def to_dict(self) -> dict:
        return asdict(self)


def from_dict(cls, d: dict):
    """Rebuild a dataclass from JSON, including the nested ones in TipResult/PassportCheck."""
    d = dict(d)
    if cls is TipResult:
        if d.get("voice"):
            d["voice"] = VoiceCheck(**d["voice"])
        if d.get("passport"):
            d["passport"] = from_dict(PassportCheck, d["passport"])
    if cls is PassportCheck and d.get("passport"):
        d["passport"] = Passport(**d["passport"])
    return cls(**d)


# ---------------------------------------------------------------- interfaces
# Each real module exposes exactly these names. Signatures are the contract.
#
# eleven.py (Alex)
#   speak(text: str) -> bytes                     TTS, eleven_flash_v2_5, WAV 16k mono
#   transcribe(wav: bytes) -> str                 Scribe, scribe_v2
#   phrase_match(said: str, expected: str) -> bool  forgiving: case, punctuation, "17" == "seventeen"
#   captcha_text() -> Optional[str]               PVC captcha text to read; None = captcha unavailable
#   captcha_check(wav: bytes) -> Optional[bool]   None = captcha unavailable, fall back to ECAPA
#   clone(name: str, wavs: list[bytes]) -> str    instant voice clone, returns voice_id
#   synth(voice_id: str, text: str) -> bytes      speech in the cloned voice, WAV 16k mono
#
# audio.py (Terry)
#   record(seconds: float) -> bytes               blocking, WAV 16k mono
#   play(wav: bytes) -> None                      blocking until playback ends
#   beep() -> None                                short tone, blocking
#
# voice.py (Terry)
#   THRESHOLD: float                              set from calibration at 13:00
#   enroll(wavs: list[bytes]) -> None             saves the voiceprint under HOME
#   is_enrolled() -> bool
#   score(wav: bytes) -> float                    cosine vs voiceprint; passes if >= THRESHOLD
#
# pay.py (Jasper)
#   charge(to: str, amount_cents: int, currency: str, description: str) -> Payment
#
# ledger.py (Jasper)
#   append(entry: dict) -> LedgerEntry            all LedgerEntry fields except prev_hash/hash
#   get(tip_id: str) -> Optional[LedgerEntry]
#   verify(tip_id: str) -> bool                   hash chain intact from the start up to this entry
#   spent_today(passport_id: str) -> int          cents paid today under this passport
#
# passport.py (Jasper)
#   issue(req: PassportRequest) -> tuple[str, Passport]   (token, passport); Alter or local HMAC
#   check(token: str, to: str, amount_cents: int, spent_today_cents: int) -> PassportCheck
#        invalid: bad signature, expired, unreadable
#        out_of_scope: recipient not allowed, over max_cents, over daily_cents

MODULES = ("eleven", "audio", "voice", "pay", "ledger", "passport")


def _fake_names() -> set[str]:
    v = os.environ.get("TIP_FAKE", "").strip()
    if v in ("", "0"):
        return set()
    if v == "1":
        return set(MODULES)
    return {n.strip() for n in v.split(",") if n.strip()}


def is_fake(name: str) -> bool:
    return name in _fake_names()


def load(name: str):
    """tipd calls this instead of importing directly: load('eleven').speak(...)."""
    if is_fake(name):
        return FAKES[name]
    return importlib.import_module(name)


# ---------------------------------------------------------------- fakes
# Knobs for tests and rehearsal:
#   TIP_FAKE_VOICE = pass (default) | wrong_voice | wrong_words | silent
#   TIP_FAKE_CAPTCHA = 0 (default, ECAPA path) | 1 (captcha path)
import base64 as _b64
import hashlib as _hashlib
import json as _json
import uuid as _uuid

_SILENT_WAV = (b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00"
               b"\x00}\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00")


def _knob() -> str:
    return os.environ.get("TIP_FAKE_VOICE", "pass")


def _fake_eleven():
    m = types.SimpleNamespace()
    m.speak = lambda text: _SILENT_WAV
    m.transcribe = lambda wav: "" if _knob() == "silent" else "fake transcript"
    m.phrase_match = lambda said, expected: _knob() not in ("wrong_words", "silent")
    m.captcha_text = lambda: "fake captcha words" if os.environ.get("TIP_FAKE_CAPTCHA") == "1" else None
    m.captcha_check = lambda wav: (_knob() != "wrong_voice") if os.environ.get("TIP_FAKE_CAPTCHA") == "1" else None
    m.clone = lambda name, wavs: "fake_voice_id"
    m.synth = lambda voice_id, text: _SILENT_WAV
    return m


def _fake_audio():
    m = types.SimpleNamespace()
    m.record = lambda seconds: _SILENT_WAV
    m.play = lambda wav: None
    m.beep = lambda: None
    return m


def _fake_voice():
    m = types.SimpleNamespace(THRESHOLD=0.5)
    m.enroll = lambda wavs: None
    m.is_enrolled = lambda: True
    m.score = lambda wav: 0.21 if _knob() == "wrong_voice" else 0.83
    return m


def _fake_pay():
    m = types.SimpleNamespace()

    def charge(to, amount_cents, currency, description):
        pid = "pi_fake_" + _uuid.uuid4().hex[:12]
        return Payment(id=pid, receipt_url=f"https://pay.stripe.com/receipts/fake/{pid}")
    m.charge = charge
    return m


def _fake_ledger():
    m = types.SimpleNamespace()
    rows: list[LedgerEntry] = []

    def append(entry):
        prev = rows[-1].hash if rows else "0" * 64
        h = _hashlib.sha256((prev + _json.dumps(entry, sort_keys=True)).encode("utf-8")).hexdigest()
        e = LedgerEntry(**entry, prev_hash=prev, hash=h)
        rows.append(e)
        return e

    def get(tip_id):
        return next((r for r in rows if r.id == tip_id), None)

    def verify(tip_id):
        return get(tip_id) is not None

    def spent_today(passport_id):
        day = int(time.time() // 86400)
        return sum(r.amount_cents for r in rows
                   if r.approver == passport_id and int(r.ts // 86400) == day)
    m.append, m.get, m.verify, m.spent_today = append, get, verify, spent_today
    return m


def _fake_passport():
    """Unsigned base64 JSON. Real passports are signed; this only exercises the routing."""
    m = types.SimpleNamespace()

    def issue(req: PassportRequest):
        p = Passport(id="pp_" + _uuid.uuid4().hex[:10], agent=req.agent, owner=OWNER,
                     to=[normalize_to(t) for t in req.to], max_cents=req.max_cents,
                     daily_cents=req.daily_cents, purpose=req.purpose,
                     expires_at=time.time() + req.expires_s)
        return _b64.urlsafe_b64encode(_json.dumps(asdict(p)).encode("utf-8")).decode("ascii"), p

    def check(token, to, amount_cents, spent_today_cents):
        try:
            p = Passport(**_json.loads(_b64.urlsafe_b64decode(token.encode("ascii"))))
        except Exception:
            return PassportCheck("invalid", "unreadable passport")
        if p.expires_at < time.time():
            return PassportCheck("invalid", "passport expired", p)
        if normalize_to(to) not in p.to:
            return PassportCheck("out_of_scope", f"{to} is not on this passport", p)
        if amount_cents > p.max_cents:
            return PassportCheck("out_of_scope", f"over the {p.max_cents}c per-payment cap", p)
        if spent_today_cents + amount_cents > p.daily_cents:
            return PassportCheck("out_of_scope", f"over the {p.daily_cents}c daily cap", p)
        return PassportCheck("ok", "", p)
    m.issue, m.check = issue, check
    return m


FAKES = {"eleven": _fake_eleven(), "audio": _fake_audio(), "voice": _fake_voice(),
         "pay": _fake_pay(), "ledger": _fake_ledger(), "passport": _fake_passport()}
