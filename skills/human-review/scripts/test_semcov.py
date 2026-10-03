#!/usr/bin/env python3
"""The Tests tab's matrix, drawn by a script: ticket left, coverage right, pairing between.

`semcov.py` replaced a model that wrote the whole matrix — HTML, CSS and script — on every
paid run. What it still takes from a model is one JSON file, and only for the sentences
the scripted pairing could not decide. This file holds each half to what it promised:

- the ticket splits into sentences with ids that survive an edit elsewhere in it;
- the right-hand column is the tests whose coverage runs a changed line, and only those;
- a mapping is checked against `reference/test-mapping.schema.json` and against the facts;
- the same inputs draw the same bytes, coloured as the mapping says;
- a model-written matrix from an older run is kept until a mapping exists;
- the scripted pairing finds the obvious links on its own, with the evidence beside them,
  and on the demo PR it agrees with the paid run's pairing at least as well as it did when
  it was written (skipped where that checkout is not on disk).
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


S = _load("semcov", "semcov.py")

TICKET = """The Visit should be linked to the vet that attended. Visit should show its vet.

---

## What it has to do

1. **Booking a visit lets you choose the vet, and lets you not choose one.** The vet is optional.
2. **Editing a visit can change the vet.** Clearing the field must persist as empty.

## Out of scope

Searching visits by vet, e.g. by name.
"""


# --- the schema ------------------------------------------------------------------------

GOOD = {"schema": "test-mapping/1", "sentences": [
    {"id": "s1a2b3c", "coverage": "covered",
     "tests": [{"id": "src/VisitTest.java:12", "strength": "asserted", "why": "asserts it"}]},
    {"id": "s4d5e6f", "coverage": "missing", "tests": [], "gap": "nothing reads it back"},
    {"id": "s0a0b0c-2", "coverage": "n/a", "tests": []}]}


def test_a_good_mapping_passes_the_schema_and_the_facts():
    assert S.problems(GOOD) == []
    assert S.problems(GOOD, {"s1a2b3c", "s4d5e6f", "s0a0b0c-2"}, {"src/VisitTest.java:12"}) == []


@pytest.mark.parametrize("mutate, says", [
    (lambda d: d.pop("schema"), "missing required `schema`"),
    (lambda d: d["sentences"][0].update(coverage="mostly"), "must be one of"),
    (lambda d: d["sentences"][0]["tests"][0].update(strength="strong"), "must be one of"),
    (lambda d: d["sentences"][0]["tests"][0].update(id="no line number"), "does not match"),
    (lambda d: d["sentences"][0].update(id="sentence-1"), "does not match"),
    (lambda d: d["sentences"][0].update(extra=1), "unknown key `extra`"),
    (lambda d: d["sentences"][1]["tests"].append(
        {"id": "a.java:1", "strength": "asserted"}), "a sentence nothing covers has no tests"),
    (lambda d: d["sentences"][0]["tests"][0].update(strength="exercised"),
     "needs at least one asserted test"),
    (lambda d: d["sentences"].append(dict(d["sentences"][1])), "appears twice"),
])
def test_a_malformed_mapping_is_refused_and_says_why(mutate, says):
    doc = json.loads(json.dumps(GOOD))
    mutate(doc)
    got = S.problems(doc)
    assert any(says in p for p in got), got


def test_the_facts_a_schema_cannot_check():
    """A model may only answer for the sentences it was asked about, with the tests it was
    shown — an id it made up resolves to nothing, and the page would draw a row for it."""
    got = S.problems(GOOD, {"s1a2b3c"}, set())
    assert any("not a sentence it was asked about" in p for p in got)
    assert any("not one of the tests listed" in p for p in got)


def test_the_schema_is_real_json_schema_too():
    """The skill checks with its own small validator; the real package must agree."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = S.load_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(GOOD, schema)
    bad = json.loads(json.dumps(GOOD))
    bad["sentences"][0]["coverage"] = "mostly"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


# --- the ticket -------------------------------------------------------------------------

def test_the_ticket_splits_into_sentences_in_its_own_shape():
    blocks = S.parse_ticket(TICKET)
    kinds = [b["kind"] for b in blocks]
    assert kinds == ["p", "hr", "h", "ol", "h", "p"]
    ol = blocks[3]
    assert ol["start"] == 1 and len(ol["items"]) == 2
    first = [s["text"] for s in ol["items"][0]["sentences"]]
    # The bold lead sentence is one sentence, its markup whole, and not cut at its comma.
    assert first == ["Booking a visit lets you choose the vet, and lets you not choose one.",
                     "The vet is optional."]
    assert ol["items"][0]["sentences"][0]["md"].startswith("**") \
        and ol["items"][0]["sentences"][0]["md"].endswith("**")
    # `e.g.` does not end a sentence; a heading is a section, not a sentence.
    last = blocks[-1]["sentences"]
    assert [s["text"] for s in last] == ["Searching visits by vet, e.g. by name."]
    assert last[0]["section"] == "Out of scope"
    assert len(S.ticket_sentences(blocks)) == 7


def test_sentence_ids_survive_an_edit_elsewhere_in_the_ticket():
    """The id is the sentence's own words: a model's answer about sentence three still
    applies after somebody rewords sentence one."""
    before = {s["text"]: s["id"] for s in S.ticket_sentences(S.parse_ticket(TICKET))}
    edited = TICKET.replace("that attended.", "that attended the consultation.")
    after = {s["text"]: s["id"] for s in S.ticket_sentences(S.parse_ticket(edited))}
    kept = set(before) & set(after)
    assert len(kept) == len(before) - 1
    assert all(before[t] == after[t] for t in kept)
    assert all(S.problems({"schema": "test-mapping/1", "sentences": [
        {"id": i, "coverage": "missing", "tests": []}]}) == [] for i in after.values())


def test_a_repeated_sentence_gets_its_own_id():
    ids = [s["id"] for s in S.ticket_sentences(S.parse_ticket("Must work. Must work.\n"))]
    assert len(set(ids)) == 2 and ids[1] == ids[0] + "-2"


# --- a small repository, measured -------------------------------------------------------

