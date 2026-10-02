"""The page opens itself once, where the reader is — and never on a rebuild."""
from __future__ import annotations

import importlib.util
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
    monkeypatch.setattr(sr, "probe", lambda port: {"pid": 1, "served": str(tmp_path)})
    monkeypatch.setattr(sys, "argv", ["serve-review.py", str(tmp_path)])
    assert sr.main() == 0
    assert opened == []
    assert capsys.readouterr().out.strip().endswith("/review.html")
