#!/usr/bin/env python3
"""Ask a cheap model for the one judgement the Tests tab's matrix still needs, and nothing else.

`refresh-report.py` is the machine half of `/human-review`: fixed inputs, fixed outputs,
free, safe to run again at any time. This is the other half, reduced to the smallest thing
that can still be a *command*. The matrix used to be all of it — a model wrote
`assets/requirements-map.html` and a `test-index/` catalogue, layout and CSS included, at
$4–$10 a run. Now `semcov.py` draws the matrix and pairs most of the ticket's sentences
with the tests that run this PR's changed lines on its own, from shared evidence; what is
left is the sentences it could not pair, each with a handful of candidate tests. This
program asks a model about exactly those and writes the answer — `test-mapping.json`,
checked against `reference/test-mapping.schema.json` — and the build merges it in.

The Demo film's script, the other model-written artifact, has a sibling of this program:
`rerun-film.py`, which borrows its plumbing (`claude_argv`, `_priced`, `record_run`).

Four properties are the whole point:

- **A cheap model, named here.** `haiku` by default: choosing among eight tests for a
  sentence is not work that needs more. `--model`, `"mappingModel"` in the repository's
  `human-review.json`, or `$HUMAN_REVIEW_MAPPING_MODEL` change it. Its price lands on the
  cost tab through `.model-runs.json`, read off the CLI's own `total_cost_usd`.
- **Nothing is asked when nothing is open.** A ticket the script paired whole gets an empty
  answer written and no model run at all.
- **The previous answer is kept**, under `.human-review/.model-prev/`. Dot-prefixed,
  because `publish-demo.sh` publishes what does not start with a dot.
- **It refuses rather than half-writes.** An answer that is not JSON, fails the schema,
  names a sentence it was not asked about or a test it was not shown, or claims a coverage
  its own links cannot stand behind, is not written: this exits non-zero, says why, and the
  file on disk stays what it was.

    rerun-model.py                      # pair the open sentences, in .human-review/
    rerun-model.py --dry-run            # print the command and what is open, spend nothing
    rerun-model.py --prompt-only        # the whole prompt on stdout, for another harness
    rerun-model.py --answer reply.json  # validate and install an answer made elsewhere

`--prompt-only` and `--answer` are how a session that is not Claude Code runs this step:
GitHub Copilot picks its cheap model itself (*Auto*, or `gpt-5-mini`), so it reads the
prompt, answers it, and hands the answer back here to be checked and written.

`--dry-run` is not a nicety either: every test of this file, and every check that the
button is wired to the right program, runs through it. Nothing below it may cost money.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: The prompt, beside the skill's other prose rather than inside this file. It is the thing
#: being paid for, so it is reviewed like prose and diffed like prose. The open sentences
#: and their candidate tests are appended to it as JSON.
PROMPT = HERE.parent / "reference" / "matrix-prompt.md"

#: What the model owns, in `refresh-report.MODEL_OWNED`'s own spelling minus `content.json`
#: — the layout and the ledes are a human's answer to "what is this page for", and no
#: button regenerates those.
WRITES = ("test-mapping.json",)

#: The cheap model the pairing asks by default. A Copilot session picks its own (*Auto* or
#: `gpt-5-mini`) and comes back through `--answer`.
MAPPING_MODEL_DEFAULT = "haiku"
#: Where a repository says which model pairs its sentences, in `human-review.json`.
MAPPING_MODEL_KEY = "mappingModel"

#: Where the copy being replaced goes. Dot-prefixed: see the module docstring.
PREV = ".model-prev"

#: What each paid run really cost, appended here so the button that spends it can stop
#: guessing. The label used to read `~$5 on Sonnet` — a number typed once, never measured —
#: and three real runs on the demo page came in at $4.00, $8.09 and $10.63. A reader who
#: budgeted for the label was out by a factor of two, in the direction that matters.
#:
#: Dot-prefixed like the manifest, and for the same reason: it is a record of this
#: machine's spending, not a fact about the branch, so it must not travel in the zip.
RUNS_LEDGER = ".model-runs.json"
RUNS_KEPT = 20

#: The film script's model (`rerun-film.py` reads it here), named rather than defaulted.
#: `HUMAN_REVIEW_MODEL` overrides it for the one case that is not a preference — a harness
#: where `sonnet` resolves to nothing. The pairing does not use it: see `mapping_model`.
MODEL = os.environ.get("HUMAN_REVIEW_MODEL") or "sonnet"


def mapping_model(flag: str | None = None, config: Path = Path("human-review.json")) -> str:
    """Which model pairs the open sentences: the flag, then `$HUMAN_REVIEW_MAPPING_MODEL`,
    then the repository's `human-review.json` (`"mappingModel"`), then haiku."""
    if flag:
        return flag
    if os.environ.get("HUMAN_REVIEW_MAPPING_MODEL"):
        return os.environ["HUMAN_REVIEW_MAPPING_MODEL"]
    try:
        got = json.loads(config.read_text(encoding="utf-8")).get(MAPPING_MODEL_KEY)
        if isinstance(got, str) and got.strip():
            return got.strip()
    except (OSError, ValueError, AttributeError):
        pass
    return MAPPING_MODEL_DEFAULT


