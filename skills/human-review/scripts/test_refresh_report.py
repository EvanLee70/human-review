"""The line between the half a program can redo and the half only a model writes.

`refresh-report.py` is the program half, as one command. What these pin is the boundary
itself: that a refresh never writes the model's artifacts, never buys a model call, refuses
rather than builds a page whose judgement is missing, and that "refresh the page" does not
silently mean "drive a browser and run the test suite again".
"""
import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), str(HERE / f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


refresh = _load("refresh-report")
build = _load("build-review-html")

# The page builder is a package now (`hrbuild/`), and `build-review-html.py` re-exports
# every name in it so the rest of the skill still finds them here. A *patch* is the one
# thing a re-export cannot carry: rebinding `build.X` leaves the module that defines `X`
# calling the original. So the handful of tests below that replace a function reach for
# the module that owns it.
logging_tab = importlib.import_module("hrbuild.tabs.logging")


def _review(tmp_path: Path, *, complete=True) -> Path:
    d = tmp_path / ".human-review"
    (d / "assets").mkdir(parents=True)
    if complete:
        (d / "content.json").write_text("{}", encoding="utf-8")
        (d / "test-mapping.json").write_text('{"schema": "test-mapping/1", "sentences": []}',
                                             encoding="utf-8")
    return d


# ── what the program refuses to do ────────────────────────────────────────────────

def test_a_missing_judgement_stops_the_refresh_and_is_named(tmp_path):
    """A page rebuilt without the findings, the matrix or the catalogue is not a smaller
    page — it is the same page with its argument deleted. And the reason it is missing is
    never "a step did not run", so the build's own wording would send the reader after the
    wrong half: this one names the model's part and stops."""
    d = _review(tmp_path, complete=False)
    assert [rel for rel, _ in refresh.missing_model_work(d)] == [
        "content.json", "test-mapping.json"]
    assert refresh.missing_model_work(_review(tmp_path / "ok")) == []


def test_a_model_written_matrix_from_an_older_run_stands_in_for_the_mapping(tmp_path):
    """Before the matrix was drawn by `semcov.py`, a model wrote its whole HTML. A review
    made then has no `test-mapping.json`, and its matrix is still a matrix: the refresh
    builds it as it is rather than refusing a page that was fine yesterday."""
    d = _review(tmp_path)
    (d / "test-mapping.json").unlink()
    assert [rel for rel, _ in refresh.missing_model_work(d)] == ["test-mapping.json"]
    (d / "assets" / "requirements-map.html").write_text("<div/>", encoding="utf-8")
    assert refresh.missing_model_work(d) == []


def test_the_refusal_exits_three_and_says_which_half_writes_them(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _review(tmp_path, complete=False)
    assert refresh.main(["--dry-run"]) == 3
    err = capsys.readouterr().err
    assert "only a model writes" in err and "content.json" in err
    assert "/human-review" in err, "the way to get them back is named"


def test_no_report_at_all_is_a_different_answer(tmp_path, capsys, monkeypatch):
    """Nothing to refresh is not a broken report: it is a review that never ran."""
    monkeypatch.chdir(tmp_path)
    assert refresh.main(["--dry-run"]) == 2
    assert "there is no report to refresh" in capsys.readouterr().err


# ── what "refresh" means by default ───────────────────────────────────────────────

def test_the_default_refresh_reruns_no_producer(tmp_path):
    """Most refreshes are a change to the *page*, not to the evidence. Re-deriving a
    Code City shot nobody touched is a minute of waiting for an identical byte."""
    d = _review(tmp_path)
    cmds = refresh.plan(d, "none", None, True, False, None)
    assert not any("run-steps.py" in c[1] for c in cmds)
    assert [Path(c[1]).name for c in cmds] == ["build-review-html.py", "serve-review.py"]


def test_cheap_leaves_out_the_producers_that_drive_a_browser_or_a_suite(tmp_path):
    """`--steps cheap` is "re-derive what a change to the branch could have changed",
    and the four that need a served app, a Chrome or a tracing backend are not that."""
    assert refresh.steps_argv("cheap") == ["--skip", "sequence,video,city,dsaudit"]
    assert refresh.steps_argv("all") == []
    assert refresh.steps_argv("none") is None and refresh.steps_argv("") is None
    assert refresh.steps_argv("diagrams,tests") == ["--only", "diagrams,tests"]
    # Every name in the heavy list is a step that exists; a typo here would silently
    # stop skipping something.
    steps = _load("run-steps")
    assert set(refresh.HEAVY_STEPS) <= {s[0] for s in steps.STEPS}


def test_static_is_the_set_a_button_may_fire_and_leaves_out_the_suites(tmp_path):
    """`--steps static` is what the page's own Rerun asks for, so the bar is higher than
    `cheap`: nothing in it may need a served app, a browser, a film or a test suite.

    `cheap` is not that set, and the gap is the point. It skips the four heavy ones and
    keeps `traces`, whose configured `commands` are the project's own e2e suite — in
    petclinic a cucumber run against :4200. A reader who edited a test body and pressed a
    button asked for the page to catch up, not for a browser suite to be run at them."""
    argv = refresh.steps_argv("static")
    assert argv[0] == "--only"
    named = argv[1].split(",")
    assert named == list(refresh.STATIC_STEPS)
    # Every name is a step that exists — a typo would silently drop a producer.
    steps = _load("run-steps")
    assert set(named) <= {s[0] for s in steps.STEPS}
    # And none of them is one of the four that need something up, nor the suite harvester.
    assert not (set(named) & set(refresh.HEAVY_STEPS))
    assert "traces" not in named
    # The manifest behind the Tests tab is in, because that is the case this exists for.
    assert "tests" in named


def test_the_producers_run_before_the_build_and_the_build_before_the_server(tmp_path):
    d = _review(tmp_path)
    cmds = refresh.plan(d, "cheap", "origin/main", True, False, None)
    assert [Path(c[1]).name for c in cmds] == [
        "run-steps.py", "build-review-html.py", "serve-review.py"]
    # Containment rather than a tail slice: the producer command grows flags (`--timing`,
    # `--force`, `--no-ledger`), and a test that pins their *order* fails on every one of
    # them while proving nothing about the thing it is named for.
    assert "--base" in cmds[0] and cmds[0][cmds[0].index("--base") + 1] == "origin/main"


def test_a_refresh_never_stamps_the_ledger(tmp_path):
    """`.steps.json` is an input to the build's own cost cache, and a refresh's step windows
    name a stretch of a *later* session in which none of the reviewed conversation happened.
    Stamping it is therefore both meaningless and expensive — it costs the build the whole
    cost ledger, which is the single slowest thing on a rebuild."""
    d = _review(tmp_path)
    for steps in ("static", "cheap", "all"):
        cmds = refresh.plan(d, steps, None, False, False, None)
        assert "--no-ledger" in cmds[0], f"--steps {steps} would stamp the ledger"


def test_the_page_is_never_served_when_the_caller_asked_only_for_the_file(tmp_path):
    d = _review(tmp_path)
    cmds = refresh.plan(d, "none", None, False, False, None)
    assert not any("serve-review.py" in c[1] for c in cmds)


# ── the model call the build itself makes ─────────────────────────────────────────

def test_a_refresh_builds_with_no_model_unless_asked(tmp_path):
    """The Logging tab's privacy verdicts are the one model call inside a *build*. A
    refresh reuses what an earlier run paid for and buys nothing new."""
    d = _review(tmp_path)
    build_cmd = [c for c in refresh.plan(d, "none", None, True, False, None)
                 if "build-review-html.py" in c[1]][0]
    assert "--no-model" in build_cmd
    allowed = [c for c in refresh.plan(d, "none", None, True, True, None)
               if "build-review-html.py" in c[1]][0]
    assert "--no-model" not in allowed


# ── the cost the page reports ─────────────────────────────────────────────────────

def test_a_refresh_keeps_the_session_that_did_the_work(tmp_path):
    """A build run in a later session cannot recompute what the first one spent, and a
    page that drops the number silently is a page claiming the review was free."""
    d = _review(tmp_path)
    (d / ".session").write_text("abc-123\n", encoding="utf-8")
    assert refresh.session_id(d) == "abc-123"
    assert refresh.session_id(tmp_path / "nowhere") is None


def test_a_blank_session_file_means_no_claude_session_not_the_refreshers_own(tmp_path):
    """A Copilot run writes `.session` empty. Read as "absent", the refresh fell back to its
    own `$CLAUDE_CODE_SESSION_ID` and billed that conversation as the review run."""
    d = _review(tmp_path)
    (d / ".session").write_text("\n", encoding="utf-8")
    assert refresh.session_id(d) == ""
    from hrbuild.tabs.cost import cost_session
    import os
    os.environ["CLAUDE_CODE_SESSION_ID"] = "the-refresher"
    try:
        assert cost_session(d) == ""
        assert cost_session(tmp_path / "nowhere") == "the-refresher"
    finally:
        del os.environ["CLAUDE_CODE_SESSION_ID"]


def test_the_skill_tells_the_reader_to_run_the_program_not_the_commands():
    """The whole point is that nobody retypes the three commands — including the model
    reading this skill, which is how the two halves got run together in the first place."""
    skill = (HERE.parent / "SKILL.md").read_text(encoding="utf-8")
    assert "refresh-report.py" in skill
    assert skill.count("refresh-report.py") >= 2, "Step 5 and the iteration path"


# ── what the skill no longer does ─────────────────────────────────────────────────
# The judgement moved to the branch. These pin the removal rather than the addition,
# because the failure mode is a skill that *also* still triages: the model would form a
# second opinion, write it into content.json, and the page would carry two records of one
# review with no way for a reader to tell which decisions were actually made.

def _skill() -> str:
    return (HERE.parent / "SKILL.md").read_text(encoding="utf-8")


def test_the_skill_no_longer_gates_on_a_pass_having_run():
    """`review-passes.py --require` refused to build a page when no pass was found in the
    conversation. The passes now run in the *coding* session, so the gate was refusing
    every honest run of the new flow."""
    skill = _skill()
    assert "--require" not in skill
    assert "I can't write up a review that hasn't happened" not in skill
    # It survives as a cost source, and the skill says which.
    assert "review-passes.py" in skill and "cost" in skill


def test_the_skill_no_longer_triages_or_edits_code():
    skill = _skill()
    for gone in ("Non-disputable", "Disputable",
                 "leave your own fixes\nuncommitted", "leave your own fixes uncommitted"):
        assert gone not in skill, gone
    assert "Change no code in this step" in skill


def test_the_skill_no_longer_reads_transcripts_for_the_third_pile():
    """Modes A/B/C had a subagent read an authoring transcript verbatim to reconstruct
    what the coder assumed. The coder writes them down now, while it still has them."""
    skill = _skill()
    assert "mode B" not in skill and "third pile" not in skill
    assert "authoring-sessions.py" not in skill


def test_the_skill_names_the_model_the_matrix_subagent_gets():
    """The matrix and the catalogue are the only paid, non-reproducible work left, and
    their price is a visible line on the cost tab. Left to a default, the run pays Opus
    rates for a table Sonnet writes no worse."""
    skill = _skill()
    assert "model: sonnet" in skill


def test_the_skill_points_at_the_branchs_own_record():
    skill = _skill()
    assert "review-points.md" in skill
    assert "review-points.py --check" in skill
    assert '{"auto": "review-points"}' in skill
    assert "/record-review" in skill


def test_content_json_is_still_model_owned_but_no_longer_the_judgement():
    """A page cannot be built without it, so the refusal stays. What it *claims* changed:
    'the judgement' was a description of an artifact that no longer holds one."""
    assert "content.json" in refresh.MODEL_OWNED
    assert "judgement" not in refresh.MODEL_OWNED["content.json"]
    assert "layout" in refresh.MODEL_OWNED["content.json"]
    assert "review-points" in refresh.MODEL_OWNED["content.json"]
    # The matrix is drawn by a script now; only the pairing the script left open is a
    # model's, and only that is owned.
    assert "test-mapping.json" in refresh.MODEL_OWNED
    assert "assets/requirements-map.html" not in refresh.MODEL_OWNED
    assert "test-index" not in refresh.MODEL_OWNED


def test_the_two_review_tab_producers_are_safe_to_rerun_from_the_page():
    """Both need nothing but the repository, and both feed the tab a reader is looking at
    when they press Rerun."""
    assert "reviewpoints" in refresh.STATIC_STEPS
    assert "aftermath" in refresh.STATIC_STEPS


def test_a_commit_that_promises_a_points_file_it_does_not_have_is_refused(tmp_path,
                                                                         monkeypatch):
    """The branch says it recorded its own review and the file is gone — a rebase kept the
    message and dropped the file. Every other missing input degrades to a named absence,
    because 'this project has no such thing' is a real state; this one cannot be."""
    monkeypatch.chdir(tmp_path)

    class Proc:
        returncode = 0
        stdout = '{"review": "deadbeefcafe", "points_file": "review-points.md",' \
                 ' "fallback": false}'
        stderr = ""

    monkeypatch.setattr(refresh.subprocess, "run", lambda *a, **k: Proc())
    assert refresh.broken_points_promise("origin/main") == ("deadbeefcafe",
                                                            "review-points.md")
    # …and not when the file is there.
    (tmp_path / "review-points.md").write_text("## Fixed\n", encoding="utf-8")
    assert refresh.broken_points_promise("origin/main") is None


def test_the_fallback_guess_never_triggers_the_refusal(tmp_path, monkeypatch):
    """`review-commits.py`'s fallback *is* 'the one commit touching the points file', so a
    fallback plus a missing file is a contradiction in terms — and refusing on it would
    refuse every build on a branch that has neither."""
    monkeypatch.chdir(tmp_path)

    class Proc:
        returncode = 3
        stdout = '{"review": "deadbeefcafe", "points_file": "review-points.md",' \
                 ' "fallback": true}'
        stderr = ""

    monkeypatch.setattr(refresh.subprocess, "run", lambda *a, **k: Proc())
    assert refresh.broken_points_promise("origin/main") is None


def test_the_base_the_check_asks_about_is_the_one_the_producers_use(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert refresh.config_base(None) == "origin/main"
    (tmp_path / "human-review.json").write_text('{"base": "origin/release"}',
                                                encoding="utf-8")
    assert refresh.config_base(None) == "origin/release"
    assert refresh.config_base("HEAD~3") == "HEAD~3", "the flag wins"


def test_the_skill_still_asks_for_the_film_script():
    """Cutting SKILL.md from 2333 lines to 782 dropped the one line asking the model to
    write `.human-review/feature-script.js`; every run since filmed nothing (exit 2)."""
    skill = _skill()
    assert ".human-review/feature-script.js" in skill
    assert "reference/feature-script.md" in skill


# ── +1 LLM script: the film's script is model-owned, and its absence is not a refusal ──

def test_the_film_script_is_model_owned_but_not_required():
    """The second model-written artifact, beside the matrix — listed as such, and kept out
    of the refusal: no script means no film, a state the Demo tab already names."""
    assert "feature-script.js" in refresh.MODEL_OWNED_OPTIONAL
    assert "rerun-film.py" in refresh.MODEL_OWNED_OPTIONAL["feature-script.js"]
    assert "feature-script.js" not in refresh.MODEL_OWNED


def test_a_review_without_a_film_script_still_refreshes(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HUMAN_REVIEW_FEATURE_SCRIPT", raising=False)
    monkeypatch.setattr(refresh, "broken_points_promise", lambda base: None)
    d = _review(tmp_path)
    assert refresh.missing_model_work(d) == []
    assert refresh.main(["--dry-run", "--no-serve", "--steps", "video"]) == 0
    err = capsys.readouterr().err
    # Said, so the reader knows who writes it — and only as a note.
    assert "feature-script.js" in err and "rerun-film.py" in err
    # Not said when the film is not being asked for.
    assert refresh.main(["--dry-run", "--no-serve", "--steps", "static"]) == 0
    assert "rerun-film.py" not in capsys.readouterr().err


def test_a_refresh_never_writes_or_touches_the_film_script(tmp_path, monkeypatch):
    """Nothing the refresh runs is the model step: the plan names no rerun-film.py, and a
    dry run with the film asked for leaves the script byte-for-byte where it was."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(refresh, "broken_points_promise", lambda base: None)
    d = _review(tmp_path)
    script = d / "feature-script.js"
    script.write_text("module.exports = async () => ({ok: true});\n", encoding="utf-8")
    before = script.stat().st_mtime_ns, script.read_bytes()
    for steps in ("all", "video", "static", "none"):
        cmds = refresh.plan(d, steps, None, False, False, None)
        assert not any("rerun-film.py" in part or "rerun-model.py" in part
                       for c in cmds for part in c), steps
        assert refresh.main(["--dry-run", "--no-serve", "--steps", steps]) == 0
    assert (script.stat().st_mtime_ns, script.read_bytes()) == before
    assert refresh.film_script(d) == script
