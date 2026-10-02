#!/usr/bin/env python3
"""The mechanical half of /record-review, so the agent spends its turns on judgement only.

The first run in VS Code Copilot Chat took 332 tool calls. Most of them were not the review:
finding where the format file lives, working out the base, re-reading the diff a hunk at a
time, composing four reviewer prompts, wiring trailers, and hand-writing seventeen PR
comments. Every one of those has exactly one right answer, so a program gives it.

  record-review.py prepare [--base REF] [--impl-subject "…"]
      Resolves the change set, commits uncommitted implementation work if asked to,
      writes the whole diff to one file and one brief per reviewer lens, and prints what
      the agent needs next — nothing else to look up.

  record-review.py finish --subject "…"
      Fills review-points.md's front-matter, checks the file, commits the fixes with it
      under `[auto-fix]` and the three trailers, then derives and checks the PR comments.

What stays with the agent is what only it can do: run the reviewers on the briefs, accept or
decline each finding, write the assumptions it made, and edit the code it accepts.

Exit codes: 0 ok · 2 not a git repo / bad base · 3 uncommitted implementation and no
--impl-subject · 4 review-points.md missing or does not parse.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HR = (HERE.parent / "human-review").resolve()
SCRIPTS = HR / "scripts"
WORK = ".human-review/review"
POINTS = "review-points.md"
DEFAULT_GENERATED = ["**/generated/**", "docs/generated/**", "openapi.yaml",
                     "**/*.genseq.*", "**/api-types.ts", "**/*.drawio*"]

#: Files a reviewer of the *code* does not need. Three of the four lenses read only the
#: code diff: in the second VS Code run the four reviewers paged through one 88 KB patch
#: 66 times between them, and half of it was tests. `"tests"` in human-review.json adds
#: project-specific patterns.
DEFAULT_TESTS = ["**/src/test/**", "**/test/**", "**/tests/**", "**/*.spec.*", "**/*.test.*",
                 "**/*Test.java", "**/*Tests.java", "**/*IT.java", "**/e2e/**",
                 "**/*.feature"]
READS = {"correctness": ["code"], "security": ["code"], "ticket-fit": ["code"],
         "tests": ["code", "tests"]}

LENSES = {
    "correctness": "what input or sequence of actions makes this code return the wrong "
                   "thing, lose state, or crash. Race conditions, off-by-one, null and empty "
                   "cases, error paths.",
    "security": "what an attacker or a careless caller can do with this change: injection, "
                "missing authorization, data leaked into logs or responses, unbounded input.",
    "ticket-fit": "what the ticket asked for that the code does not do, and what the code "
                  "does that nobody asked for. Read the ticket text below first.",
    "tests": "what this change can break while every test stays green: behaviour no test "
             "pins, tests that assert too little, tests that would pass against the old code.",
}

BRIEF = """You are a read-only reviewer. Do not edit any file. Lens: **{lens}** — {what}

The change set (base {base_short}..{head_short}) is in {patches}. Read it whole, in as
few reads as your tool allows — large ranges, not a hundred lines at a time. Open other
files only to confirm a suspicion, and only the lines you need.
{ticket}
Try to BREAK the change, not to approve it. Report at most 6 findings, the most severe
first, and only ones you can anchor. Answer with nothing but this, one block per finding:

### <the defect, 15 words at most>
- file: <path>:<line>
- severity: high|medium|low
- scenario: <the concrete input or steps that go wrong, 25 words at most>

