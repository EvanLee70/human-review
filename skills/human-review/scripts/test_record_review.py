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


DRIFT_POINTS = """## Fixed

### Add the mean
- file: app.py:5
- source: correctness reviewer

## Ignored

### Points at nothing
- file: app.py:4
- source: ticket-fit reviewer
- why: out of scope

## Assumptions

### Empty input returns zero
- file: app.py:2
- alternative: raise on empty input
- confidence: 0.6
"""


def test_finish_carries_every_ref_to_the_tree_it_commits_and_warns_on_a_blank_one(repo):
    """Run 6 committed an assumption anchored at the implementation commit after the fixes
    had moved it two lines down; the page quoted a blank line. `finish` maps it through the
    diff before the commit, rewrites it, and says `anchors: review-commit`."""
    r, base, feature, head = repo
    (r / "app.py").write_text("import math\ndef total(xs):\n    return sum(xs)\n\n"
                              "def mean(xs):\n    return total(xs) / len(xs)\n")
    (r / "review-points.md").write_text(DRIFT_POINTS)
    before = git(r, "rev-parse", "HEAD")
    done = rr(r, "finish", "--subject", "add the mean")
    out = done.stdout + done.stderr
    # Eval run 10: a finish that committed over such a warning was followed by two more
    # `[auto-fix]` commits re-pointing the line by hand. It refuses now, writes nothing.
    assert done.returncode == 4, out
    assert "re-anchored app.py:2 -> app.py:3" in out
    assert "WARNING app.py:4: points at a blank line" in out
    assert "nothing committed" in out
    assert git(r, "rev-parse", "HEAD") == before
    assert (r / "review-points.md").read_text() == DRIFT_POINTS, "left as the agent wrote it"
    (r / "review-points.md").write_text(DRIFT_POINTS.replace("app.py:4", "app.py:6"))
    done = rr(r, "finish", "--subject", "add the mean")
    assert done.returncode == 0, done.stdout + done.stderr
    assert git(r, "rev-parse", "HEAD~1") == before, "one commit for the round"
    committed = git(r, "show", "HEAD:review-points.md")
    assert "### Empty input returns zero\n- file: app.py:3\n" in committed
    assert "- file: app.py:5\n" in committed, "a Fixed ref is read at the working tree"
    assert "\nanchors: review-commit\n" in committed.split("---", 2)[1] + "\n"
    report = json.loads((r / ".human-review" / "review-points.json").read_text())
    assert report["frontmatter"]["anchors"] == "review-commit"


def test_an_assumption_written_as_it_reads_now_is_not_read_at_the_implementation(repo):
    """Eval run 10: prompt.md says to write every line as it reads now, and the agent did —
    `ExceptionControllerAdvice.java:88`, the handler in the working tree. Read first at the
    implementation commit, line 88 was one the fixes replaced, so finish warned "the fixes
    removed that line", committed anyway, and two re-anchor commits followed. The working
    tree's reading lands on code, so it is the one meant: no warning, one commit."""
    r, base, feature, head = repo
    (r / "app.py").write_text("def total(xs):\n    return sum(xs or [])\n")
    (r / "review-points.md").write_text(POINTS)
    done = rr(r, "finish", "--subject", "guard the empty list")
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert "WARNING" not in out
    assert git(r, "rev-parse", "HEAD~1") == head
    assert "### Empty input returns zero\n- file: app.py:2\n" in git(
        r, "show", "HEAD:review-points.md")


def test_finish_run_again_before_the_push_amends_its_own_commit(repo):
    """One `[auto-fix]` commit per round: a second finish before `ci --push` is the same
    round corrected, so it amends — keeping the round's subject, not the correction's."""
    r, base, feature, head = repo
    (r / "review-points.md").write_text(POINTS)
    assert rr(r, "finish", "--subject", "guard the empty list").returncode == 0
    (r / "review-points.md").write_text(
        (r / "review-points.md").read_text().replace("why: out of scope",
                                                     "why: out of scope for #25"))
    done = rr(r, "finish", "--subject", "re-anchor one line")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "amended" in done.stdout
    assert git(r, "rev-parse", "HEAD~1") == head, "still one commit after the feature"
    assert git(r, "log", "-1", "--format=%s") == "[auto-fix] guard the empty list"
    assert "out of scope for #25" in git(r, "show", "HEAD:review-points.md")
    state = json.loads((r / ".human-review" / "review" / "state.json").read_text())
    assert state["reviewCommit"] == git(r, "rev-parse", "HEAD")
    assert len(state["finishes"]) == 1


