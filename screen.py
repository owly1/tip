"""tipd's console: the screen judges watch during the demo. Owner: Matthew.

Monochrome on purpose: bold, dim and reverse video only. No screen at all when stdout
isn't a terminal (tests, pipes): then only `plain()` log lines print. ANSI is also off
with NO_COLOR, or on Windows when VT mode can't be enabled. Nothing here may raise.
"""
from __future__ import annotations

import math
import textwrap
import os
import re
import sys
import threading
import time

W = 62  # inner width of a card

_LOCK = threading.RLock()
_ASCII = str.maketrans({"█": "#", "░": "-", "┌": "+", "┐": "+", "└": "+", "┘": "+", "─": "-",
                        "│": "|", "●": "*", "○": "o", "✓": "ok", "✗": "x", "→": "->",
                        "…": "...", "·": "-", "“": '"', "”": '"'})
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_vt_ok: bool | None = None
_countdown: "_Countdown | None" = None


# ---------------------------------------------------------------- terminal plumbing
def _tty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _enable_vt() -> bool:
    global _vt_ok
    if _vt_ok is None:
        _vt_ok = True
        if os.name == "nt":
            try:
                import ctypes
                k32 = ctypes.windll.kernel32
                h = k32.GetStdHandle(-11)
                mode = ctypes.c_uint32()
                _vt_ok = bool(k32.GetConsoleMode(h, ctypes.byref(mode))) and \
                    bool(k32.SetConsoleMode(h, mode.value | 0x0004))
            except Exception:
                _vt_ok = False
    return _vt_ok


def on() -> bool:
    """Is the card screen shown at all?"""
    return _tty()


def _ansi() -> bool:
    return _tty() and not os.environ.get("NO_COLOR") and _enable_vt()


def _sty(text: str, *codes: str) -> str:
    if not codes or not _ansi():
        return text
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"


def bold(t): return _sty(t, "1")
def dim(t): return _sty(t, "2")
def rev(t): return _sty(t, "1", "7")


def _unicode_ok() -> bool:
    try:
        "█░┌─│●✓→…·".encode(sys.stdout.encoding or "ascii")
        return True
    except Exception:
        return False


def _write(s: str) -> None:
    with _LOCK:
        try:
            if not _unicode_ok():
                s = s.translate(_ASCII)
            try:
                sys.stdout.write(s)
            except UnicodeEncodeError:
                sys.stdout.write(s.translate(_ASCII).encode("ascii", "replace").decode("ascii"))
            sys.stdout.flush()
        except Exception:
            pass


def _vis(s: str) -> int:
    return len(_ANSI_RE.sub("", s))


def _row(text: str = "") -> str:
    pad = W - _vis(text)
    return f"│ {text}{' ' * pad} │" if pad >= 0 else f"│ {text}"


def _rows(text: str, style=None) -> list[str]:
    """Wrap plain text to the card width (URLs break anywhere); styled text isn't wrapped."""
    if _ANSI_RE.search(text) or len(text) <= W:
        return [_row(style(text) if style else text)]
    parts = textwrap.wrap(text, W, subsequent_indent="  ", break_long_words=True,
                          break_on_hyphens=False) or [""]
    return [_row(style(p) if style else p) for p in parts]


def plain(text: str) -> None:
    """A one-line log, only when there's no screen (piped / tests)."""
    if not on():
        _write(text + "\n")


def money(cents) -> str:
    try:
        return f"${int(cents) / 100:,.2f}"
    except Exception:
        return str(cents)


def _cents_to_dollars(text: str) -> str:
    return re.sub(r"\b(\d+)c\b", lambda m: money(int(m.group(1))), text or "")


# ---------------------------------------------------------------- cards
def banner(url: str, fake: list[str]) -> None:
    if not on():
        plain(f"tipd listening on {url}")
        plain(f"fake modules: {', '.join(fake) if fake else 'none'}")
        return
    _write("\n" + rev(f" tip · tipd on {url} ".ljust(W + 4)) + "\n")
    _write(dim(f"  fake: {', '.join(fake)}" if fake else "  all real") + "\n\n")