If you find nothing worth reporting, answer `none`.
"""


def git(*args: str, check: bool = True) -> str:
    return subprocess.run(["git", *args], check=check, capture_output=True,
                          text=True).stdout.strip()


def root() -> Path:
    try:
        return Path(git("rev-parse", "--show-toplevel"))
    except subprocess.CalledProcessError:
        sys.exit("record-review: not inside a git repository")


def config(repo: Path) -> dict:
    try:
        return json.loads((repo / "human-review.json").read_text())
    except (OSError, ValueError):
        return {}


def generated_globs(repo: Path) -> list[str]:
    return config(repo).get("generated") or DEFAULT_GENERATED


def test_globs(repo: Path) -> list[str]:
    return DEFAULT_TESTS + list(config(repo).get("tests") or [])


def resolve_base(given: str | None) -> str:
    ref = given or "origin/main"
    try:
        return git("merge-base", "HEAD", ref) if not given else git("rev-parse", ref)
    except subprocess.CalledProcessError:
        sys.exit(f"record-review: cannot resolve base {ref!r}")


def dirty(repo: Path) -> list[str]:
    lines = git("status", "--porcelain").splitlines()
    return [l for l in lines if not l[3:].startswith(".human-review")]


def stage_all() -> None:
    """Everything but the throwaway directory, which most repos ignore anyway — and an
    exclude pathspec naming an ignored path makes `git add` fail outright."""
    git("add", "-A")
    git("reset", "-q", "--", ".human-review", check=False)


def push_gate() -> str:
    """The repository's own pre-push checks, run now instead of at /human-review's push.

    They are the cheapest reviewer there is — deterministic, free, already written — and
    the one the first VS Code run never asked: four model reviewers passed a commit whose
    openapi.yaml the Spectral hook refuses, and the page then stopped at its push. A
    dry-run push runs the hook without sending anything. Returns the lines that say why
    it refused, or "" when it passes."""
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    r = subprocess.run(["git", "push", "--dry-run", "origin", f"HEAD:refs/heads/{branch}"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return ""
    lines = [l.rstrip() for l in (r.stdout + r.stderr).splitlines() if l.strip()]
    keep = [l for l in lines if re.search(r"\berror\b|❌|rejected|denied|fatal", l, re.I)]
    return "\n".join((keep or lines[-5:])[:12])


def session_id() -> str:
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "")


def trailers(**keys: str) -> str:
    return "\n".join(f"{k}: {v}" for k, v in keys.items() if v)


# --------------------------------------------------------------------------- prepare

def prepare(args) -> int:
    repo = root()
    os.chdir(repo)
    base = resolve_base(args.base)
    if dirty(repo):
        if not args.impl_subject:
            print("The implementation is not committed. Re-run with "
                  "--impl-subject \"<what it implements>\" and it is committed for you, "
                  "alone, before the review.")
            for l in dirty(repo)[:20]:
                print("  ", l)
            return 3
        stage_all()
        msg = args.impl_subject + ("\n\n" + trailers(**{"Claude-Session": session_id()})
                                   if session_id() else "")
        git("commit", "-q", "-m", msg)
    head = git("rev-parse", "HEAD")
    commits = git("log", "--format=%h %s", f"{base}..HEAD").splitlines()
    if not commits:
        print("Nothing to review: HEAD is the base.")
        return 2

    excludes = [f":(exclude,glob){g}" for g in generated_globs(repo)]
    tests = [f":(glob){g}" for g in test_globs(repo)]
    files = git("diff", "--name-only", f"{base}..HEAD", "--", ".", *excludes).splitlines()
    test_files = set(git("diff", "--name-only", f"{base}..HEAD", "--", *tests).splitlines())
    groups = {"code": [f for f in files if f not in test_files],
              "tests": [f for f in files if f in test_files]}
    work = repo / WORK
    work.mkdir(parents=True, exist_ok=True)
    sizes = {}
    for name, paths in groups.items():
        patch = git("diff", "--no-ext-diff", "-U3", f"{base}..HEAD", "--", *paths) if paths else ""
        (work / f"diff-{name}.patch").write_text(patch + "\n")
        sizes[name] = (len(paths), len(patch) // 1024)
    ticket = ""
    if args.ticket:
        ticket = f"\nThe ticket, as the human gave it:\n\n{args.ticket.strip()}\n"
    for lens, what in LENSES.items():
        patches = " and ".join(f"`{WORK}/diff-{g}.patch`" for g in READS[lens] if groups[g])
        (work / f"brief-{lens}.md").write_text(BRIEF.format(
            lens=lens, what=what, patches=patches,
            base_short=base[:8], head_short=head[:8], ticket=ticket))
    gate = push_gate()
    if gate:
        (work / "gate.txt").write_text(gate + "\n")
    state = {"base": base, "implementation": head, "session": session_id()}
    (work / "state.json").write_text(json.dumps(state, indent=2) + "\n")

    print(f"base            {base[:8]}  ({args.base or 'merge-base with origin/main'})")
    print(f"implementation  {head[:8]}  ({len(commits)} commit(s): {commits[0]}"
          + (" …" if len(commits) > 1 else "") + ")")
    print("diff            " + "  ".join(
        f"{WORK}/diff-{g}.patch ({n} files, {kb} KB)" for g, (n, kb) in sizes.items())
        + " — generated files left out")
    print("briefs          " + "  ".join(f"{WORK}/brief-{l}.md" for l in LENSES))
    if gate:
        print("push gate       FAILS — the repository's own pre-push checks refuse this "
              f"commit ({WORK}/gate.txt). Each error is a finding with `source: pre-push "
              "hook`; /human-review cannot run until it is fixed:")
        for line in gate.splitlines():
            print("                " + line)
    else:
        print("push gate       passes (the repository's pre-push checks, run as a dry-run)")
    print()
    print("Next: start one read-only reviewer subagent per brief, in parallel, each given "
          "only its brief file's content. Then decide every finding, edit what you accept, "
          f"write {POINTS} (piles only; front-matter is filled for you) and run:")
    print(f"  {Path(__file__).resolve()} finish --subject \"<what the fixes do>\" "
          "--reviewers \"<how the review ran>\"")
    return 0


# --------------------------------------------------------------------------- finish

def fill_frontmatter(text: str, values: dict[str, str]) -> str:
    """Add the keys the program knows, keep every key the agent wrote."""
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    have = dict(re.findall(r"^([A-Za-z-]+):\s*(.*)$", m.group(1), re.M)) if m else {}
    merged = {**{k: v for k, v in values.items() if v}, **have}
    head = "---\n" + "\n".join(f"{k}: {v}" for k, v in merged.items()) + "\n---\n"
    return head + (text[m.end():] if m else "\n" + text.lstrip())


def finish(args) -> int:
    repo = root()
    os.chdir(repo)
    points = repo / POINTS
    if not points.is_file():
        print(f"{POINTS} is missing at the repository root.")
        return 4
    try:
        state = json.loads((repo / WORK / "state.json").read_text())
    except (OSError, ValueError):
        state = {"base": resolve_base(None), "implementation": git("rev-parse", "HEAD"),
                 "session": session_id()}
    points.write_text(fill_frontmatter(points.read_text(), {
        "base": state["base"], "implementation": state["implementation"],
        "reviewers": args.reviewers or "", "session": state.get("session", ""),
        "fixed-in": "HEAD"}))

    check = subprocess.run([sys.executable, str(SCRIPTS / "review-points.py"), "--check"],
                           capture_output=True, text=True)
    print(check.stdout.strip().splitlines()[0] if check.stdout.strip() else "")
    if check.returncode != 0:
        print(check.stdout + check.stderr)
        return 4

    stage_all()
    msg = f"[auto-fix] {args.subject}\n\n" + trailers(**{
        "Review-Points": POINTS, "Implements": state["implementation"],
        "Claude-Session": state.get("session", "")})
    git("commit", "-q", "-m", msg)
    sha = git("rev-parse", "--short", "HEAD")
    print(f"committed       {sha} [auto-fix] {args.subject}")

    for extra in (["--from-review-points"], ["--check", "--base", state["base"]]):
        r = subprocess.run([sys.executable, str(SCRIPTS / "push-pr-comments.py"), *extra],
                           capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip().splitlines()
        print("pr comments     " + (out[0] if out else f"exit {r.returncode}"))
    print("Done. Nothing was pushed. /human-review builds the page when the human wants it.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--base")
    p.add_argument("--impl-subject", help="commit uncommitted work as the implementation")
    p.add_argument("--ticket", help="the ticket text, handed to every reviewer")
    f = sub.add_parser("finish")
    f.add_argument("--subject", required=True)
    f.add_argument("--reviewers", default="")
    args = ap.parse_args(argv)
    return prepare(args) if args.cmd == "prepare" else finish(args)


if __name__ == "__main__":
    raise SystemExit(main())
