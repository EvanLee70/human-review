"""A click on a guide that carries its commit asks the editor bridge for the *reviewed*
version of the file, and whatever the bridge answers goes back to the page as it is."""
from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest

from test_action_server import _call, server, srv  # noqa: F401  (fixture)

SHA = "cbaa17bdf354be94b36996c6c7efbd02e6b4c057"


def _file(root: Path) -> Path:
    f = root / "src" / "Visit.java"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("class Visit {}\n")
    return f


def test_the_bridge_refusal_reaches_the_page_with_its_prompt(server, tmp_path, monkeypatch):
    f = _file(tmp_path)
    asked, opened = [], []
    refusal = {"ok": False, "error": "no-window", "message": "No open VS Code window is on this commit",
               "prompt": "open a checkout at that commit"}
    monkeypatch.setattr(srv, "review_open", lambda *a: asked.append(a) or (409, json.dumps(refusal).encode()))
    monkeypatch.setattr(srv, "open_in_editor", lambda *a: opened.append(a))

    status, payload = _call(server, "GET", f"{srv.OPEN}?path={f}&line=3&sha={SHA}&root={tmp_path}&branch=test-pr")

    assert status == 409 and json.loads(payload) == refusal
    assert asked == [(f, 3, SHA, str(tmp_path), "test-pr")]
    # A refusal is the answer, not a cue to open the file the old way.
    assert opened == []


def test_without_a_bridge_the_file_opens_as_it_always_did(server, tmp_path, monkeypatch):
    f = _file(tmp_path)
    opened = []
    monkeypatch.setattr(srv, "review_open", lambda *a: None)
    monkeypatch.setattr(srv, "open_in_editor", lambda *a: opened.append(a))

    status, _ = _call(server, "GET", f"{srv.OPEN}?path={f}&line=3&sha={SHA}&root={tmp_path}")

    assert status == 204 and opened == [(f, 3)]


def test_a_guide_without_a_commit_never_asks_the_bridge(server, tmp_path, monkeypatch):
    f = _file(tmp_path)
    monkeypatch.setattr(srv, "review_open", lambda *a: pytest.fail("asked the bridge"))
    monkeypatch.setattr(srv, "open_in_editor", lambda *a: None)

    status, _ = _call(server, "GET", f"{srv.OPEN}?path={f}&line=3")

    assert status == 204


def _bridge(status: int, body: dict):
    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((self.path, self.headers["x-relay-token"],
                        json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, got


def test_an_old_bridge_is_skipped_for_one_that_knows_the_route(tmp_path, monkeypatch):
    registry = tmp_path / ".walkie-talkie" / "ide"
    registry.mkdir(parents=True)
    old, _ = _bridge(404, {"ok": False})
    new, got = _bridge(200, {"ok": True, "edited": True})
    try:
        for name, httpd, token in (("vscode-1.json", old, "a"), ("vscode-2.json", new, "b")):
            (registry / name).write_text(json.dumps({"port": httpd.server_address[1], "token": token}))
        monkeypatch.setattr(srv.Path, "home", classmethod(lambda cls: tmp_path))

        status, body = srv.review_open(Path("/r/src/Visit.java"), 3, SHA, "/r", "test-pr")

        assert status == 200 and json.loads(body)["edited"] is True
        assert got == [("/review-open", "b", {"file": "/r/src/Visit.java", "line": 3, "sha": SHA,
                                              "root": "/r", "branch": "test-pr"})]
    finally:
        old.shutdown()
        new.shutdown()


def test_no_bridge_listening_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(srv.Path, "home", classmethod(lambda cls: tmp_path))
    assert srv.review_open(Path("/r/x"), 1, SHA, "/r", "") is None
