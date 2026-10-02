"""The page opens itself once, where the reader is — and never on a rebuild."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("serve_review", HERE / "serve-review.py")
sr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sr)


def _calls(monkeypatch, tmp_path, env):
    seen = []
    opener = tmp_path / "open-in-browser.py"
    opener.write_text("")
    monkeypatch.setattr(sr, "VSC_OPENER", opener)
    for k in ("TERM_PROGRAM", "VSCODE_IPC_HOOK_CLI"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sr.subprocess, "run",
                        lambda cmd, **kw: seen.append(("vscode", cmd[-1])) or
                        type("R", (), {"returncode": 0})())
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: seen.append(("browser", url)))
    sr.open_page("http://127.0.0.1:7654/review.html")
    return seen


def test_inside_vscode_the_page_goes_beside_the_code(monkeypatch, tmp_path):
    assert _calls(monkeypatch, tmp_path, {"TERM_PROGRAM": "vscode"}) == [
        ("vscode", "http://127.0.0.1:7654/review.html")]


def test_anywhere_else_the_default_browser(monkeypatch, tmp_path):
    assert _calls(monkeypatch, tmp_path, {}) == [
        ("browser", "http://127.0.0.1:7654/review.html")]


def test_a_server_already_running_opens_nothing(monkeypatch, tmp_path, capsys):
    """A rebuild reaches the open tab by itself; opening again piles up one tab per run."""
    opened = []
    monkeypatch.setattr(sr, "open_page", opened.append)
    monkeypatch.setattr(sr, "probe", lambda port: {sr.MARKER_KEY: 1, "pid": 1,
                                                    "served": str(tmp_path.resolve())})
    monkeypatch.setattr(sys, "argv", ["serve-review.py", str(tmp_path)])
    assert sr.main() == 0
    assert opened == []
    assert capsys.readouterr().out.strip().endswith("/review.html")


# ---- the same report, wherever its server landed --------------------------------------------
# Refreshing one report walked 7655 → 7656 → 7657: only the preferred port was *asked*, the
# rest were only tested for being free, so every refresh past an occupied :7654 started one
# more server. These pin the lookup; the real-socket version is in test_action_server.py.

def _world(monkeypatch, tmp_path, ports: dict, argv=()):
    """`ports` maps port → what its marker says. Records spawns, opens and kills."""
    seen = {"spawned": [], "opened": [], "killed": []}
    monkeypatch.setattr(sr, "probe", lambda port: ports.get(port))
    monkeypatch.setattr(sr, "free", lambda port: port not in ports)
    monkeypatch.setattr(sr, "open_page", seen["opened"].append)
    monkeypatch.setattr(sr.subprocess, "Popen", lambda cmd, **kw: seen["spawned"].append(cmd))
    monkeypatch.setattr(sr.os, "kill", lambda pid, sig: seen["killed"].append(pid))
    monkeypatch.setattr(sys, "argv", ["serve-review.py", str(tmp_path), "--port", "17654",
                                      *argv])
    return seen


def _ours(tmp_path, pid=42):
    return {sr.MARKER_KEY: 1, "pid": pid, "served": str(tmp_path.resolve())}


def _theirs(pid=7):
    return {sr.MARKER_KEY: 1, "pid": pid, "served": "/elsewhere/other-checkout/.human-review"}


def test_a_refresh_finds_its_own_server_past_another_checkouts(monkeypatch, tmp_path, capsys):
    seen = _world(monkeypatch, tmp_path, {17654: _theirs(), 17655: _theirs(8),
                                          17656: _ours(tmp_path)})
    assert sr.main() == 0
    assert capsys.readouterr().out.strip() == "http://127.0.0.1:17656/review.html"
    assert seen == {"spawned": [], "opened": [], "killed": []}


def test_the_recorded_port_is_asked_first_even_outside_the_range(monkeypatch, tmp_path, capsys):
    (tmp_path / sr.IDENTITY_FILE).write_text(json.dumps({"port": 18999, "pid": 42}))
    seen = _world(monkeypatch, tmp_path, {17654: _theirs(), 18999: _ours(tmp_path)})
    assert sr.main() == 0
    assert capsys.readouterr().out.strip() == "http://127.0.0.1:18999/review.html"
    assert seen["spawned"] == [] and seen["opened"] == []


def test_a_stale_identity_file_is_a_hint_not_a_proof(monkeypatch, tmp_path, capsys):
    """The file names :18999, but that port now answers for another checkout. The marker
    decides, so a new server starts on the next free port instead."""
    (tmp_path / sr.IDENTITY_FILE).write_text(json.dumps({"port": 18999, "pid": 42}))
    seen = _world(monkeypatch, tmp_path, {17654: _theirs(), 18999: _theirs(9)})
    monkeypatch.setattr(sr, "probe", lambda port, _p={17654: _theirs(), 18999: _theirs(9)}:
                        _p.get(port) or (_ours(tmp_path) if seen["spawned"] else None))
    assert sr.main() == 0
    assert capsys.readouterr().out.strip() == "http://127.0.0.1:17655/review.html"
    assert len(seen["spawned"]) == 1 and seen["killed"] == []


def test_a_marker_without_the_key_is_somebody_elses_server(monkeypatch, tmp_path, capsys):
    impostor = {"pid": 5, "served": str(tmp_path.resolve())}       # no humanReview key
    seen = _world(monkeypatch, tmp_path, {17654: impostor})
    monkeypatch.setattr(sr, "probe", lambda port: impostor if port == 17654 else
                        (_ours(tmp_path) if seen["spawned"] else None))
    assert sr.main() == 0
    assert capsys.readouterr().out.strip() == "http://127.0.0.1:17655/review.html"
    assert len(seen["spawned"]) == 1


def test_stop_stops_this_reports_server_and_never_another_checkouts(monkeypatch, tmp_path):
    seen = _world(monkeypatch, tmp_path, {17654: _theirs(7), 17657: _ours(tmp_path, 42)},
                  argv=["--stop"])
    assert sr.main() == 0
    assert seen["killed"] == [42]


def test_stop_with_no_server_for_this_report_kills_nothing(monkeypatch, tmp_path):
    seen = _world(monkeypatch, tmp_path, {17654: _theirs(7)}, argv=["--stop"])
    assert sr.main() == 0
    assert seen["killed"] == []
