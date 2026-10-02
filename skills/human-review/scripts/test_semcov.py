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
    asked = S.model_input(g["ticket"], g["sentences"], g["rows"], g["scripted"], g["docs"])
    assert [s["text"] for s in asked["sentences"]] == ["Owners are greeted by a pirate."]


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
