#!/usr/bin/env python3
"""Synthesise one narration cue: a .wav, and the time every word starts inside it.

Wraps `tts-cue.swift` (AVSpeechSynthesizer — offline, on this machine, no API key and no
narration text leaving the laptop) and turns its UTF-16 word marks into times against the
caller's own whitespace tokens, which is what the karaoke captions are drawn from.

`--engine fish` speaks the cue in a cloned voice instead: the Fish Audio model `--fish-voice`
names (a community model id), else NARRATION_FISH_VOICE, else "POTUS 47 - Trump". It is never
the only voice of a film: the recorder asks for the offline voice and every cloned one, cuts
one film per voice from the same take, and the Demo tab offers them as radio buttons. The key is read from $FISH_API_KEY, else from a
`FISH_API_KEY=` line in ~/.claude/fish-audio.env — never from the repository, which is
public. With no key, or when Fish fails, this exits 3 like any other synthesis failure and
there is simply no second film. The narration text does leave the laptop on that path.

Fish returns audio and nothing else — no word marks — so on that path each word's time is
an estimate: the voiced stretch of the .wav (silence trimmed at both ends) shared out by
word length, with a breath's worth extra after punctuation. Against Whisper's word times it
drifts by ~0.4s at most over a one-line cue, which a karaoke caption absorbs. Every Fish clip
is cached by (model, voice, speed, text), so re-filming an unchanged demo costs nothing.

The synthesizer marks words, not punctuation, so a token like an em dash inherits the mark
before it; runs that share a mark are spread evenly across the gap to the next one, so two
words never pop on the same frame.

Usage:
    narrate-cue.py --text "..." --out cue03.wav [--voice Samantha] [--rate 0.5]
                   [--max-seconds N] [--engine macos|fish] [--fish-voice <model id>]

Prints {"duration":…, "voice":…, "words":[{"w":…, "t":…}, …]} on stdout. Exits 3 with
{"error":…} — never a traceback — when the machine cannot synthesise, so callers can go on
without narration.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import array
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import wave
from pathlib import Path

SRC = Path(__file__).resolve().parent / "tts-cue.swift"
# Compiling takes ~2s and running the source through `swift` pays it every cue, so the binary
# is cached outside the repo and keyed by the source it was built from.
CACHE = Path.home() / "Library" / "Caches" / "human-review"


def binary() -> Path:
  digest = hashlib.sha256(SRC.read_bytes()).hexdigest()[:12]
  exe = CACHE / f"tts-cue-{digest}"
  if exe.is_file():
    return exe
  if not shutil.which("swiftc"):
    raise RuntimeError("swiftc not found — install the Xcode command line tools")
  CACHE.mkdir(parents=True, exist_ok=True)
  for stale in CACHE.glob("tts-cue-*"):
    stale.unlink(missing_ok=True)
  subprocess.run(["swiftc", "-O", str(SRC), "-o", str(exe)], check=True,
      capture_output=True, text=True)
  return exe


def utf16_starts(text: str) -> list[tuple[str, int]]:
  """Each whitespace token with its UTF-16 offset — the unit AVSpeechSynthesizer marks in."""
  out, off = [], 0
  for chunk in text.split(" "):
    if chunk:
      out.append((chunk, off))
    off += len(chunk.encode("utf-16-le")) // 2 + 1
  return out


def align(text: str, marks: list[dict], duration: float) -> list[dict]:
  tokens = utf16_starts(text)
  marks = sorted(marks, key=lambda m: m["location"])
  times: list[float | None] = []
  for _, start in tokens:
    hit = [m["t"] for m in marks if m["location"] <= start]
    times.append(hit[-1] if hit else 0.0)

  # Tokens the synthesizer did not mark separately (punctuation, hyphenated leftovers) share
  # the previous token's time; spread each such run over the gap so they arrive one by one.
  i = 0
  while i < len(times):
    j = i
    while j + 1 < len(times) and times[j + 1] == times[i]:
      j += 1
    if j > i:
      nxt = times[j + 1] if j + 1 < len(times) else duration
      span = max(0.0, (nxt - times[i])) * 0.6
      for k in range(i + 1, j + 1):
        times[k] = times[i] + span * (k - i) / (j - i + 1)
    i = j + 1
  return [{"w": tok, "t": round(t, 3)} for (tok, _), t in zip(tokens, times)]


FISH_URL = "https://api.fish.audio/v1/tts"
FISH_MODEL = os.environ.get("NARRATION_FISH_MODEL", "s2.1-pro-free")
FISH_VOICE = os.environ.get("NARRATION_FISH_VOICE", "e58b0d7efca34eb38d5c4985e378abcb")
FISH_KEY_FILE = Path.home() / ".claude" / "fish-audio.env"


def fish_key() -> str:
  key = os.environ.get("FISH_API_KEY", "").strip()
  if not key and FISH_KEY_FILE.is_file():
    m = re.search(r"^\s*FISH_API_KEY\s*=\s*['\"]?([^'\"\s]+)", FISH_KEY_FILE.read_text(),
        re.MULTILINE)
    key = m.group(1) if m else ""
  return key


def voiced_span(wav_path: Path) -> tuple[float, float, float]:
  """(start, end, duration) of the part of a 16-bit .wav that is not silence."""
  with wave.open(str(wav_path)) as w:
    rate, ch = w.getframerate(), w.getnchannels()
    pcm = array.array("h", w.readframes(w.getnframes()))
  dur = len(pcm) / ch / rate
  step = max(1, rate * ch // 100)                          # 10 ms windows
  loud = [i for i in range(0, len(pcm), step)
      if max((abs(v) for v in pcm[i:i + step]), default=0) > 600]
  if not loud:
    return 0.0, dur, dur
  return loud[0] / ch / rate, min(dur, (loud[-1] + step) / ch / rate), dur


def estimate_words(text: str, start: float, end: float) -> list[dict]:
  tokens = [t for t in text.split(" ") if t]
  costs = []
  for tok in tokens:
    letters = sum(c.isalnum() for c in tok)
    pause = 5 if tok[-1:] in ".!?" else 3 if tok[-1:] in ",;:" or tok in ("—", "–", "-") else 0
    costs.append((letters + 1, pause))
  total = sum(a + b for a, b in costs) or 1
  t, out = start, []
  for tok, (said, pause) in zip(tokens, costs):
    out.append({"w": tok, "t": round(t, 3)})
    t += (end - start) * (said + pause) / total
  return out


def fish(text: str, out: Path, key: str, speed: float, voice: str = FISH_VOICE) -> dict:
  speed = round(min(2.0, max(0.5, speed)), 2)
  digest = hashlib.sha256(f"{FISH_MODEL}|{voice}|{speed}|{text}".encode()).hexdigest()[:16]
  cached = CACHE / "fish" / f"{digest}.wav"
  if not cached.is_file():
    body = {"text": text, "reference_id": voice, "format": "wav", "sample_rate": 44100}
    if speed != 1.0:
      body["prosody"] = {"speed": speed}
    req = urllib.request.Request(FISH_URL, data=json.dumps(body).encode(), headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "model": FISH_MODEL})
    with urllib.request.urlopen(req, timeout=90) as res:
      audio = res.read()
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(audio)
  shutil.copyfile(cached, out)
  start, end, dur = voiced_span(out)
  return {"duration": round(dur, 3), "voice": f"fish:{voice}",
      "words": estimate_words(text, start, end)}


def synthesize(text: str, out: Path, voice: str, rate: float, engine: str = "macos",
    fish_voice: str = "") -> dict:
  if engine == "fish":
    key = fish_key()
    if not key:
      raise RuntimeError("no Fish Audio key ($FISH_API_KEY or ~/.claude/fish-audio.env)")
    # The offline voice's rate 0.5 is its natural pace; Fish's natural pace is speed 1.0.
    return fish(text, out, key, rate / 0.5, fish_voice or FISH_VOICE)
  return macos(text, out, voice, rate)


def macos(text: str, out: Path, voice: str, rate: float) -> dict:
  proc = subprocess.run([str(binary()), text, str(out), voice, str(rate)],
      check=True, capture_output=True, text=True)
  payload = json.loads(proc.stdout)
  return {
    "duration": payload["duration"],
    "voice": payload["voice"],
    "words": align(text, payload["marks"], payload["duration"]),
  }


def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("--text", required=True)
  ap.add_argument("--out", required=True)
  ap.add_argument("--voice", default="Samantha")
  ap.add_argument("--rate", type=float, default=0.5,
      help="AVSpeechUtterance rate, 0..1; 0.5 is the system default")
  ap.add_argument("--max-seconds", type=float, default=0.0,
      help="if the cue would run longer, re-speak it faster (never beyond 1.35x)")
  ap.add_argument("--engine", choices=("macos", "fish"), default="macos")
  ap.add_argument("--fish-voice", default="",
      help="Fish Audio model id for --engine fish; NARRATION_FISH_VOICE when absent")
  args = ap.parse_args()

  try:
    result = synthesize(args.text, Path(args.out), args.voice, args.rate, args.engine,
        args.fish_voice)
    if args.max_seconds and result["duration"] > args.max_seconds:
      # Speeding a cue up is the lesser evil against narration bleeding over the next shot,
      # but only up to the point where the voice still sounds like it is explaining something.
      faster = min(args.rate * 1.35, args.rate * result["duration"] / args.max_seconds)
      result = synthesize(args.text, Path(args.out), args.voice, faster, args.engine,
          args.fish_voice)
  except Exception as exc:                                  # noqa: BLE001 — reported, not raised
    detail = getattr(exc, "stderr", "") or str(exc)
    print(json.dumps({"error": detail.strip()[:400]}))
    return 3
  print(json.dumps(result))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
