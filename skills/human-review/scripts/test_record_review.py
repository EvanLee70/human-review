#!/usr/bin/env python3
"""`record-review.py finish`, run for real against a throwaway repository.

Two things the Copilot run had to monkey-patch around (victorrentea/human-review#2): a
harness that needs its own co-author trailer had no way to add one, and `Implements:`
named whatever commit happened to be HEAD — housekeeping that landed after the feature —
instead of the feature itself. Both are options now, and the structured report the
command writes keeps the audited range, the implementation, HEAD and the recording commit
apart.

Run with:  python3 -m pytest test_record_review.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
RR = HERE.parent.parent / "record-review" / "record-review.py"
sys.path.insert(0, str(HERE))

import review_points_schema as schema  # noqa: E402

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
ENV.pop("CLAUDE_CODE_SESSION_ID", None)

POINTS = """## Fixed

### Guard the empty list
- file: app.py:2
- source: correctness reviewer
- fixed-in: HEAD

## Ignored

### Rename the module
- file: app.py:1
- source: ticket-fit reviewer
- why: out of scope

## Assumptions

### Empty input returns zero
- file: app.py:2
- alternative: raise on empty input
- confidence: 0.6
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=ENV).stdout.strip()


def rr(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(RR), *args], cwd=repo, capture_output=True,
                          text=True, env=ENV)


@pytest.fixture
def repo(tmp_path: Path):
    """base → feature → housekeeping, and `prepare` run over the whole range."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / ".gitignore").write_text(".human-review/\n")
    (r / "README").write_text("x\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    base = git(r, "rev-parse", "HEAD")
    (r / "app.py").write_text("def total(xs):\n    return sum(xs)\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "feature")
    feature = git(r, "rev-parse", "HEAD")
    (r / "CHANGELOG").write_text("housekeeping\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "housekeeping")
    head = git(r, "rev-parse", "HEAD")
    done = rr(r, "prepare", "--base", base, "--no-ci")
    assert done.returncode == 0, done.stdout + done.stderr
    return r, base, feature, head


def test_finish_names_the_feature_commit_and_takes_extra_trailers(repo):
    r, base, feature, head = repo
    (r / "review-points.md").write_text(POINTS)
    done = rr(r, "finish", "--subject", "guard the empty list", "--reviewers", "4 subagents",
              "--implements", feature, "--harness", "copilot",
              "--commit-trailer", "Co-authored-by: Copilot <copilot@example.com>")
    assert done.returncode == 0, done.stdout + done.stderr
    msg = git(r, "log", "-1", "--format=%B")
    assert msg.startswith("[auto-fix] guard the empty list")
    assert f"Implements: {feature}" in msg, "the feature, not the housekeeping after it"
    assert f"Audited: {base}..{head}" in msg
    assert "Review-Points: review-points.md" in msg
    assert msg.rstrip().endswith("Co-authored-by: Copilot <copilot@example.com>")

    report = json.loads((r / ".human-review" / "review-points.json").read_text())
    assert schema.problems(report) == []
    prov = report["provenance"]
    assert prov["implementation"] == feature
    assert prov["auditedBase"] == base and prov["auditedHead"] == head
    assert prov["head"] == head and prov["harness"] == "copilot"
    state = json.loads((r / ".human-review" / "review" / "state.json").read_text())
    assert state["reviewCommit"] == git(r, "rev-parse", "HEAD")


def test_the_committed_record_resolves_its_own_review_commit_afterwards(repo):
    """The review commit cannot name itself; the report read back off the branch can."""
    r, base, feature, head = repo
    (r / "review-points.md").write_text(POINTS)
    assert rr(r, "finish", "--subject", "s").returncode == 0
    out = r / "again.json"
    subprocess.run([sys.executable, str(HERE / "review-points.py"), "--root", str(r),
                    "--out", str(out)], check=True, capture_output=True, env=ENV)
    prov = json.loads(out.read_text())["provenance"]
    assert prov["reviewCommit"] == git(r, "rev-parse", "HEAD")
    assert prov["implementation"] == head, "without --implements, prepare's HEAD"


def test_a_malformed_trailer_is_refused_before_anything_is_committed(repo):
    r, *_ = repo
    (r / "review-points.md").write_text(POINTS)
    before = git(r, "rev-parse", "HEAD")
    done = rr(r, "finish", "--subject", "s", "--commit-trailer", "not a trailer")
    assert done.returncode != 0 and "--commit-trailer" in done.stderr
    assert git(r, "rev-parse", "HEAD") == before


def test_a_record_that_does_not_parse_is_refused_before_anything_is_committed(repo):
    r, *_ = repo
    (r / "review-points.md").write_text(POINTS.replace("## Ignored", "## Maybe later"))
    before = git(r, "rev-parse", "HEAD")
    done = rr(r, "finish", "--subject", "s")
    assert done.returncode == 4
    assert "unknown section" in done.stdout + done.stderr
    assert git(r, "rev-parse", "HEAD") == before


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_a_declared_copilot_harness_does_not_inherit_the_parent_claude_session(monkeypatch):
    """`copilot -p` started from a Claude session inherits `CLAUDE_CODE_SESSION_ID`;
    hr-try-3 recorded a Copilot review under that Claude session's id."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("record_review", RR)
    rr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rr)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-claude")
    assert rr.session_id("copilot-cli") == ""
    assert rr.harness("copilot-cli") == "copilot-cli"
    assert rr.session_id("claude-code") == "parent-claude"
    assert rr.session_id() == "parent-claude"


@pytest.mark.parametrize("conclusion,code", [("success", 0), ("failure", 1)])
def test_ci_exits_red_so_the_review_loop_knows_it_is_not_done(tmp_path, conclusion, code):
    """`RR ci` printed findings and exited 0 either way, so nothing could loop on it."""
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=ENV)
    (repo / "a.txt").write_text("a\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=ENV)
    subprocess.run(["git", "commit", "-qm", "a"], cwd=repo, check=True, env=ENV)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    gh = bin_ / "gh"
    gh.write_text("#!/bin/sh\n"
                  "case \"$2\" in list) echo '[{\"databaseId\":1,\"name\":\"CI\","
                  f"\"status\":\"completed\",\"conclusion\":\"{conclusion}\"}}]';; "
                  "*) echo 'Spectral ##[error] bad';; esac\n")
    gh.chmod(0o755)
    env = {**ENV, "PATH": f"{bin_}:{os.environ['PATH']}"}
    r = subprocess.run([sys.executable, str(RR), "ci", "--wait-minutes", "0.1"],
                       cwd=repo, capture_output=True, text=True, env=env)
    assert r.returncode == code, r.stdout + r.stderr
