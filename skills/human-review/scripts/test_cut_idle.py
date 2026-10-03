#!/usr/bin/env python3
"""cut-idle.py: the still stretches of the raw take go, and every cue moves with them.

Eval run 14's film was 2:48 for 43 s of speech — 72% silence — because the recorder films
while it synthesizes three voices and holds every shot for the slowest one. Getting the
arithmetic wrong is invisible short of watching: the caption and the voice land over the
wrong shot. So it is pinned here.
"""
from __future__ import annotations
import importlib.util
import json
import re
from pathlib import Path

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("cut_idle", HERE / "cut-idle.py")
cut = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cut)


def test_a_cue_after_a_cut_moves_back_by_the_cut():
  cuts = [(5.0, 8.0), (10.0, 11.0)]
  assert cut.remap(4.0, cuts) == 4.0
  assert cut.remap(9.0, cuts) == 6.0
  assert cut.remap(12.0, cuts) == 8.0


def test_a_moment_inside_a_cut_lands_on_its_splice():
  assert cut.remap(6.5, [(5.0, 8.0)]) == 5.0


def test_the_cut_drops_exactly_the_stretches_and_cues_follow(tmp_path):
  import subprocess
  raw = tmp_path / "raw.webm"
  subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25",
                  "-t", "10", "-c:v", "libvpx", str(raw)], check=True)
  (tmp_path / "cues.json").write_text(json.dumps([{"t": 1.0, "text": "a"}, {"t": 7.0, "text": "b"}]))
  (tmp_path / "idle.json").write_text(json.dumps([[2.0, 4.0], [5.0, 6.0]]))
  out, cues = tmp_path / "cut.mkv", tmp_path / "cut.json"
  subprocess.run(["python3", str(HERE / "cut-idle.py"), str(raw), str(tmp_path / "cues.json"),
                  str(tmp_path / "idle.json"), str(out), str(cues)], check=True)
  assert abs(cut.probe(out) - 7.0) < 0.1
  assert [c["t"] for c in json.loads(cues.read_text())] == [1.0, 4.0]


def test_overlapping_stretches_join_and_slivers_are_dropped():
  assert cut.merged([[3, 4], [1, 2], [1.5, 2.5], [6, 6.05], [9, 30]], 10.0) == \
      [(1, 2.5), (3, 4), (9, 10.0)]


def test_a_fast_voice_loses_the_wait_for_the_slow_one_never_its_own_line():
  # Shot at 10 s held 7 s for the slowest voice; this voice speaks for 4 s.
  spans = cut.voice_spans([{"t": 10.0, "speech": 4.0, "hold": 7.0},
                           {"t": 20.0, "text": "silent cue"}])
  assert spans == [(10.0 + 4.0 + cut.BEAT, 17.0)]


def test_the_recorder_keeps_the_harness_free_of_apostrophes_and_cuts_every_voice():
  sh = (HERE / "record-feature-video.sh").read_text(encoding="utf-8")
  node = sh.split("node -e '", 1)[1].split("\n' \"$BASE_URL\"", 1)[0]
  assert "'" not in node, "an apostrophe ends the single-quoted node -e block"
  assert sh.count('"$SCRIPT_DIR/cut-idle.py"') == 2, "the standard voice and each cloned one"


def test_each_voice_radio_carries_its_own_cue_times(tmp_path):
  from hrbuild.tabs import demo
  assets = tmp_path / "assets"
  assets.mkdir()
  (assets / "f.webm").write_bytes(b"\x1aE\xdf\xa3")
  (assets / "f.voice-slow.webm").write_bytes(b"\x1aE\xdf\xa3")
  (assets / "f.cues.json").write_text(json.dumps([{"t": 1.0, "text": "a"}, {"t": 4.0, "text": "b"}]))
  (assets / "f.voices.json").write_text(json.dumps(
      [{"key": "slow", "label": "Slow", "video": "f.voice-slow.webm", "t": [1.0, 6.5]}]))
  out = demo.video_html({"video": "assets/f.webm", "runtime": {}, "appLinks": []}, tmp_path)
  ts = re.findall(r'data-src="([^"]+)" data-ts="([^"]+)"', out)
  assert ts == [("assets/f.webm", "1.00,4.00"), ("assets/f.voice-slow.webm", "1.00,6.50")]