def _repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repository with three tests and one changed source file, and a review directory
    whose coverage says two of the tests run a changed line and one does not."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Visit.java").write_text(
        "class Visit {\n  Vet vet;\n  void setVet(Vet v) { this.vet = v; }\n"
        "  Vet getVet() { return vet; }\n}\n", encoding="utf-8")
    (root / "test").mkdir()
    (root / "test" / "VisitTest.java").write_text(
        "class VisitTest {\n"
        "  @Test\n"
        "  void create_withoutVet_leavesItUnassigned() {\n"
        "    post(visit());\n"
        "    assertThat(saved().getVet()).isNull();\n"
        "  }\n"
        "  @Test\n"
        "  void update_changesTheVet() {\n"
        "    put(visit().setVet(helen));\n"
        "    assertThat(saved().getVet()).isEqualTo(helen);\n"
        "  }\n"
        "  @Test\n"
        "  void owners_areListed() {\n"
        "    assertThat(get(\"/owners\")).hasSize(3);\n"
        "  }\n"
        "}\n", encoding="utf-8")
    review = root / ".human-review"
    (review / "assets").mkdir(parents=True)
    (review / "content.json").write_text(json.dumps(
        {"pr": {"repo": "https://github.com/acme/clinic",
                "ticket": {"number": 7, "title": "Visit has a vet", "url": "u"}},
         "testChanges": "assets/test-changes.json"}), encoding="utf-8")
    (review / "ticket-body.json").write_text(json.dumps(
        {"number": 7, "title": "Visit has a vet", "url": "u", "author": "ana", "avatar": "",
         "createdAt": "2026-06-13T09:31:55Z",
         "body": "1. **Booking a visit lets you leave the vet unassigned.**\n"
                 "2. **Editing a visit changes the vet.**\n"
                 "3. Owners are greeted by a pirate.\n"}), encoding="utf-8")
    test = "test/VisitTest.java"
    hits = {"src/Visit.java": [3, 4]}
    (review / "assets" / "test-coverage.json").write_text(json.dumps({
        "version": 1, "changed": {"src/Visit.java": [3, 4]},
        "executable": {"src/Visit.java": [3, 4]}, "unmeasurable": [], "suites": [],
        "tests": [
            {"id": "1", "suite": "Backend JUnit", "title": "create_withoutVet_leavesItUnassigned",
             "file": test, "line": 3, "source": "jacoco", "hits": hits},
            {"id": "2", "suite": "Backend JUnit", "title": "update_changesTheVet",
             "file": test, "line": 8, "source": "jacoco", "hits": hits},
            {"id": "3", "suite": "Backend JUnit", "title": "owners_areListed",
             "file": test, "line": 13, "source": "jacoco", "hits": {}},
        ]}), encoding="utf-8")
    (review / "assets" / "test-changes.json").write_text(json.dumps({"tests": [
        {"name": "create_withoutVet_leavesItUnassigned", "path": test, "line": 3,
         "status": "added"},
        {"name": "update_changesTheVet", "path": test, "line": 8, "status": "modified"},
        {"name": "owners_areListed", "path": test, "line": 13, "status": "unchanged"}]}),
        encoding="utf-8")
    return root, review


def test_the_right_column_is_only_the_tests_that_run_a_changed_line(tmp_path):
    root, review = _repo(tmp_path)
    rows, measured = S.covering_tests(S._spec(review), review, root)
    assert measured
    assert [r["id"] for r in rows] == ["test/VisitTest.java:3", "test/VisitTest.java:8"]
    assert [r["status"] for r in rows] == ["new", "changed"]
    assert {r["cat"] for r in rows} == {"unit"}


def test_without_a_coverage_run_the_column_is_the_branch_s_own_tests(tmp_path):
    root, review = _repo(tmp_path)
    (review / "assets" / "test-coverage.json").unlink()
    rows, measured = S.covering_tests(S._spec(review), review, root)
    assert not measured
    assert len(rows) == 3


def test_the_script_pairs_what_shared_evidence_decides_and_leaves_the_rest_open(tmp_path):
    root, review = _repo(tmp_path)
    g = S.gather(S._spec(review), review, root)
    by_text = {s["id"]: s["text"] for s in g["sentences"]}
    decided = {by_text[e["id"]]: e for e in g["scripted"]["decided"]}
    book = decided["Booking a visit lets you leave the vet unassigned."]
    assert [t["id"] for t in book["tests"]] == ["test/VisitTest.java:3"]
    assert book["tests"][0]["strength"] == "asserted"
    # Every scripted link says what it was made on.
    assert book["tests"][0]["by"] == "script" and book["tests"][0]["evidence"]
    edit = decided["Editing a visit changes the vet."]
    assert [t["id"] for t in edit["tests"]] == ["test/VisitTest.java:8"]
    # Nothing on the card shares a word with the pirate: the model's to decide.
    open_texts = {by_text[sid] for sid in g["scripted"]["open"]}
    assert open_texts == {"Owners are greeted by a pirate."}
    # Every sentence that makes a claim goes to the model in the one call — the paired ones
    # with the script's links to confirm or reject, the open one with its candidates.
    asked = S.model_input(g["ticket"], g["sentences"], g["rows"], g["scripted"], g["docs"])
    got = {s["text"]: s for s in asked["sentences"]}
    assert set(got) == set(by_text.values())
    assert [t["id"] for t in got["Booking a visit lets you leave the vet unassigned."]
            ["scripted"]] == ["test/VisitTest.java:3"]
    assert got["Owners are greeted by a pirate."]["scripted"] == []


# --- the page ---------------------------------------------------------------------------

def _render(tmp_path):
    root, review = (tmp_path / "repo", tmp_path / "repo" / ".human-review") \
        if (tmp_path / "repo").is_dir() else _repo(tmp_path)
    g = S.gather(S._spec(review), review, root)
    pirate = next(sid for sid in g["scripted"]["open"])
    model = {"schema": "test-mapping/1",
             "sentences": [{"id": pirate, "coverage": "missing", "tests": []}]}
    entries = S.merge(g["sentences"], g["scripted"], model)
    return g, entries, S.render(g["ticket"], g["blocks"], g["rows"], entries, root), pirate


def test_the_same_inputs_draw_the_same_bytes(tmp_path):
    _, _, first, _ = _render(tmp_path / "a")
    _, _, again, _ = _render(tmp_path / "a")
    assert first == again
    assert S.GENERATED in first and 'class="rm-data"' in first


def test_sentences_are_coloured_as_the_mapping_says(tmp_path):
    g, entries, page, pirate = _render(tmp_path)
    cov = {e["id"]: e["coverage"] for e in entries}
    for sid, c in cov.items():
        assert f'data-s="{sid}" data-cov="{S.COV_ATTR[c]}"' in page
    # The model's sentence carries the robot; the script's do not.
    assert f'data-s="{pirate}" data-cov="missing" data-src="model"' in page
    data = json.loads(page.split('class="rm-data">')[1].split("</script>")[0])
    assert data["sentences"][pirate]["by"] == "model"
    assert set(data["tests"]) == {r["id"] for r in g["rows"]}
    # The ticket keeps its numbered list and its author.
    assert "<ol start=\"1\">" in page and ">ana</span>" in page


def test_who_paired_a_sentence_is_said_on_its_hover_and_once_not_after_every_clause(tmp_path):
    """Eval run 8: a 🤖 after each of 30+ highlighted clauses made the ticket column
    unreadable. The provenance is on the sentence's hover and in one note under the header."""
    _, entries, page, _ = _render(tmp_path)
    css = (S.ASSETS / "reqmap.css").read_text(encoding="utf-8")
    assert "[data-src=model]::after" not in css
    assert page.count(S.AI_NOTE) == 1 and 'class="rm-ainote"' in page
    js = (S.ASSETS / "reqmap.js").read_text(encoding="utf-8")
    assert "'🤖 checked by AI':'paired by script'" in js
    # No model answer, no note.
    root, review = tmp_path / "repo", tmp_path / "repo" / ".human-review"
    g = S.gather(S._spec(review), review, root)
    bare = S.render(g["ticket"], g["blocks"], g["rows"],
                    S.merge(g["sentences"], g["scripted"], None), root)
    assert S.AI_NOTE not in bare


def test_a_sentence_nobody_paired_is_not_called_missing(tmp_path):
    root, review = _repo(tmp_path)
    g = S.gather(S._spec(review), review, root)
    entries = S.merge(g["sentences"], g["scripted"], None)
    page = S.render(g["ticket"], g["blocks"], g["rows"], entries, root)
    assert page.count('data-cov="unmapped"') == 1
    assert S.split_counts(entries)["sentences"]["unmapped"] == 1


