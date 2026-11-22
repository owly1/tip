"""`tip doctor`: what's real, what's fake, what's missing. Owner: Matthew.

Runs on the machine that runs tipd. Never prints key values, only whether they're set.
Exit 0 when nothing is broken (warnings are fine), 1 otherwise.
"""
from __future__ import annotations

import importlib
import inspect
import json
import os
import platform
import sys
import urllib.request
from pathlib import Path

import contract

OK, WARN, BAD = "ok", "warn", "BAD"
_MARK = {OK: "✓", WARN: "!", BAD: "✗"}
_ASCII = {OK: "ok", WARN: " !", BAD: " x"}

# function names each teammate module must provide (from contract.py's interfaces block)
INTERFACES = {
    "eleven": ("Alex", ["speak", "transcribe", "phrase_match", "captcha_text", "captcha_check", "clone", "synth"]),
    "audio": ("Terry", ["record", "play", "beep"]),
    "voice": ("Terry", ["enroll", "is_enrolled", "score"]),
    "pay": ("Jasper", ["charge"]),
    "ledger": ("Jasper", ["append", "get", "verify", "spent_today"]),
    "passport": ("Jasper", ["issue", "check"]),
}

KEYS = [  # (name, severity if missing, what it's for)
    ("ELEVENLABS_API_KEY", BAD, "TTS, Scribe, clone"),
    ("ELEVENLABS_VOICE_ID", WARN, "PVC captcha (else ECAPA only)"),
    ("STRIPE_SECRET_KEY", BAD, "test payments"),
    ("ALTER_API_KEY", WARN, "Alter passports (else local HMAC)"),
    ("TIP_PASSPORT_KEY", WARN, "local passport signing (generated if missing)"),
]