def mapping_argv(model: str) -> list[str]:
    """The pairing's headless invocation: the prompt on stdin, the answer in the JSON
    envelope's `result`. No tools at all — everything the model needs is in the prompt, and
    a run that cannot open a file cannot wander the repository at the invoice's expense —
    and no MCP servers, which would only slow it down."""
    extra = (os.environ.get("HUMAN_REVIEW_MODEL_ARGS") or "").split()
    return ["claude", "-p", "--model", model, "--output-format", "json", "--tools", "",
            "--strict-mcp-config", *extra]


#: What makes a directory a review directory. Checked before anything is bought, because
#: `--dir` arrives from a server that computed it as a path *relative to the repository
#: root* — so the value is `.` whenever the report is served from the root itself, and `.`
#: is a directory that exists. Without this, a misconfigured server would have handed a
#: model the repository and asked it to write a matrix into it, at full price, and the
#: first sign of trouble would have been the invoice.
LOOKS_LIKE_A_REVIEW = "content.json"


def claude_argv(root: Path) -> list[str]:
    """The headless invocation, as argv, with the prompt going in on **stdin**.

    On stdin and not as the trailing argument, which is how this was first written and
    which does not work: `--add-dir` takes a *list* of directories, so a prompt after it is
    swallowed as another directory and `claude` exits with "Input must be provided" — a
    failure that looks like a broken model step and is actually a broken command line. It
    is also the safer shape, because a 4KB positional argument is a 4KB positional
    argument.

    `--permission-mode acceptEdits` because the run's whole job is to write two files and
    there is nobody at a keyboard to approve each one; `--add-dir` so the repository is
    reachable when the server's cwd and the repository are not the same directory. Built as
    data, and printed by `--dry-run`, so "which model did that page cost" is a question the
    command answers rather than one the invoice does.
    """
    extra = (os.environ.get("HUMAN_REVIEW_MODEL_ARGS") or "").split()
    return ["claude", "-p", "--model", MODEL, "--permission-mode", "acceptEdits",
            "--output-format", "json", "--add-dir", str(root), *extra]


def missing(review: Path) -> list[str]:
    """Which of the model's artifacts is not on disk, or is on disk and empty."""
    gone = []
    for rel in WRITES:
        p = review / rel
        if p.is_dir():
            if not any(p.iterdir()):
                gone.append(rel)
        elif not p.is_file() or not p.stat().st_size:
            gone.append(rel)
    return gone


def _semcov():
    """`semcov.py`, the scripted half: what is open, what to ask, how to check the answer."""
    import importlib.util
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    if "semcov" in sys.modules:
        return sys.modules["semcov"]
    spec = importlib.util.spec_from_file_location("semcov", HERE / "semcov.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["semcov"] = mod
    spec.loader.exec_module(mod)
    return mod


def parse_answer(text: str):
    """The JSON document out of a model's reply: the whole reply, or the one fenced block
    in it, or the span from the first `{` to the last `}`. None when there is none."""
    text = (text or "").strip()
    for candidate in (text,
                      *[m.group(1) for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S)],
                      text[text.find("{"):text.rfind("}") + 1] if "{" in text else ""):
        try:
            doc = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(doc, dict):
            return doc
    return None


