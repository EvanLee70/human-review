#!/usr/bin/env python3
"""Cut one voice's film out of the raw feature take: the still stretches go, cues shift.

    cut-idle.py <raw.webm> <cues.json> <idle.json> <out.webm> <out-cues.json>

The recorder films while it synthesizes each line in every voice and while a script pauses,
and it holds every shot until the SLOWEST voice has said its line — so the raw take is a
frozen screen for seconds per cue (eval run 14: 120 s of silence in a 168 s film).
`idle.json` lists the stretches the recorder measured, in seconds on the cue clock. On top of
those, each cue's `hold` minus this voice's own `speech` is cut, so a fast voice is not left
waiting in silence for a slow one. `cues.json` carries the voice being cut (its `speech`);
the output cues are the same cues on the cut clock. No stretch to cut: copied unchanged.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

MIN_SPAN = 0.15   # shorter than this is not worth a splice
BEAT = 0.35       # what the recorder adds after a spoken line before the next shot


def voice_spans(cues: list[dict]) -> list[tuple[float, float]]:
  """The rest of each held shot after THIS voice has finished its line."""
  out = []
  for c in cues:
    if c.get("hold") and c.get("speech"):
      out.append((c["t"] + c["speech"] + BEAT, c["t"] + c["hold"]))
  return out


def merged(spans: list, duration: float) -> list[tuple[float, float]]:
  """Sorted, clamped to the footage, overlaps joined, slivers dropped."""
  out: list[list[float]] = []
  for a, b in sorted((max(0.0, float(a)), min(duration, float(b))) for a, b in spans):
    if b - a < MIN_SPAN:
      continue
    if out and a <= out[-1][1]:
      out[-1][1] = max(out[-1][1], b)
    else:
      out.append([a, b])
  return [(a, b) for a, b in out]


def remap(t: float, cuts: list[tuple[float, float]]) -> float:
  """Where moment `t` of the raw take lands in the cut one (a cut moment lands at its splice)."""
  gone = 0.0
  for a, b in cuts:
    if t >= b:
      gone += b - a
    elif t > a:
      gone += t - a
  return t - gone


def kept(cuts: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
  out, at = [], 0.0
  for a, b in cuts:
    if a > at:
      out.append((at, a))
    at = b
  if duration > at:
    out.append((at, duration))
  return out


def probe(video: Path) -> float:
  return float(subprocess.run(
      ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
       str(video)], capture_output=True, text=True, check=True).stdout.strip())


def main() -> int:
  raw, cues_path, idle_path, out_raw, out_cues = map(Path, sys.argv[1:6])
  cues = json.loads(cues_path.read_text(encoding="utf-8"))
  try:
    spans = json.loads(idle_path.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    spans = []
  duration = probe(raw)
  cuts = merged(list(spans) + voice_spans(cues), duration)
  if not cuts:
    shutil.copyfile(raw, out_raw)
    out_cues.write_text(json.dumps(cues, indent=1), encoding="utf-8")
    return 0
  parts = kept(cuts, duration)
  graph = "".join(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}];"
                  for i, (a, b) in enumerate(parts))
  graph += "".join(f"[v{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=1:a=0[out]"
  subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-filter_complex", graph,
                  "-map", "[out]", "-c:v", "libvpx", "-b:v", "6M", "-crf", "4",
                  "-deadline", "realtime", "-cpu-used", "8", str(out_raw)], check=True)
  for c in cues:
    c["t"] = round(remap(float(c["t"]), cuts), 3)
    c.pop("hold", None)
  out_cues.write_text(json.dumps(cues, indent=1), encoding="utf-8")
  gone = sum(b - a for a, b in cuts)
  print(f"[video] cut {gone:.1f}s of still footage ({len(cuts)} stretches): "
        f"{duration:.1f}s -> {duration - gone:.1f}s", file=sys.stderr)
  return 0


if __name__ == "__main__":
  sys.exit(main())