def _env_values() -> tuple[dict, Path | None]:
    for p in (Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"):
        if p.is_file():
            try:
                from dotenv import dotenv_values
                vals = dict(dotenv_values(p, encoding="utf-8"))
            except ImportError:
                vals = {}
                for line in p.read_text(encoding="utf-8").splitlines():
                    if "=" in line and not line.lstrip().startswith("#"):
                        k, v = line.split("=", 1)
                        vals[k.strip()] = v.strip().strip('"').strip("'")
            names = {k for k, _, _ in KEYS}
            return {**vals, **{k: v for k, v in os.environ.items() if k in names}}, p
    return {k: v for k, v in os.environ.items()}, None


def check_keys() -> list[tuple[str, str, str]]:
    vals, path = _env_values()
    out = [(OK, ".env", str(path)) if path else (BAD, ".env", "missing: cp .env.example .env")]
    for name, sev, why in KEYS:
        v = (vals.get(name) or "").strip()
        if not v or v in ("sk_test_",):
            out.append((sev, name, f"not set ({why})"))
        elif name == "STRIPE_SECRET_KEY" and v.startswith(("sk_live_", "rk_live_")):
            out.append((BAD, name, "LIVE key! use a test key (sk_test_...)"))
        elif name == "STRIPE_SECRET_KEY" and not v.startswith(("sk_test_", "rk_test_")):
            out.append((WARN, name, "doesn't look like a Stripe test key"))
        else:
            out.append((OK, name, "set"))
    return out


def _is_stub(fn) -> bool:
    try:
        return "raise NotImplementedError" in inspect.getsource(fn)
    except (OSError, TypeError):
        return False


def check_modules() -> list[tuple[str, str, str]]:
    out = []
    for name, (owner, fns) in INTERFACES.items():
        label = f"{name}.py ({owner})"
        try:
            mod = importlib.import_module(name)
        except Exception as e:  # noqa: BLE001
            out.append((BAD, label, f"import fails: {type(e).__name__}: {e}"))
            continue
        missing = [f for f in fns if not callable(getattr(mod, f, None))]
        stubs = [f for f in fns if f not in missing and _is_stub(getattr(mod, f))]
        fake = " · TIP_FAKE on" if contract.is_fake(name) else ""
        if missing:
            out.append((BAD, label, f"missing {', '.join(missing)}"))
        elif len(stubs) == len(fns):
            out.append((WARN, label, f"stub, not started{fake}"))
        elif stubs:
            out.append((WARN, label, f"partial: still stubbed {', '.join(stubs)}{fake}"))
        else:
            out.append((OK, label, f"implemented{fake}"))
    return out


def check_tipd() -> list[tuple[str, str, str]]:
    url = (os.environ.get("TIPD_URL") or contract.TIPD_URL).rstrip("/")
    try:
        with urllib.request.urlopen(url + "/health", timeout=2) as r:
            body = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return [(WARN, "tipd", f"not running at {url} (start it with: tipd)")]
    fake = body.get("fake") or []
    return [(OK, "tipd", f"running at {url}"),
            (WARN if fake else OK, "tipd modules", "fake: " + ", ".join(fake) if fake else "all real")]


def check_audio(listen: bool = True) -> list[tuple[str, str, str]]:
    try:
        import numpy as np
        import sounddevice as sd
    except Exception as e:  # noqa: BLE001
        return [(BAD, "audio", f"sounddevice/numpy missing: {e}")]
    out = []
    try:
        dev_in = sd.query_devices(kind="input")
        out.append((OK, "mic", dev_in["name"]))
    except Exception as e:  # noqa: BLE001
        return [(BAD, "mic", f"no input device: {e}")]
    try:
        out.append((OK, "speakers", sd.query_devices(kind="output")["name"]))
    except Exception as e:  # noqa: BLE001
        out.append((BAD, "speakers", f"no output device: {e}"))
    if listen:
        try:
            rec = sd.rec(int(0.5 * contract.SAMPLE_RATE), samplerate=contract.SAMPLE_RATE,
                         channels=1, dtype="float32")
            sd.wait()
            rms = float(np.sqrt(np.mean(np.square(rec)))) if rec.size else 0.0
            if rms == 0.0:
                out.append((BAD, "mic level", "pure silence: grant the terminal mic access "
                                              "(System Settings > Privacy & Security > Microphone)"))
            elif rms < 1e-4:
                out.append((WARN, "mic level", f"very quiet ({rms:.5f})"))
            else:
                out.append((OK, "mic level", f"hearing sound (rms {rms:.4f})"))
        except Exception as e:  # noqa: BLE001
            out.append((BAD, "mic level", f"can't record: {e}"))
    return out


def check_voiceprint() -> list[tuple[str, str, str]]:
    try:
        voice = contract.load("voice")
        if not contract.is_fake("voice") and _is_stub(voice.is_enrolled):
            return [(WARN, "voiceprint", "voice.py not implemented yet")]
        if voice.is_enrolled():
            return [(OK, "voiceprint", f"enrolled · threshold {getattr(voice, 'THRESHOLD', '?')}")]
        return [(WARN, "voiceprint", "not enrolled: run tip enroll")]
    except Exception as e:  # noqa: BLE001
        return [(BAD, "voiceprint", f"check failed: {e}")]


def check_misc() -> list[tuple[str, str, str]]:
    out = [(OK, "python", f"{platform.python_version()} · {platform.system()}")]
    try:
        contract.HOME.mkdir(parents=True, exist_ok=True)
        probe = contract.HOME / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        out.append((OK, "TIP_HOME", str(contract.HOME)))
    except OSError as e:
        out.append((BAD, "TIP_HOME", f"not writable: {e}"))
    out.append((OK if os.environ.get("TIP_PASSPORT") else WARN, "TIP_PASSPORT",
                "set in this shell" if os.environ.get("TIP_PASSPORT") else "not set in this shell (agents need it)"))
    return out


def run(listen: bool = True) -> list[tuple[str, str, list]]:
    return [
        ("tipd", check_tipd()),
        ("modules", check_modules()),
        ("keys", check_keys()),
        ("audio", check_audio(listen)),
        ("voice", check_voiceprint()),
        ("this machine", check_misc()),
    ]


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="tip doctor", description="Check what's real, fake or missing.")
    p.add_argument("--no-mic", action="store_true", help="don't open the mic")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    sections = run(listen=not a.no_mic)
    bad = any(sev == BAD for _, rows in sections for sev, _, _ in rows)
    if a.json:
        print(json.dumps({"ok": not bad, "sections": {
            name: [{"level": s, "check": c, "detail": d} for s, c, d in rows] for name, rows in sections}}))
        return 1 if bad else 0
    enc = (getattr(sys.stdout, "encoding", None) or "").lower()
    marks = _MARK if "utf" in enc else _ASCII
    for name, rows in sections:
        print(name)
        for sev, check, detail in rows:
            print(f"  {marks[sev]} {check:<22} {detail}")
    print("\n" + ("all good" if not bad else "something's broken (✗ above)" if marks is _MARK
                  else "something's broken (x above)"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