def card_open(title: str, lines: list[str] = ()) -> None:
    if not on():
        return
    out = ["┌" + "─" * (W + 2) + "┐",
           "│" + rev(" " + title[:W].ljust(W) + " ") + "│"]
    out += [r for l in lines for r in _rows(l)]
    _write("\n".join(out) + "\n")


def line(text: str) -> None:
    if on():
        _write("\n".join(_rows(text)) + "\n")


def verdict(ok: bool, text: str) -> None:
    if not on():
        return
    label = f"  {text}  "
    label = label if len(label) <= W else label[: W - 3] + "…  "
    _write(_row() + "\n" + _row(rev(label)) + "\n")


def card_close(result_text: str = "", url: str = "") -> None:
    if not on():
        return
    stop_countdown()
    out = []
    if result_text:
        out += _rows(result_text)
    if url:
        out += _rows("receipt " + url, dim)
    out.append("└" + "─" * (W + 2) + "┘")
    _write("\n".join(out) + "\n\n")


# ---------------------------------------------------------------- voice ID stages
def speaking() -> None:
    line(dim("speaking challenge…"))


def say(phrase: str) -> None:
    line("say:  " + bold(f"“{phrase.upper()}”"))


class _Countdown(threading.Thread):
    def __init__(self, seconds: float):
        super().__init__(daemon=True)
        self.seconds, self.stop = seconds, threading.Event()
        self.t0 = time.monotonic()

    def _draw(self, left: float, final: bool = False) -> None:
        if final:
            text = dim(f"● recorded {self.seconds:g}s")
        else:
            dot = "●" if int((time.monotonic() - self.t0) * 2) % 2 == 0 else "○"
            done = min(1.0, (time.monotonic() - self.t0) / max(self.seconds, 0.01))
            n = 20
            bar = "█" * round(done * n) + "░" * (n - round(done * n))
            text = bold(f"{dot} listening {math.ceil(max(left, 0)):d}s") + "  " + dim(bar)
        clear = "\x1b[2K" if _ansi() else ""
        _write("\r" + clear + _row(text) + ("\n" if final else ""))

    def run(self) -> None:
        while not self.stop.wait(0.1):
            self._draw(self.seconds - (time.monotonic() - self.t0))

    def finish(self) -> None:
        self.stop.set()
        self.join(timeout=1)
        self._draw(0, final=True)


def listening(seconds: float) -> None:
    global _countdown
    if not on():
        return
    stop_countdown()
    _countdown = _Countdown(seconds)
    _countdown._draw(seconds)
    _countdown.start()


def done_listening() -> None:
    stop_countdown()


def stop_countdown() -> None:
    global _countdown
    c, _countdown = _countdown, None
    if c is not None:
        try:
            c.finish()
        except Exception:
            pass


def heard(transcript: str, words_ok: bool) -> None:
    mark = "✓" if words_ok else "✗"
    line(f"heard: “{transcript}”"[: W - 10] + f"  words {mark}")


def score_bar(score, threshold: float, voice_ok: bool, method: str, captcha=None) -> None:
    if not on():
        return
    if method == "captcha" and captcha is not None:
        line(f"elevenlabs captcha  {'✓' if captcha else '✗'}")
    if score is None:
        line("voice  " + dim("no voiceprint: run `tip enroll`"))
        return
    n = 25
    s = min(max(float(score), 0.0), 1.0)
    cells = ["█" if i < round(s * n) else "░" for i in range(n)]
    cells.insert(min(max(round(threshold * n), 0), n), "|")
    mark = "✓" if voice_ok else "✗"
    line(f"voice  [{''.join(cells)}] {bold(f'{score:.2f}')}  threshold {threshold:.2f}  {mark}")