def test_finish_after_the_push_is_a_new_round(repo, tmp_path):
    """Pushed, the commit is never rewritten: a CI round's finish is a commit of its own."""
    r, base, feature, head = repo
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(r, "remote", "add", "origin", str(remote))
    (r / "review-points.md").write_text(POINTS)
    assert rr(r, "finish", "--subject", "round one").returncode == 0
    first = git(r, "rev-parse", "HEAD")
    git(r, "push", "-q", "origin", "HEAD:main")
    git(r, "fetch", "-q", "origin")
    (r / "app.py").write_text("def total(xs):\n    return sum(xs)\n\n")
    done = rr(r, "finish", "--subject", "round two")
    assert done.returncode == 0, done.stdout + done.stderr
    assert git(r, "rev-parse", "HEAD~1") == first
    assert git(r, "log", "-1", "--format=%s") == "[auto-fix] round two"


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
    # On origin, so `ci` has a run to wait for (a commit that never reached origin exits 2).
    subprocess.run(["git", "init", "-q", "--bare", str(tmp_path / "origin.git")], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(tmp_path / "origin.git")],
                   cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:refs/heads/main"], cwd=repo,
                   check=True, env=ENV)
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=repo, check=True, env=ENV)
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


def test_the_review_records_its_own_cost_in_the_harness_that_ran_it(tmp_path):
    """`prepare` stamps the review's start, the first `ci` the reviewers' end, `finish` the
    fixes' end — and commits the three components as review-cost.json. A Copilot CLI run
    is found in its session store by cwd, branch and time, and named on the commit."""
    import datetime as dt
    import time
    from test_harness_cost import Copilot

    home = tmp_path / "home"
    home.mkdir()
    db = tmp_path / "store.db"
    store = Copilot(db)
    env = {**ENV, "HOME": str(home), "HUMAN_REVIEW_COPILOT_DB": str(db),
           "HUMAN_REVIEW_VSCODE_USER": str(tmp_path / "none")}
    r = (tmp_path / "repo").resolve()
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / ".gitignore").write_text(".human-review/\n")
    (r / "README").write_text("x\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    base = git(r, "rev-parse", "HEAD")
    git(r, "checkout", "-qb", "feat")
    (r / "app.py").write_text("def total(xs):\n    return sum(xs)\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "feature")

    def run(*args):
        p = subprocess.run([sys.executable, str(RR), *args], cwd=r, capture_output=True,
                           text=True, env=env)
        return p

    def stamp(offset_s=0.0):
        return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset_s)) \
            .isoformat(timespec="milliseconds").replace("+00:00", "Z")

    store.session("cop", r, "Run the record-review skill from the human-review plugin")
    assert run("prepare", "--base", base, "--no-ci", "--harness", "copilot-cli") \
        .returncode == 0
    time.sleep(1.1)
    store.call("cop", stamp(), 10.0, agent="reviewer-1")
    time.sleep(1.1)
    run("ci", "--wait-minutes", "0")                   # after the reviewers: stamps their end
    state = json.loads((r / ".human-review/review/state.json").read_text())
    assert state["reviewStartedAt"] < state["reviewersDoneAt"]
    time.sleep(1.1)
    store.call("cop", stamp(), 3.0)                    # deciding and fixing
    (r / "review-points.md").write_text(POINTS)
    done = run("finish", "--subject", "s", "--harness", "copilot-cli")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "cost            review-cost.json" in done.stdout

    assert "review-cost.json" in git(r, "show", "--name-only", "--format=", "HEAD")
    doc = json.loads((r / "review-cost.json").read_text())
    assert schema.cost_problems(doc) == []
    comps = {c["key"]: c for c in doc["components"]}
    assert comps["review"]["aic"] == 10.0 and comps["autofix"]["aic"] == 3.0
    assert "Copilot-Session: cop" in git(r, "log", "-1", "--format=%B")

    # A CI round: the fixes run on, and the record is rewritten with one more round.
    time.sleep(1.1)
    store.call("cop", stamp(), 2.0)
    (r / "app.py").write_text("def total(xs):\n    return sum(xs or [])\n")
    assert run("finish", "--subject", "ci round", "--harness", "copilot-cli").returncode == 0
    doc = json.loads((r / "review-cost.json").read_text())
    assert len(doc["rounds"]) == 2
    assert {c["key"]: c for c in doc["components"]}["autofix"]["aic"] == 5.0