def test_a_model_written_matrix_is_kept_until_a_mapping_exists(tmp_path, capsys):
    """Back-compat: an older run's matrix is rendered as it is, with a note, and only
    replaced — and kept in `.model-prev/` — once `test-mapping.json` exists."""
    root, review = _repo(tmp_path)
    frag = review / S.FRAGMENT
    frag.write_text('<div class="reqmap">the model\'s</div>', encoding="utf-8")
    assert S.write_fragment(S._spec(review), review, root) == "kept the model-written matrix"
    assert frag.read_text() == '<div class="reqmap">the model\'s</div>'
    assert "no test-mapping.json" in capsys.readouterr().err
    T = importlib.import_module("hrbuild.tabs.tests")
    assert T.scripted_reqmap(S._spec(review), review, root) is None

    (review / S.MAPPING).write_text(json.dumps({"schema": "test-mapping/1", "sentences": []}),
                                    encoding="utf-8")
    said = T.scripted_reqmap(S._spec(review), review, root)
    assert said and "by script" in said
    assert S.GENERATED in frag.read_text()
    assert (review / ".model-prev" / "requirements-map.html").read_text() \
        == '<div class="reqmap">the model\'s</div>'
    merged = json.loads((review / S.MERGED).read_text())
    assert "links by script" in merged["note"]


def test_the_build_relays_a_scripted_matrix_without_rewording_its_card(tmp_path):
    """`reqmap_layout` re-lays the scripted matrix like the model's, but its card already
    says what it lists — the model's `Semantic test coverage` strip is not put back on it."""
    root, review = _repo(tmp_path)
    (review / S.MAPPING).write_text(json.dumps({"schema": "test-mapping/1", "sentences": []}),
                                    encoding="utf-8")
    S.write_fragment(S._spec(review), review, root)
    T = importlib.import_module("hrbuild.tabs.tests")
    out = T.reqmap_layout((review / S.FRAGMENT).read_text(), S._spec(review), review, root)
    assert 'class="rm-head"' in out and "Issue <span class=\"rm-num\">#7</span>" in out
    assert T.COVCARD_WHO in out and T.CARD_WHO not in out


# --- a keyword match is a candidate, not proof ------------------------------------------
#
# Run 5 painted "The grid should be sortable by any column" fully covered: the script linked
# it to a scenario that checks the page size, on the words `grid` and `sort`, and no model
# was ever asked because every sentence had a link. A scripted link is now a candidate the
# model confirms or rejects, and a sentence is green only on a test the model says asserts it.

def _sids(g):
    by_text = {s["text"]: s["id"] for s in g["sentences"]}
    return (by_text["Booking a visit lets you leave the vet unassigned."],
            by_text["Editing a visit changes the vet."],
            by_text["Owners are greeted by a pirate."])


def test_an_unconfirmed_scripted_link_is_never_drawn_as_covered(tmp_path):
    root, review = _repo(tmp_path)
    g = S.gather(S._spec(review), review, root)
    book, edit, _ = _sids(g)
    entries = {e["id"]: e for e in S.merge(g["sentences"], g["scripted"], None)}
    assert entries[book]["coverage"] == "unconfirmed" and entries[book]["tests"]
    page = S.render(g["ticket"], g["blocks"], g["rows"], list(entries.values()), root)
    assert 'data-cov="covered"' not in page.split('class="rm-legend"')[1].split("</div>", 1)[1]
    assert f'data-s="{book}" data-cov="unconfirmed"' in page
    # The legend names the state it introduces, and only when the page uses it.
    assert 'class="rm-lg" data-cov="unconfirmed"' in page
    assert 'class="rm-lg" data-cov="narrowed"' not in page


def _answer(book, edit, pirate, book_verdict="confirm"):
    keep = book_verdict == "confirm"
    return {"schema": "test-mapping/1", "sentences": [
        {"id": book, "coverage": "covered" if keep else "missing",
         "tests": [{"id": "test/VisitTest.java:3", "strength": "asserted",
                    "why": "asserts the saved vet is null"}] if keep else [],
         "review": [{"id": "test/VisitTest.java:3", "verdict": book_verdict,
                     "why": "reads the vet back" if keep else "never reads the vet"}]},
        {"id": edit, "coverage": "narrowed", "decision": "d1",
         "tests": [{"id": "test/VisitTest.java:8", "strength": "asserted",
                    "why": "asserts the new vet"}],
         "review": [{"id": "test/VisitTest.java:8", "verdict": "confirm",
                     "why": "changes the vet and reads it back"}]},
        {"id": pirate, "coverage": "missing", "tests": []}]}


def _with_decision(review):
    (review / "review-points.json").write_text(json.dumps({"assumptions": [
        {"title": "Only the vet's name is editable", "alternative": "edit any field",
         "why": "the ticket's scope"}], "findings": []}), encoding="utf-8")


def test_the_model_confirms_rejects_and_narrows_in_one_answer(tmp_path):
    root, review = _repo(tmp_path)
    _with_decision(review)
    g = S.gather(S._spec(review), review, root)
    book, edit, pirate = _sids(g)
    assert [d["id"] for d in g["decisions"]] == ["d1"]
    asked = S.model_input(g["ticket"], g["sentences"], g["rows"], g["scripted"], g["docs"],
                          g["decisions"])
    assert asked["decisions"][0]["text"].startswith("Assumed: Only the vet's name is editable")
    links = S.scripted_links(g["scripted"])
    ok = _answer(book, edit, pirate)
    assert S.problems(ok, {x["id"] for x in asked["sentences"]},
                      {t["id"] for t in asked["tests"]}, links, {"d1"}) == []

    entries = {e["id"]: e for e in S.merge(g["sentences"], g["scripted"], ok, g["decisions"])}
    assert entries[book]["coverage"] == "covered" and entries[book]["by"] == "model"
    # The script's evidence stays beside the link the model confirmed.
    assert entries[book]["tests"][0]["evidence"]
    assert entries[edit]["coverage"] == "narrowed"
    page = S.render(g["ticket"], g["blocks"], g["rows"], list(entries.values()), root)
    data = json.loads(page.split('class="rm-data">')[1].split("</script>")[0])
    assert "Only the vet's name is editable" in data["sentences"][edit]["gap"]
    assert data["sentences"][edit]["gapKind"] == "requirement"

    # Rejected: off the sentence's tests, on its record with the reason, and not green.
    no = _answer(book, edit, pirate, book_verdict="reject")
    entries = {e["id"]: e for e in S.merge(g["sentences"], g["scripted"], no, g["decisions"])}
    assert entries[book]["coverage"] == "missing" and entries[book]["tests"] == []
    assert entries[book]["rejected"] == [{"id": "test/VisitTest.java:3",
                                         "why": "never reads the vet"}]
    assert S.split_counts(list(entries.values()))["rejected"] == 1


@pytest.mark.parametrize("mutate, says", [
    (lambda d: d["sentences"][0].pop("review"), "neither confirmed nor rejected"),
    (lambda d: d["sentences"][0]["review"][0].update(verdict="reject"),
     "rejected but still in `tests`"),
    (lambda d: d["sentences"][0]["tests"].clear() or d["sentences"][0].update(
        coverage="missing"), "confirmed but not in `tests`"),
    (lambda d: d["sentences"][2].update(review=[
        {"id": "test/VisitTest.java:3", "verdict": "reject", "why": "x"}]),
     "not a link the script made"),
    (lambda d: d["sentences"][1].pop("decision"), "names the recorded decision"),
    (lambda d: d["sentences"][1].update(decision="d9"), "not one of the decisions listed"),
    (lambda d: d["sentences"][0]["review"][0].pop("why"), "missing required `why`"),
])
def test_an_answer_that_skips_or_fudges_a_verdict_is_refused(tmp_path, mutate, says):
    root, review = _repo(tmp_path)
    g = S.gather(S._spec(review), review, root)
    doc = _answer(*_sids(g))
    mutate(doc)
    got = S.problems(doc, None, None, S.scripted_links(g["scripted"]), {"d1"})
    assert any(says in p for p in got), got


