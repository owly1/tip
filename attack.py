"""Live clone attack for the demo. Owner: Matthew.

A pre-made clone clip would fail the random phrase and prove nothing. A real attacker
hears the phrase and answers it in the cloned voice, so the words check passes and only
the voice check can stop it. This does that:

  terminal 1:  TIP_DEMO_ATTACK=1 tipd
  terminal 2:  python attack.py --voice-id <clone id>      (or --clone a.wav b.wav, or --replay me.wav)
  terminal 3:  tip @sam 50

It polls tipd's demo-only GET /challenge, synthesizes the phrase in the cloned voice while
the laptop is still speaking the prompt, and plays it into the mic once recording starts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

import contract

POLL_S = 0.1
PLAY_DELAY_S = 0.3


class NoEndpoint(Exception):
    pass


def fetch_challenge(url: str) -> dict:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/challenge", timeout=2) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NoEndpoint() from e
        raise


def say(msg: str) -> None:
    print(msg, flush=True)


def run(url: str, make_wav: Callable[[str], bytes], loop: bool = False,
        fetch: Callable[[str], dict] = fetch_challenge, sleep: Callable[[float], None] = time.sleep,
        max_polls: Optional[int] = None) -> int:
    """Wait for challenges and answer them. make_wav(phrase) -> WAV bytes to play.
    Returns the number of attacks played."""
    audio = contract.load("audio")
    seen_id = None
    pending: Optional[tuple[int, bytes]] = None  # (challenge id, wav) ready to play
    played = 0
    polls = 0
    while max_polls is None or polls < max_polls:
        polls += 1
        ch = fetch(url)
        cid, phase = ch.get("id"), ch.get("phase")
        if ch.get("active") and cid != seen_id and phase in ("speaking", "listening"):
            seen_id = cid
            phrase = ch.get("phrase", "")
            say(f'heard challenge "{phrase}"')
            t0 = time.perf_counter()
            wav = make_wav(phrase)
            say(f"synthesizing in cloned voice... ({time.perf_counter() - t0:.1f} s)")
            pending = (cid, wav)
            ch = fetch(url)  # synthesis took time; re-read the phase before waiting again
            phase = ch.get("phase") if ch.get("id") == cid else None
        if pending and phase == "listening" and ch.get("id") == pending[0]:
            sleep(PLAY_DELAY_S)
            say("playing into mic")
            audio.play(pending[1])
            pending = None
            played += 1
            if not loop:
                return played
        elif pending and not ch.get("active"):
            say("challenge ended before recording started; skipped")
            pending = None
        sleep(POLL_S)
    return played


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Live clone attack on tip's Voice ID (demo). Needs tipd started with "
                    "TIP_DEMO_ATTACK=1. To attack from another laptop, start tipd with "
                    "TIPD_HOST=0.0.0.0 and pass --url http://<matthew's ip>:7777.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--voice-id", help="existing ElevenLabs clone voice id")
    src.add_argument("--clone", nargs="+", metavar="WAV", help="make a clone from these recordings first")
    src.add_argument("--replay", metavar="WAV", help="replay a genuine recording instead (words should fail)")
    ap.add_argument("--url", default=os.environ.get("TIPD_URL", contract.TIPD_URL))
    ap.add_argument("--loop", action="store_true", help="keep attacking every challenge")
    a = ap.parse_args(argv)

    if a.replay:
        clip = Path(a.replay).read_bytes()
        say(f"replay mode: will play {a.replay} for every challenge")
        make_wav = lambda phrase: clip  # noqa: E731
    else:
        eleven = contract.load("eleven")
        voice_id = a.voice_id
        if a.clone:
            voice_id = eleven.clone("matthew-clone", [Path(p).read_bytes() for p in a.clone])
            say(f"cloned voice id: {voice_id}  (reuse with --voice-id {voice_id})")
        make_wav = lambda phrase: eleven.synth(voice_id, phrase)  # noqa: E731

    try:
        fetch_challenge(a.url)
    except NoEndpoint:
        say("start tipd with TIP_DEMO_ATTACK=1")
        return 1
    except (urllib.error.URLError, OSError):
        say(f"tipd isn't reachable at {a.url}")
        return 1

    say(f"waiting for a voice challenge on {a.url} ...")
    try:
        run(a.url, make_wav, loop=a.loop)
    except KeyboardInterrupt:
        pass
    except NoEndpoint:
        say("start tipd with TIP_DEMO_ATTACK=1")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