def test_a_headless_claude_review_commits_its_cost_with_the_reviewer_count(tmp_path):
    """Eval run 8, `claude -p --resume … /record-review`: finish printed `review-cost.json:
    not written — unknown key subagents` and committed nothing, so the page fell back to a
    derivation that measured nothing. The record is the primary source; it must be written."""
    import datetime as dt
    import time
    from test_harness_cost import _prompt, _turn

    home = tmp_path / "home"
    sid = "s-claude"
    env = {**ENV, "HOME": str(home), "CLAUDE_CODE_SESSION_ID": sid,
           "HUMAN_REVIEW_COPILOT_DB": str(tmp_path / "none.db"),
           "HUMAN_REVIEW_VSCODE_USER": str(tmp_path / "none")}
    r = (tmp_path / "repo").resolve()
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / ".gitignore").write_text(".human-review/\n")
    (r / "README").write_text("x\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    base = git(r, "rev-parse", "HEAD")
    git(r, "checkout", "-qb", "feat")
    (r / "app.py").write_text("def total(xs):\n    return sum(xs)\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", f"feature\n\nClaude-Session: {sid}")
    slug = home / ".claude" / "projects" / str(r).replace("/", "-")
    slug.mkdir(parents=True)
    f = slug / f"{sid}.jsonl"

    def stamp():
        return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds") \
            .replace("+00:00", "Z")

    def run(*args):
        return subprocess.run([sys.executable, str(RR), *args], cwd=r, capture_output=True,
                              text=True, env=env)

    time.sleep(1.1)
    _prompt(f, stamp(), "/openspec-apply-change x")
    _turn(f, stamp(), "m1", edit=r / "app.py")
    time.sleep(1.1)
    assert run("prepare", "--base", base, "--no-ci").returncode == 0
    time.sleep(1.1)
    _turn(f, stamp(), "m2")
    agents = slug / sid / "subagents"
    agents.mkdir(parents=True)
    _turn(agents / "agent-r1.jsonl", stamp(), "r1", model="claude-sonnet-5-5", read=0,
          out=10_000)
    time.sleep(1.1)
    run("ci", "--wait-minutes", "0")                   # stamps the reviewers' end
    time.sleep(1.1)
    _turn(f, stamp(), "m3")
    (r / "review-points.md").write_text(POINTS)
    done = run("finish", "--subject", "s", "--harness", "claude-code")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "not written" not in done.stdout, done.stdout
    assert "review-cost.json" in git(r, "show", "--name-only", "--format=", "HEAD")
    doc = json.loads((r / "review-cost.json").read_text())
    assert schema.cost_problems(doc) == []
    comps = {c["key"]: c for c in doc["components"]}
    assert comps["implementation"]["usd"] == pytest.approx(0.20)
    assert comps["review"]["usd"] == pytest.approx(0.30)
    assert comps["review"]["entries"][0]["subagents"] == 1
    assert comps["autofix"]["usd"] == pytest.approx(0.20)
    state = json.loads((r / ".human-review/review/state.json").read_text())
    assert "costError" not in state


# ── what the prompt asks of the record, held to the words the page depends on ───────

PROMPT = (HERE.parent.parent / "record-review" / "prompt.md").read_text(encoding="utf-8")


def test_the_prompt_files_a_refuted_finding_under_ignored_at_info_with_its_evidence():
    """Run 5 kept a disproved finding at medium, and the page counted it as worth a look."""
    flat = " ".join(PROMPT.split())
    assert "**refute**" in flat and "goes here too, never under Fixed and never dropped" in flat
    assert "`severity: info`" in flat and "names the evidence that disproves it" in flat
    assert "severity: high|medium|low|info" in flat


def test_the_prompt_asks_an_assumption_why_its_confidence_is_what_it_is():
    flat = " ".join(PROMPT.split())
    assert "Assumptions: 1-2 sentences" in flat
    assert "what holds the confidence where it is" in flat


def test_the_prompt_asks_a_fixed_item_to_anchor_every_place_the_fix_changed():
    """The page deals the fix commit's hunks out by these lines; an unanchored hunk is
    listed apart, so a fix that names one of its three places loses the other two."""
    flat = " ".join(PROMPT.split())
    assert "(Fixed: one per place the fix changed, tests too)" in flat


def test_ci_does_not_wait_for_a_commit_that_never_reached_origin(tmp_path):
    """Eval run 7: prepare's push gate refused the commit, `RR ci` waited out its full
    window for a CI run that could not exist, and the headless session ended first."""
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=ENV)
    (repo / "a.txt").write_text("a\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=ENV)
    subprocess.run(["git", "commit", "-qm", "a"], cwd=repo, check=True, env=ENV)
    r = subprocess.run([sys.executable, str(RR), "ci", "--wait-minutes", "5"],
                       cwd=repo, capture_output=True, text=True, env=ENV, timeout=60)
    assert r.returncode == 2 and "not on origin" in r.stdout