# --- the OpenSpec change under the issue ---------------------------------------------------

SPEC_MD = """# Spec Delta

## ADDED Requirements

### Requirement: Paging inputs
The API SHALL use pages of 5, 10 or 20 owners.

#### Scenario: Invalid size
- **WHEN** a user supplies `size=7`
- **THEN** the API returns HTTP 400

### Requirement: Business-key sorting
The API SHALL accept only `sort=name` or `sort=city`.

## REMOVED Requirements

### Requirement: Unbounded list
The API SHALL return every owner.
"""


def _with_spec(root):
    d = root / "openspec" / "changes" / "paginate-owners"
    (d / "specs" / "owner-list").mkdir(parents=True)
    (d / "specs" / "owner-list" / "spec.md").write_text(SPEC_MD, encoding="utf-8")
    (d / "proposal.md").write_text(
        "## Why\n\nIssue #7 asks for it.\n\n- Sorting is limited to Name and City, "
        "narrowing the original request.\n- Out of scope: sorting by Telephone.\n",
        encoding="utf-8")


def test_the_spec_requirements_are_numbered_under_the_issue_and_paired_too(tmp_path):
    """Run 5: the issue had 2 bullets, its OpenSpec change 10 requirements, and the matrix
    mapped only the 2. The change is found by the issue number its proposal mentions."""
    root, review = _repo(tmp_path)
    _with_spec(root)
    g = S.gather(S._spec(review), review, root)
    heading = S.SPEC_HEADING.format(name="paginate-owners")
    kinds = [(b["kind"], b.get("text")) for b in g["blocks"]]
    assert ("h", heading) in kinds
    spec_ol = g["blocks"][kinds.index(("h", heading)) + 1]
    assert spec_ol["kind"] == "ol" and len(spec_ol["items"]) == 2, "REMOVED is not a claim"
    first = spec_ol["items"][0]["sentences"][0]
    assert first["text"].startswith("Paging inputs — The API SHALL use pages of 5, 10 or 20")
    assert first["requirement"] == "Paging inputs"
    assert first["scenarios"] == ["Invalid size: WHEN a user supplies size=7 THEN the API "
                                  "returns HTTP 400"]
    # The issue still comes first, and the provenance line says both, in plain words.
    assert g["sentences"][0]["text"] == "Booking a visit lets you leave the vet unassigned."
    assert g["ticket"]["origin"].endswith(
        "then the 2 requirements of the OpenSpec change paginate-owners")
    asked = S.model_input(g["ticket"], g["sentences"], g["rows"], g["scripted"], g["docs"],
                          g["decisions"])
    by_req = [x for x in asked["sentences"] if x.get("requirement") == "Paging inputs"]
    assert by_req and by_req[0]["scenarios"]
    # The proposal's scope cuts are decisions the model reads.
    texts = [d["text"] for d in g["decisions"]]
    assert any("Sorting is limited to Name and City" in t for t in texts), texts
    assert any("Out of scope: sorting by Telephone" in t for t in texts), texts


def test_a_change_for_another_issue_is_not_appended(tmp_path):
    root, review = _repo(tmp_path)
    _with_spec(root)
    (root / "openspec" / "changes" / "paginate-owners" / "proposal.md").write_text(
        "Issue #70 asks for it.\n", encoding="utf-8")
    g = S.gather(S._spec(review), review, root)
    assert not any(b["kind"] == "h" for b in g["blocks"])
    assert "OpenSpec" not in g["ticket"]["origin"]


# --- why a test is on the card --------------------------------------------------------------

def test_every_test_says_why_it_is_listed_and_the_ones_about_the_change_come_first(tmp_path):
    root, review = _repo(tmp_path)
    g = S.gather(S._spec(review), review, root)
    rows = {r["id"]: dict(r) for r in g["rows"]}
    rows["test/VisitTest.java:8"]["status"] = "unchanged"
    rows["test/VisitTest.java:8"]["aimed"] = False
    paired = {"test/VisitTest.java:3"}
    assert S.test_rank(rows["test/VisitTest.java:3"], paired) == 0
    assert S.test_rank(rows["test/VisitTest.java:8"], set()) == 3
    assert S.test_rank({**rows["test/VisitTest.java:8"], "aimed": True}, set()) == 2
    assert S.test_rank({**rows["test/VisitTest.java:8"], "status": "new"}, set()) == 1
    assert S.test_why(rows["test/VisitTest.java:3"]) == \
        "its coverage ran changed lines of Visit.java 3–4"
    page = S.render(g["ticket"], g["blocks"], g["rows"],
                    S.merge(g["sentences"], g["scripted"], None), root)
    data = json.loads(page.split('class="rm-data">')[1].split("</script>")[0])
    assert data["tests"]["test/VisitTest.java:3"]["why"].startswith("its coverage ran")
    assert set(data["ranks"]) == {"0", "1", "2", "3"}
    js = (S.ASSETS / "reqmap.js").read_text(encoding="utf-8")
    assert "rank(a)-rank(b)" in js and "rm-tgroup" in js and "t.why" in js


def test_the_untouched_and_unpaired_groups_start_folded_behind_a_count(tmp_path):
    """Run 6: the two "untouched and unpaired" groups listed 33 tests about something else
    (UserTest, SpecialtyTest, VisitDateRangeTest…) and the tab came out twice the
    reference's height. They fold behind one button that counts them; the paired and the
    branch-written groups never fold."""
    root, review = _repo(tmp_path)
    g = S.gather(S._spec(review), review, root)
    page = S.render(g["ticket"], g["blocks"], g["rows"],
                    S.merge(g["sentences"], g["scripted"], None), root)
    data = json.loads(page.split('class="rm-data">')[1].split("</script>")[0])
    assert data["fold"] == {"from": 2, "label": "more tests that only pass through changed code"}
    assert S.FOLD_FROM_RANK == 2, "rank 0 (paired) and 1 (written by the branch) stay open"
    js = (S.ASSETS / "reqmap.js").read_text(encoding="utf-8")
    # Folded only when something stays open above; counted on the button; toggled by it.
    assert "rank(id)<F.from" in js and "rank(id)>=F.from" in js
    assert "foldN+' '" in js and "'hide':'show'" in js
    assert "row.dataset.fold='yes'" in js and "g.dataset.fold='yes'" in js
    assert "closest('.rm-fold')" in js
    css = (S.ASSETS / "reqmap.css").read_text(encoding="utf-8")
    assert ".rm-list[data-unfold=no] [data-fold=yes]{display:none}" in css


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_matrix_script_still_parses():
    import subprocess
    got = subprocess.run(["node", "--check", str(S.ASSETS / "reqmap.js")],
                         capture_output=True, text=True)
    assert got.returncode == 0, got.stderr