def build_prompt(asked: dict) -> str:
    """The prompt file, then what is open, as the JSON the prompt describes."""
    return (PROMPT.read_text(encoding="utf-8").rstrip() + "\n\n## Input\n\n```json\n"
            + json.dumps(asked, indent=1, ensure_ascii=False) + "\n```\n")


def check_answer(doc, asked: dict) -> list[str]:
    """Every reason this answer may not be written: the schema, then the facts — only the
    sentences asked about, only the tests shown."""
    if doc is None:
        return ["the reply holds no JSON object"]
    sc = _semcov()
    return sc.problems(doc, {x["id"] for x in asked["sentences"]},
                       {t["id"] for t in asked["tests"]})


def install(review: Path, doc: dict) -> Path:
    """Write the answer, by everyone's name for it."""
    out = review / WRITES[0]
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def keep_previous(review: Path) -> Path:
    """Put today's answer somewhere a reader can get it back from, and return where.

    Copied and not moved: until the new answer has passed its checks, the old one is still
    the one the page is built from."""
    dest = review / PREV
    dest.mkdir(parents=True, exist_ok=True)
    for rel in WRITES:
        src = review / rel
        target = dest / Path(rel).name
        if src.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(src, target)
        elif src.is_file():
            shutil.copy2(src, target)
    return dest


def _priced(out: str):
    """`(cost, what the model said)` out of `claude -p --output-format json`.

    `(None, "")` for anything this does not recognise, which is not an error: the run
    happened and the artifacts are on disk either way, and a bookkeeping field that moved
    between CLI versions must never be the reason a $5 run reports as a failure.
    """
    try:
        doc = json.loads(out)
        if not isinstance(doc, dict):
            raise ValueError
    except Exception:
        return None, ""
    cost = doc.get("total_cost_usd")
    return (float(cost) if isinstance(cost, (int, float)) else None,
            doc.get("result") or "")