def test_a_narrowed_sentence_names_and_links_the_decision_it_rests_on(tmp_path):
    """Run 6: "The grid should be sortable by any column" popped only "narrowed by a
    recorded decision". The decision is the proposal's line 86, and the hover and the box
    now say so — the box with a link to the line."""
    root, review = _repo(tmp_path)
    _with_spec(root)
    g = S.gather(S._spec(review), review, root)
    dec = next(d for d in g["decisions"] if "Sorting is limited" in d["text"])
    assert dec["where"] == "proposal.md:5"
    assert dec["quote"].startswith("Sorting is limited to Name and City")
    assert dec["href"].startswith("vscode://file/") and dec["href"].endswith(
        "openspec/changes/paginate-owners/proposal.md:5:1")
    # The model reads the decision by id and text only; where it lives is the page's.
    asked = S.model_input(g["ticket"], g["sentences"], g["rows"], g["scripted"], g["docs"],
                          g["decisions"])
    assert all(set(d) == {"id", "text"} for d in asked["decisions"])

    book, edit, pirate = _sids(g)
    answer = _answer(book, edit, pirate)
    answer["sentences"][1]["decision"] = dec["id"]
    entries = S.merge(g["sentences"], g["scripted"], answer, g["decisions"])
    page = S.render(g["ticket"], g["blocks"], g["rows"], entries, root)
    data = json.loads(page.split('class="rm-data">')[1].split("</script>")[0])
    s = data["sentences"][edit]
    assert s["decisionWhere"] == "proposal.md:5" and s["decisionHref"] == dec["href"]
    assert s["decisionName"].startswith("proposal.md:5 “Sorting is limited to Name and City")
    # The sentence's own hover (before the script swaps it for the badge tip) names it too.
    assert "narrowed on purpose — not delivered as written — by proposal.md:5 “Sorting" \
        in page
    js = (S.ASSETS / "reqmap.js").read_text(encoding="utf-8")
    assert "narrowed by '" in js and "s.decisionName" in js
    assert "'Recorded in '+w" in js and "s.decisionHref" in js


def test_a_long_decision_is_cut_on_the_hover_and_kept_whole_in_the_box():
    entry = {"decisionRef": {"where": "design.md:12", "quote": "word " * 40}}
    name = S.decision_name(entry)
    assert name.startswith("design.md:12 “word") and name.endswith("…”")
    assert len(name) < S.DECISION_TIP_MAX + 20
    assert S.decision_name({}) == ""


def test_a_deleted_test_on_an_unmeasured_card_links_to_the_base_commit(tmp_path):
    """Its HEAD `line` is where the removal landed — unrelated code. The card links the
    blob at the base commit instead, and says so on the hover."""
    root, review = _repo(tmp_path)
    (review / "assets" / "test-coverage.json").unlink()
    url = "https://github.com/acme/clinic/blob/abc123/test/VisitTest.java#L20"
    doc = json.loads((review / "assets" / "test-changes.json").read_text())
    doc["tests"].append({"name": "obsolete", "path": "test/VisitTest.java", "line": 16,
                         "status": "deleted", "baseLine": 20, "baseUrl": url})
    (review / "assets" / "test-changes.json").write_text(json.dumps(doc))
    g = S.gather(S._spec(review), review, root)
    page = S.render(g["ticket"], g["blocks"], g["rows"],
                    S.merge(g["sentences"], g["scripted"], None), root, g["measured"])
    data = json.loads(page.split('class="rm-data">')[1].split("</script>")[0])
    gone = next(t for t in data["tests"].values() if t["title"] == "obsolete")
    assert gone["status"] == "deleted" and gone["href"] == url
    assert gone["hrefTip"] == S.DELETED_HREF_TIP and gone["parts"] == []
    assert "t.hrefTip||'Open in VS Code'" in (S.ASSETS / "reqmap.js").read_text()


# --- the model step, end to end, with `claude` stubbed --------------------------------------

NO_CHANGE = {"schema": "test-mapping-check/1", "sentences": []}


def _fake_claude(tmp_path, answer: dict, check: dict | None = None) -> Path:
    """A `claude` on PATH that records its stdin and answers in the CLI's JSON envelope —
    no network, no spend. The pairing prompt gets `answer` (stdin in `prompt.txt`); the
    second read's gets `check` (stdin in `check-prompt.txt`), by default a read that lowers
    nothing."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                   "text = sys.stdin.read()\n"
                   "second = 'test-mapping-check-input' in text\n"
                   f"open({str(tmp_path / 'check-prompt.txt')!r} if second else "
                   f"{str(tmp_path / 'prompt.txt')!r}, 'w').write(text)\n"
                   "reply = " + repr(check or NO_CHANGE) + " if second else " + repr(answer)
                   + "\nprint(json.dumps({'total_cost_usd': 0.0021, 'result': json.dumps(reply),"
                   " 'modelUsage': {'claude-haiku-4-5': {'inputTokens': 100, "
                   "'outputTokens': 10}}}))\n", encoding="utf-8")
    exe.chmod(0o755)
    return bin_dir


def test_the_model_step_asks_about_every_scripted_link_in_one_call(tmp_path, monkeypatch):
    """The run-5 hole: every sentence had a scripted link, so rerun-model.py wrote "the
    script paired every sentence; no model was asked" and the keyword match went out green."""
    root, review = _repo(tmp_path)
    _with_decision(review)
    g = S.gather(S._spec(review), review, root)
    answer = _answer(*_sids(g))
    import os
    monkeypatch.setenv("PATH", f"{_fake_claude(tmp_path, answer)}{os.pathsep}"
                               + os.environ["PATH"])
    monkeypatch.chdir(root)
    RM = _load("rerun_model", "rerun-model.py")
    assert RM.main(["--dir", str(review)]) == 0
    prompt = (tmp_path / "prompt.txt").read_text()
    assert prompt.count('"scripted"') == 3 and '"decisions"' in prompt
    assert "test/VisitTest.java:3" in prompt
    written = json.loads((review / S.MAPPING).read_text())
    assert written == answer
    said = S.write_fragment(S._spec(review), review, root)
    assert "1 rejected" not in said and "by model" in said
    page = (review / S.FRAGMENT).read_text()
    book, edit, _ = _sids(g)
    assert f'data-s="{book}" data-cov="covered" data-src="model"' in page
    assert f'data-s="{edit}" data-cov="narrowed" data-src="model"' in page


def test_a_sentence_missing_a_verdict_is_dropped_and_stays_unconfirmed(tmp_path, monkeypatch):
    """One sentence's slip costs that sentence, not the whole paid answer — and never turns
    it green: dropped, it shows its scripted links as unconfirmed."""
    root, review = _repo(tmp_path)
    _with_decision(review)
    g = S.gather(S._spec(review), review, root)
    book, edit, _ = _sids(g)
    answer = _answer(*_sids(g))
    answer["sentences"][0].pop("review")
    import os
    monkeypatch.setenv("PATH", f"{_fake_claude(tmp_path, answer)}{os.pathsep}"
                               + os.environ["PATH"])
    monkeypatch.chdir(root)
    RM = _load("rerun_model", "rerun-model.py")
    assert RM.main(["--dir", str(review)]) == 0
    written = json.loads((review / S.MAPPING).read_text())
    assert [e["id"] for e in written["sentences"]] == [edit, _sids(g)[2]]
    assert "1 sentence(s) dropped" in written["note"] and book in written["note"]
    assert (review / ".model-prev" / "test-mapping.refused.json").is_file()
    S.write_fragment(S._spec(review), review, root)
    assert f'data-s="{book}" data-cov="unconfirmed"' in (review / S.FRAGMENT).read_text()


def test_a_reply_that_is_not_the_document_is_refused_whole(tmp_path, monkeypatch):
    root, review = _repo(tmp_path)
    answer = {"sentences": []}                      # no `schema`: not the document asked for
    import os
    monkeypatch.setenv("PATH", f"{_fake_claude(tmp_path, answer)}{os.pathsep}"
                               + os.environ["PATH"])
    monkeypatch.chdir(root)
    RM = _load("rerun_model", "rerun-model.py")
    assert RM.main(["--dir", str(review)]) == 5
    assert not (review / S.MAPPING).exists()
    assert (review / ".model-prev" / "test-mapping.refused.json").is_file()


def test_the_model_is_handed_its_reply_already_laid_out(tmp_path):
    root, review = _repo(tmp_path)
    g = S.gather(S._spec(review), review, root)
    asked = S.model_input(g["ticket"], g["sentences"], g["rows"], g["scripted"], g["docs"])
    form = {e["id"]: e for e in asked["answer"]["sentences"]}
    assert set(form) == {x["id"] for x in asked["sentences"]}
    book = _sids(g)[0]
    assert form[book]["review"] == [{"id": "test/VisitTest.java:3", "verdict": "", "why": ""}]


# --- a cheap model's "covered" is checked by passes that can only lower it ----------------
#
# Eval run 8: Haiku called 25 of 27 sentences covered, none partial — "authorization and MCP
# contracts unchanged" on a test that lists owners, a `confirm` with an invented reason. The
# model stays cheap; a free script rule and a second cheap read take away what does not hold.

def _check(book, verdict, line="", claim="all", unproven=""):
    return {"schema": "test-mapping-check/1", "sentences": [
        {"id": book, "claim": claim, "unproven": unproven,
         "links": [{"id": "test/VisitTest.java:3", "verdict": verdict, "line": line,
                    "why": "the second read's reason"}]}]}


def _run_model(tmp_path, monkeypatch, check=None, answer=None, argv=()):
    root, review = _repo(tmp_path)
    _with_decision(review)
    g = S.gather(S._spec(review), review, root)
    import os
    monkeypatch.setenv("PATH", f"{_fake_claude(tmp_path, answer or _answer(*_sids(g)), check)}"
                               f"{os.pathsep}" + os.environ["PATH"])
    monkeypatch.delenv("HUMAN_REVIEW_MAPPING_CHECK", raising=False)
    # The second read is haiku's; Sonnet (the default since 3 Oct 2026) answers alone.
    monkeypatch.setenv("HUMAN_REVIEW_MAPPING_MODEL", "haiku")
    monkeypatch.chdir(root)
    RM = _load("rerun_model", "rerun-model.py")
    assert RM.main(["--dir", str(review), *argv]) == 0
    written = {e["id"]: e for e in json.loads((review / S.MAPPING).read_text())["sentences"]}
    return root, review, g, written


def test_the_second_read_is_asked_about_every_kept_link_and_only_those(tmp_path, monkeypatch):
    root, review, g, _ = _run_model(tmp_path, monkeypatch)
    book, edit, pirate = _sids(g)
    chk = (tmp_path / "check-prompt.txt").read_text()
    asked = json.loads(chk.split("```json\n")[-1].split("\n```")[0])
    # The covered sentence, with its link and the body; not the narrowed, not the missing.
    assert [x["id"] for x in asked["sentences"]] == [book]
    assert asked["sentences"][0]["links"][0]["strength"] == "asserted"
    assert [t["id"] for t in asked["tests"]] == ["test/VisitTest.java:3"]
    assert "isNull()" in asked["tests"][0]["body"]
    assert asked["answer"]["sentences"][0]["links"][0] == {
        "id": "test/VisitTest.java:3", "verdict": "", "line": "", "why": ""}
    # One press, one ledger row, both calls' price on it.
    runs = json.loads((review / ".model-runs.json").read_text())["runs"]
    assert len(runs) == 1 and runs[0]["cost"] == pytest.approx(0.0042)
    assert runs[0]["tokens"] == 220


def test_a_quoted_assertion_that_is_not_in_the_body_lowers_the_link(tmp_path, monkeypatch):
    """The run-8 `confirm` with an invented reason: the second read must copy the line that
    asserts the claim, and a line that is not in the body is no assertion."""
    _, _, g, written = _run_model(tmp_path / "probe", monkeypatch)
    book = _sids(g)[0]
    assert written[book]["coverage"] == "covered"
    _, review, g, written = _run_model(
        tmp_path / "run", monkeypatch, check=_check(book, "asserts",
                                                    line="assertThat(vet).isAbsent();"))
    e = written[book]
    assert e["coverage"] == "exercised" and e["tests"][0]["strength"] == "exercised"
    assert {"id": "test/VisitTest.java:3", "by": "second read", "from": "asserted",
            "to": "exercised"}.items() <= e["downgrades"][0].items()
    assert "not in the test's body" in e["downgrades"][0]["why"]
    assert "lowered 1 link(s) and 1 sentence(s)" in json.loads(
        (review / S.MAPPING).read_text())["note"]


def test_a_real_quote_keeps_the_link_and_a_part_claim_makes_it_partial(tmp_path, monkeypatch):
    _, _, g, _ = _run_model(tmp_path / "probe", monkeypatch)
    book = _sids(g)[0]
    _, _, g, written = _run_model(
        tmp_path / "run", monkeypatch,
        check=_check(book, "asserts", line="  assertThat(saved().getVet()).isNull()  ",
                     claim="part", unproven="Nothing checks the booking is saved."))
    e = written[book]
    assert e["tests"][0]["strength"] == "asserted"
    assert e["coverage"] == "partial" and e["gap"] == "Nothing checks the booking is saved."


def test_an_unrelated_link_is_dropped_and_shown_as_rejected(tmp_path, monkeypatch):
    _, _, g, _ = _run_model(tmp_path / "probe", monkeypatch)
    book = _sids(g)[0]
    root, review, g, written = _run_model(tmp_path / "run", monkeypatch,
                                          check=_check(book, "unrelated"))
    e = written[book]
    assert e["coverage"] == "missing" and e["tests"] == []
    # The model's confirm is turned into a rejection, so the answer still passes the checks.
    assert e["review"][0]["verdict"] == "reject" and "second read" in e["review"][0]["why"]
    S.write_fragment(S._spec(review), review, root)
    page = (review / S.FRAGMENT).read_text()
    assert f'data-s="{book}" data-cov="missing"' in page
    data = json.loads(page.split('class="rm-data">')[1].split("</script>")[0])
    assert data["sentences"][book]["rejected"][0]["id"] == "test/VisitTest.java:3"


def test_the_second_read_never_raises_anything(tmp_path):
    """Downgrade-only: `claim: all` on a partial sentence, `asserts` on an exercised link,
    a verdict on a test the sentence never had — none of it moves anything up."""
    asked = {"sentences": [{"id": "s000001", "text": "x"}],
             "tests": [{"id": "a.java:1", "title": "t", "kind": "unit", "body": "assertX(1);"},
                       {"id": "b.java:2", "title": "u", "kind": "unit", "body": "assertY(2);"}]}
    doc = {"schema": "test-mapping/1", "sentences": [
        {"id": "s000001", "coverage": "partial", "gap": "half of it",
         "tests": [{"id": "a.java:1", "strength": "asserted", "why": "w"},
                   {"id": "b.java:2", "strength": "exercised", "why": "w"}]}]}
    chk = {"schema": "test-mapping-check/1", "sentences": [
        {"id": "s000001", "claim": "all", "links": [
            {"id": "a.java:1", "verdict": "asserts", "line": "assertX(1);", "why": "w"},
            {"id": "b.java:2", "verdict": "asserts", "line": "assertY(2);", "why": "w"},
            {"id": "c.java:3", "verdict": "asserts", "line": "x", "why": "w"}]}]}
    out, lowered = S.apply_check(doc, chk, asked)
    assert out == doc and lowered == {"links": 0, "sentences": 0}
    assert S.check_problems({"schema": "test-mapping/1", "sentences": []})
    assert S.check_problems(None) == ["the reply holds no JSON object"]