def record_run(review: Path, cost, seconds: float, ledger: str = RUNS_LEDGER,
               model: str | None = None) -> None:
    """Append what this run cost, so the button can stop guessing what the next one will.

    Appended even when the cost could not be read, with `cost: null` — the *number* of runs
    is itself the answer to "has anybody ever pressed this", and a ledger that only records
    the runs it could price would quietly claim a page had never been rerun.

    Bounded, and failures are swallowed whole. This is bookkeeping running after the money
    has already been spent; an unwritable directory is not a reason to report a successful
    run as a failed one.

    `ledger` is the file it goes in: the matrix's by default, `rerun-film.py` passes its
    own — the two buttons buy different amounts of work, and an average over both would be
    the right price for neither.
    """
    path = review / ledger
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        runs = doc.get("runs") if isinstance(doc, dict) else doc
        if not isinstance(runs, list):
            runs = []
    except Exception:
        runs = []
    runs.append({"when": datetime.datetime.now(datetime.timezone.utc)
                 .isoformat(timespec="seconds"),
                 "model": model or MODEL, "cost": cost, "seconds": round(seconds, 1)})
    try:
        path.write_text(json.dumps({"version": 1, "runs": runs[-RUNS_KEPT:]}, indent=2)
                        + "\n", encoding="utf-8")
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=".human-review", help="the review directory")
    ap.add_argument("--model", help="the model that pairs the open sentences "
                                    f"(default: {MAPPING_MODEL_KEY} in human-review.json, "
                                    f"else {MAPPING_MODEL_DEFAULT})")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the command and what is open, spend nothing")
    ap.add_argument("--prompt-only", action="store_true",
                    help="print the whole prompt for another harness to answer, spend nothing")
    ap.add_argument("--answer", help="validate and install an answer made elsewhere")
    args = ap.parse_args(argv)

    review = Path(args.dir)
    if not review.is_dir() or not (review / LOOKS_LIKE_A_REVIEW).is_file():
        print(f"[model] {review}/ is not a review directory — no "
              f"{LOOKS_LIKE_A_REVIEW} in it, so there is no report to regenerate. "
              "Run /human-review from the repository root first.", file=sys.stderr)
        return 2
    if not PROMPT.is_file():
        print(f"[model] {PROMPT} is missing — that file *is* the step.", file=sys.stderr)
        return 2

    root = Path.cwd().resolve()
    sc = _semcov()
    try:
        spec = json.loads((review / LOOKS_LIKE_A_REVIEW).read_text(encoding="utf-8"))
    except ValueError:
        spec = {}
    g = sc.gather(spec if isinstance(spec, dict) else {}, review, root)
    if g is None:
        print("[model] no ticket resolved for this branch (pr.ticket in content.json, or "
              "#N in the PR title) — there are no sentences to pair.", file=sys.stderr)
        return 2
    asked = sc.model_input(g["ticket"], g["sentences"], g["rows"], g["scripted"], g["docs"])
    decided = len(g["scripted"]["decided"])
    links = sum(len(e["tests"]) for e in g["scripted"]["decided"])
    print(f"[model] {len(g['sentences'])} sentences, {len(g['rows'])} tests: the script "
          f"paired {decided} ({links} links); {len(asked['sentences'])} open.")

    if args.answer:
        doc = parse_answer(Path(args.answer).read_text(encoding="utf-8"))
        bad = check_answer(doc, asked)
        if bad:
            print(f"[model] {args.answer} is refused:", file=sys.stderr)
            for b in bad:
                print(f"  - {b}", file=sys.stderr)
            return 5
        keep_previous(review)
        print(f"[model] {install(review, doc)} written from {args.answer}.")
        return 0

    prompt = build_prompt(asked)
    if args.prompt_only:
        print(prompt)
        return 0

    if not asked["sentences"]:
        if args.dry_run:
            print("[model] dry run — nothing is open, so nothing would be asked of a model.")
            return 0
        keep_previous(review)
        install(review, {"schema": sc.SCHEMA_VERSION,
                         "note": "the script paired every sentence; no model was asked",
                         "sentences": []})
        print("[model] the script paired every sentence — no model run, nothing spent.")
        return 0

    model = mapping_model(args.model)
    argvec = mapping_argv(model)
    # Printed with the prompt named rather than quoted, always: a log line carrying all of
    # it is a log line nobody reads — including a reader checking what the button buys.
    print("[model] $ " + " ".join(a if a else '""' for a in argvec)
          + f"  < {PROMPT.name} + {len(asked['sentences'])} open sentences, "
          f"{len(asked['tests'])} candidate tests ({len(prompt)} chars)")
    if args.dry_run:
        print(f"[model] dry run — nothing was asked of {model} and nothing was paid for.")
        return 0

    if not shutil.which("claude"):
        print("[model] no `claude` on PATH. Answer the prompt in another harness instead: "
              "--prompt-only, then --answer.", file=sys.stderr)
        return 2

    kept = keep_previous(review)
    started = time.time()
    proc = subprocess.run(argvec, cwd=str(root), input=prompt, text=True,
                          capture_output=True)
    cost, said = _priced(proc.stdout)
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    record_run(review, cost, time.time() - started, model=model)
    if proc.returncode != 0:
        print(f"[model] {model} exited {proc.returncode}; {WRITES[0]} is left as it was.",
              file=sys.stderr)
        return 1
    doc = parse_answer(said or proc.stdout)
    bad = check_answer(doc, asked)
    if bad:
        print(f"[model] {model}'s answer is refused, and {WRITES[0]} is left as it was "
              f"(the previous one is also in {kept}/):", file=sys.stderr)
        for b in bad:
            print(f"  - {b}", file=sys.stderr)
        return 5
    install(review, doc)
    price = f"${cost:.4f}" if isinstance(cost, (int, float)) else "an unpriced run"
    print(f"[model] {model} paired {len(doc['sentences'])} of {len(asked['sentences'])} open "
          f"sentences for {price}; {WRITES[0]} written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