def test_a_link_that_shares_nothing_specific_with_its_sentence_is_dropped_by_the_script(
        tmp_path):
    root, review = _repo(tmp_path)
    g = S.gather(S._spec(review), review, root)
    book, edit, pirate = _sids(g)
    texts = {s["id"]: s["text"] for s in g["sentences"]}
    # The pirate sentence "covered" by the edit test: not one word, route or literal shared.
    doc = {"schema": "test-mapping/1", "sentences": [
        {"id": pirate, "coverage": "covered",
         "tests": [{"id": "test/VisitTest.java:8", "strength": "asserted", "why": "w"}]},
        {"id": book, "coverage": "covered",
         "tests": [{"id": "test/VisitTest.java:3", "strength": "asserted", "why": "w"}]}]}
    out, dropped = S.sanity(doc, texts, g["docs"])
    assert dropped == 1
    by = {e["id"]: e for e in out["sentences"]}
    assert by[pirate]["coverage"] == "missing" and by[pirate]["tests"] == []
    assert by[pirate]["downgrades"][0]["by"] == "script"
    assert by[book] == doc["sentences"][1], "a link on the sentence's own words stays"
    assert S.problems(out) == []


def test_a_quote_is_looked_up_in_the_body_whitespace_aside():
    body = ('    mockMvc.perform(get("/api/owners?" + query))\n'
            '            .andExpect(status().isBadRequest());\n')
    # Run 8's second read joined the chain on one line: the same code, accepted.
    assert S.quoted_in('mockMvc.perform(get("/api/owners?" + query)).andExpect(status()'
                       '.isBadRequest());', body)
    assert not S.quoted_in(".andExpect(status().isOk())", body)
    assert not S.quoted_in("", body) and not S.quoted_in(");", body)


def test_a_multi_line_annotation_is_part_of_the_body_the_model_reads():
    """Run 8's `invalidInput_isBadRequest` keeps its cases (`"size=7"`, …) in a four-line
    `@ValueSource`; the body the model and the script read must include them."""
    lines = ['    }', '', '    @ParameterizedTest', '    @ValueSource(strings = {',
             '            "size=7", "size=0",', '            "page=-1"})',
             '    void invalidInput_isBadRequest(String query) throws Exception {']
    assert S.annotations_start(lines, 7) == 3
    assert S.annotations_start(['  void x() {', '  }', '  @Test', '  void y() {'], 4) == 3


def test_the_second_read_is_a_setting_on_by_default(tmp_path, monkeypatch):
    RM = _load("rerun_model", "rerun-model.py")
    monkeypatch.delenv("HUMAN_REVIEW_MAPPING_CHECK", raising=False)
    cfg = tmp_path / "human-review.json"
    assert RM.mapping_check(None, cfg) is True
    cfg.write_text('{"mappingCheck": false}', encoding="utf-8")
    assert RM.mapping_check(None, cfg) is False
    monkeypatch.setenv("HUMAN_REVIEW_MAPPING_CHECK", "1")
    assert RM.mapping_check(None, cfg) is True
    assert RM.mapping_check(False, cfg) is False
    _run_model(tmp_path / "off", monkeypatch, argv=("--no-check",))
    assert not (tmp_path / "off" / "check-prompt.txt").exists()


# --- where the requirement text comes from ----------------------------------------------
#
# hr-try-4 had no PR and no `#N` anywhere, so the Tests tab was empty, although the branch
# carried the human's own request in `impl-conversation.md`. A GitHub issue still wins
# whenever one resolves; the page says which source it drew from.

CONVERSATION = """# The implementation conversation, requests 0-1

## Request 0 — the human:

I want to add pagination to the Owners grid.

Requirements:
- The page-size options must be 5, 10, and 20 rows.

### The agent:

Question 1 of 8: client-side or server-side?

## Request 1 — the human:

Server-side.
"""


def _no_issue_repo(tmp_path, monkeypatch, gh=None):
    """`_repo` with no `pr.ticket`, no cached issue, and GitHub answering only `gh`."""
    root, review = _repo(tmp_path)
    (review / "content.json").write_text(json.dumps(
        {"pr": {"repo": "https://github.com/acme/clinic", "branch": "hr-try-4"},
         "testChanges": "assets/test-changes.json"}), encoding="utf-8")
    (review / "ticket-body.json").unlink()
    asked = []

    def fake(slug, number):
        asked.append((slug, number))
        return (gh or {}).get(number)
    monkeypatch.setattr(S, "_gh_issue_full", fake)
    monkeypatch.setattr(S, "_avatar", lambda login: "")
    return root, review, asked


ISSUE_25 = {"title": "Page the owners", "url": "https://github.com/acme/clinic/issues/25",
            "author": {"login": "ana"}, "createdAt": "2026-09-01T10:00:00Z",
            "body": "1. Owners come in pages of 10.\n"}


def test_the_first_request_of_the_conversation_is_the_ticket_when_there_is_no_issue(
        tmp_path, monkeypatch):
    root, review, asked = _no_issue_repo(tmp_path, monkeypatch)
    (review / "impl-conversation.md").write_text(CONVERSATION, encoding="utf-8")
    t = S.fetch_ticket(S._spec(review), review, root)
    assert t["source"] == "conversation" and t["number"] is None
    assert "Owners grid" in t["body"] and "5, 10, and 20" in t["body"]
    assert "client-side" not in t["body"] and "Server-side" not in t["body"]
    assert "impl-conversation.md" in t["via"] and "not a GitHub issue" in t["via"]
    # `hr-try-4` ends in a counter, not an issue: nobody asked GitHub for #4.
    assert asked == []


def test_the_page_says_which_source_the_ticket_came_from(tmp_path, monkeypatch):
    root, review, _ = _no_issue_repo(tmp_path, monkeypatch)
    (review / "impl-conversation.md").write_text(CONVERSATION, encoding="utf-8")
    (review / S.MAPPING).write_text(json.dumps({"schema": "test-mapping/1", "sentences": []}),
                                    encoding="utf-8")
    assert S.write_fragment(S._spec(review), review, root)
    frag = (review / S.FRAGMENT).read_text()
    # Plain words, on a line of its own under the header strip — no file, no key.
    src = frag.split('<p class="rm-src">')[1].split("</p>")[0]
    assert src.startswith("From the first request of the conversation"), src
    assert ".md" not in src and ".json" not in src and "`" not in src
    head = frag.split('<div class="rm-tkhead">')[1].split("</div>")[0]
    assert "rm-src" not in head, "the provenance is squeezed into the header row again"
    T = importlib.import_module("hrbuild.tabs.tests")
    out = T.reqmap_layout(frag, S._spec(review), review, root)
    assert "Requirement: the implementation conversation&#x27;s first request" in out


def test_review_points_front_matter_names_the_issue(tmp_path, monkeypatch):
    root, review, asked = _no_issue_repo(tmp_path, monkeypatch, gh={25: ISSUE_25})
    (review / "impl-conversation.md").write_text(CONVERSATION, encoding="utf-8")
    (root / "review-points.md").write_text("---\nticket: #25\nbase: abc\n---\n\n## Fixed\n")
    t = S.fetch_ticket(S._spec(review), review, root)
    # The issue wins over the conversation, and says where it was named.
    assert (t["source"], t["number"], t["author"]) == ("github", 25, "ana")
    assert "review-points.md" in t["via"] and "#25" in t["via"]
    assert asked == [("acme/clinic", 25)]
    # Written down: the next build does not ask again.
    S.fetch_ticket(S._spec(review), review, root)
    assert asked == [("acme/clinic", 25)]


@pytest.mark.parametrize("value, slug", [
    ("#25", "acme/clinic"), ("25", "acme/clinic"),
    ("https://github.com/other/repo/issues/25", "other/repo"),
    ("other/repo#25", "other/repo")])
def test_a_ticket_value_names_an_issue_in_every_spelling(value, slug):
    assert S._ticket_value(value, "acme/clinic") == ((25, slug), "")


def test_a_ticket_value_that_is_text_is_the_requirement_text(tmp_path, monkeypatch):
    root, review, _ = _no_issue_repo(tmp_path, monkeypatch)
    (review / "impl-conversation.md").write_text(CONVERSATION, encoding="utf-8")
    (root / "review-points.md").write_text(
        "---\nticket: Owners come in pages of 5, 10 or 20.\n---\n")
    t = S.fetch_ticket(S._spec(review), review, root)
    assert t["source"] == "front-matter" and t["body"] == "Owners come in pages of 5, 10 or 20."


def test_the_branch_name_names_the_issue(tmp_path, monkeypatch):
    root, review, asked = _no_issue_repo(tmp_path, monkeypatch, gh={25: ISSUE_25})
    spec = S._spec(review)
    spec["pr"]["branch"] = "feature/25-page-owners"
    t = S.fetch_ticket(spec, review, root)
    assert t["number"] == 25 and "branch" in t["via"]


@pytest.mark.parametrize("branch, number", [
    ("25-page-owners", 25), ("feature/25-page-owners", 25), ("issue-25", 25),
    ("gh-25-paging", 25), ("hr-try-4", None), ("main", None), ("release/2026.10", None)])
def test_only_a_branch_that_carries_an_issue_names_one(branch, number):
    assert S.branch_issue(branch) == number


def test_an_openspec_change_matching_the_branch_beats_the_conversation(tmp_path, monkeypatch):
    root, review, _ = _no_issue_repo(tmp_path, monkeypatch)
    (review / "impl-conversation.md").write_text(CONVERSATION, encoding="utf-8")
    spec = S._spec(review)
    spec["pr"]["branch"] = "feature/page-owners"
    d = root / "openspec" / "changes" / "page-owners" / "specs" / "owners"
    d.mkdir(parents=True)
    (d / "spec.md").write_text("## ADDED Requirements\n\n### Requirement: Paging\n"
                               "The owners list SHALL come in pages.\n")
    other = root / "openspec" / "changes" / "vet-visits" / "specs" / "visits"
    other.mkdir(parents=True)
    (other / "spec.md").write_text("Visits SHALL have a vet.\n")
    t = S.fetch_ticket(spec, review, root)
    assert t["source"] == "openspec" and "pages" in t["body"] and "vet" not in t["body"]
    assert "page-owners" in t["via"]
    # A change for some other branch is not this branch's text.
    spec["pr"]["branch"] = "hr-try-4"
    assert S.fetch_ticket(spec, review, root)["source"] == "conversation"


def test_an_issue_github_cannot_answer_falls_back_to_the_conversation(tmp_path, monkeypatch):
    root, review, asked = _no_issue_repo(tmp_path, monkeypatch, gh={})
    (review / "impl-conversation.md").write_text(CONVERSATION, encoding="utf-8")
    (root / "review-points.md").write_text("---\nticket: #25\n---\n")
    t = S.fetch_ticket(S._spec(review), review, root)
    assert asked == [("acme/clinic", 25)] and t["source"] == "conversation"


def test_with_no_source_at_all_there_is_no_ticket(tmp_path, monkeypatch):
    root, review, _ = _no_issue_repo(tmp_path, monkeypatch)
    assert S.fetch_ticket(S._spec(review), review, root) is None


# --- agreement with the paid run on the demo PR ------------------------------------------

DEMO = Path.home() / "workspace" / "petclinic-pr"
DEMO_REF = DEMO / ".human-review" / ".model-prev" / "requirements-map.html"
DEMO_REF_LIVE = DEMO / ".human-review" / "assets" / "requirements-map.html"
TICKET_37 = (
    "The Visit should be linked to the vet that attended that consultation. Visit should "
    "display its vet everywhere throughout the app.\n\n---\n\n## What it has to do\n\n"
    "1. **Booking a visit lets you choose the vet, and lets you not choose one.** The vet is "
    "optional: half the time the appointment is booked before anyone knows who is taking it, "
    "and forcing a choice there produces bad data rather than information.\n"
    "2. **Editing a visit can change the vet, and can remove it.** Clearing the field must "
    "persist as empty. We had this with pet types: the old value kept coming back.\n"
    "3. **Wherever a visit is shown with its details, the vet is shown too.** Today that "
    "means the owner's page and the all-visits screen.\n"
    "4. **A visit with no vet reads as having none.** Not \"Unknown\", not "
    "blank-because-broken, and never an error. Visits created before this change have no vet "
    "and will not get one.\n\n## Out of scope\n\nSearching or filtering visits by vet.\n")


def _reference() -> Path | None:
    for p in (DEMO_REF_LIVE, DEMO_REF):
        if p.is_file() and S.GENERATED not in p.read_text(encoding="utf-8")[:400]:
            return p
    return None


@pytest.mark.skipif(_reference() is None
                    or not (DEMO / ".human-review" / "assets" / "test-coverage.json").is_file(),
                    reason="the demo PR's checkout and its model-written matrix are not here")
def test_the_scripted_pairing_agrees_with_the_paid_run_on_the_demo_pr(tmp_path):
    """Measured against the pairing a paid model run made on ticket #37 — one model's
    judgement, not a gold standard, so the floor is what the script reached when it was
    written, not a target. Read-only on the checkout: the review inputs are copied here."""
    review = tmp_path / ".human-review"
    (review / "assets").mkdir(parents=True)
    src = DEMO / ".human-review"
    shutil.copy(src / "content.json", review / "content.json")
    for f in ("test-coverage.json", "test-changes.json"):
        shutil.copy(src / "assets" / f, review / "assets" / f)
    (review / "ticket-body.json").write_text(json.dumps(
        {"number": 37, "title": "Link Visit with Vet", "url": "", "author": "victorrentea",
         "avatar": "", "createdAt": "2026-06-13T09:31:55Z", "body": TICKET_37}),
        encoding="utf-8")
    g = S.gather(S._spec(review), review, DEMO)
    entries = S.merge(g["sentences"], g["scripted"], None)
    a = S.agreement(g, entries, _reference().read_text(encoding="utf-8"))
    print(json.dumps(a))
    assert a["precision"] >= 0.35, a
    assert a["recall"] >= 0.35, a
    # Most of the ticket is decided without a model.
    assert len(g["scripted"]["decided"]) > len(g["scripted"]["open"])
