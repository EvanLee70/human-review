"""The Sequence tab: a diagram paired with the test that draws it."""
from __future__ import annotations

import functools
import html
import json
import os
import re
import subprocess
from pathlib import Path

from ..shared.diagrams import _context_svg, _source_link, render_diagrams, select_rows
from ..shared.genseq import (GENSEQ_HANDLE, genseq_by_test, genseq_details, genseq_file,
                             pair_anchor, test_of_genseq)
from ..shared.snippets import SNIPPET_BASE, snippet_html
from ..shared.svg import inline_svg

#: The three kinds of test the page names, and what each one is — the Tests tab's own
#: vocabulary, word for word, because a reader who learnt `UI` over there must not have to
#: learn it again here. The words that follow each chip are that tab's legend line; they
#: ride on the hover rather than being printed again under the tab title, since a second
#: copy of a legend is a second place for the two to disagree.
TEST_CATS = {
    "e2e":  ("UI", "clicks the screen"),
    "api":  ("API", "REST/MCP"),
    "unit": ("unit", "one isolated component"),
}

#: What actually ran the test, keyed by the extension of the file the pair quotes.
#:
#: The kind above says which END the run was driven from; it does not say what a reader
#: has to open, or in what language, to change the thing. Two rows on this tab were both
#: `UI` — a Playwright spec and a Cucumber scenario — and nothing on the shut row told
#: them apart, though one of them is the only test on the page written in a language a
#: non-programmer reads.
#:
#: Read off the extension and nothing else, and that is not the same shortcut `_pair_cat`
#: refuses. The kind is a claim about what the run DID, which a path cannot answer. The
#: runner IS the file: `.feature` is Gherkin because Gherkin is what a `.feature` file
#: contains, and no diagram is needed to know it. Longest suffix first, so `.spec.ts` is
#: not read as a bare `.ts`. The Playwright spec says `TypeScript`, the language, to sit
#: beside `Gherkin` and `JUnit` as the thing a reader would have to read, not the tool
#: that runs it; the tool stays in the hover.
TEST_RUNNERS = (
    (".feature", "Gherkin", "a Cucumber scenario"),
    (".spec.ts", "TypeScript", "a Playwright spec, in TypeScript"),
    (".spec.tsx", "TypeScript", "a Playwright spec, in TypeScript"),
    (".spec.js", "TypeScript", "a Playwright spec, in TypeScript"),
    (".java", "JUnit", "a JUnit test"),
    (".kt", "JUnit", "a JUnit test"),
)

#: A lifeline declaration in a generated sequence: `participant Browser`,
#: `actor "A vet" as Vet`, `participant UI as "Pet Clinic UI"`. Only the declarations are
#: read — an arrow can name a lifeline that was never declared, and the ORDER of the
#: declarations is the part that matters here.
SEQ_DECL = re.compile(
    r'^(?:participant|actor|database|queue|collections|boundary|control|entity)\s+'
    r'(?P<first>"[^"]*"|\S+)(?:\s+as\s+(?P<second>"[^"]*"|\S+))?\s*$')

#: The first arrow, for a diagram that declares no lifelines at all — PlantUML lets a
#: sender spring into existence on its first message, and a generator that leans on that
#: still has a driver; it is just never announced.
SEQ_ARROW = re.compile(r'^(?P<from>"[^"]*"|[^\s"<>-]+)\s*-+>+\s*'
                       r'(?P<to>"[^"]*"|[^\s"<>:-]+)\s*:')

#: What a driver lifeline is called when the run went through the screens. The generators
#: name their own driver — Playwright's is `Browser`, a `@SpringBootTest`'s is `Client` —
#: and this page already writes that convention down in `c2-from-sequence.py`. Anything
#: not on this list is a synthetic client calling the contract directly, which is `api`.
SEQ_UI_DRIVERS = re.compile(r"\b(browser|ui|frontend|front-end|chrome|playwright|page)\b",
                            re.I)


def _pair_cat(puml_rel: str, root: Path, authored: str | None = None) -> str | None:
    """Which kind of test drew this sequence: `e2e`, `api` or `unit`.

    Derived from the diagram, not from the file name, and for the same reason the pairing
    itself is derived: a `.spec.ts` is a Playwright run in one module and a component test
    in the next, and the page must not settle that by guessing at a path. The diagram is a
    record of what the run actually did, and its first lifeline is the end the test was
    driving from — `Browser` for a run through the screens, `Client` for a `@SpringBootTest`
    calling the contract in the same JVM. That is the very distinction the tab's own tip
    draws, so reading it off the picture keeps the badge and the tip from ever disagreeing.

    One lifeline is `unit`: a test that made no call anyone else could observe is a test of
    one isolated component, which is exactly what the Tests tab means by the word.

    A diagram that declares nothing is read off its first arrow instead — PlantUML lets a
    sender exist from its first message, and such a diagram has a driver too; it just never
    announced one. A diagram with neither gets no chip rather than a guessed one: an empty
    space says "not classified", and a wrong pill says something false in the page's own
    confident voice.

    `authored` wins when it is given. The classification above is a reading of a generated
    file, and a suite that names its driver something this has never heard of should be
    able to say so in the content file rather than wait for this list to grow.
    """
    if authored in TEST_CATS:
        return authored
    try:
        text = genseq_file(puml_rel, root).read_text(encoding="utf-8")
    except OSError:
        return None
    lifelines = []
    for raw in text.splitlines():
        m = SEQ_DECL.match(raw.strip())
        if not m:
            continue
        first, second = m["first"].strip('"'), (m["second"] or "").strip('"')
        # `participant "Pet Clinic UI" as UI` and `participant UI as "Pet Clinic UI"` both
        # exist; the quoted side is the label whichever order it came in, and with neither
        # quoted PlantUML's own rule applies — the alias is the second.
        label = first if not second or m["first"].startswith('"') else second
        if label not in lifelines:
            lifelines.append(label)
    if not lifelines:
        for raw in text.splitlines():
            m = SEQ_ARROW.match(raw.strip())
            if m:
                lifelines = [m["from"].strip('"'), m["to"].strip('"')]
                break
    if not lifelines:
        return None
    if len(lifelines) == 1:
        return "unit"
    return "e2e" if SEQ_UI_DRIVERS.search(lifelines[0]) else "api"


def _pair_runner(test_rel: str) -> tuple[str, str] | None:
    """`…/book-visit.feature` -> `("Gherkin", "a Cucumber scenario, in Gherkin")`."""
    low = test_rel.lower()
    for suffix, label, what in sorted(TEST_RUNNERS, key=lambda r: -len(r[0])):
        if low.endswith(suffix):
            return label, what
    return None


def _cat_chip(cat: str | None, test_rel: str = "") -> str:
    """The kind of test, and what wrote it: one pill reading `UI · Gherkin`.

    The kind alone comes off the Tests tab — same words, same palette, same pill. Copied
    rather than shared, like `FILE_PAGE` above it: the Tests tab is an included asset that
    builds its own markup, and the two will not be made to import from each other. What is
    shared is the decision, which is written down in `TEST_CATS`.

    The runner is this tab's own, and it is here because the kind is not enough to place a
    row: `UI` covers both a Playwright spec and a Cucumber feature, and a reader looking
    for the Gherkin scenario had to open every `UI` row to find which one it was. It rides
    INSIDE the same pill rather than beside it in a second one — a second chip is a second
    thing to learn and a second column to line up, for a word that only ever qualifies the
    first. A file whose extension says nothing gets the kind alone, exactly as before."""
    if cat not in TEST_CATS:
        return ""
    label, what = TEST_CATS[cat]
    runner = _pair_runner(test_rel)
    face = f"{label} \u00b7 {runner[0]}" if runner else label
    tip = what + (f", {runner[1]}" if runner else "")
    return (f'<span class="testcat" data-cat="{cat}"'
            + (f' data-runner="{html.escape(runner[0].lower(), quote=True)}"' if runner else "")
            + f' data-tip="{html.escape(tip, quote=True)}">{html.escape(face)}</span>')


#: What `run-steps.py` `_sequence` writes about the tests it traced beyond the tagged ones —
#: the tests this branch wrote or edited, picked from the test manifest (`steps.sequence.
#: select`), and the ones it left out and why.
SEQ_SELECTION = "assets/sequence.selection.json"

#: Why a picture exists, said on its row: `kind` -> (chip face, hover). Eval run 8: the page
#: showed three pictures and never said they were there because somebody had once tagged
#: those tests, nor that the branch's own paging and sorting scenarios carried no tag —
#: which was the whole reason they had none.
SEQ_WHY = {
    "tagged": ("tagged", "Has the tracing tag"),
    "added": ("new test", "No tracing tag; traced because it is new"),
    "modified": ("edited test", "No tracing tag; traced because it was edited"),
}


def sequence_selection(out_dir: Path) -> dict | None:
    try:
        doc = json.loads((Path(out_dir) / SEQ_SELECTION).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def picked_for(test_rel: str, scenarios, selection: dict | None) -> dict | None:
    """The selection entry this pair's picture was traced for, if it was one.

    By line, or by name: a JVM picture is titled with JUnit's display name
    (`defaultRequest_returnsFirstTen…()`), which begins with the slug of the method the
    manifest names."""
    for t in (selection or {}).get("picked") or []:
        if not isinstance(t, dict) or t.get("path") != test_rel:
            continue
        name = _slug(t.get("name", ""))
        for line, title in scenarios:
            if (t.get("line") and line == t["line"]) or (name and _slug(title).startswith(name)):
                return t
    return None


#: What the ledger says the branch did to a TAGGED test, said after `tagged` on its chip.
#: Eval run 10: 'Opening the owners page…' was written by this branch and also tagged, and
#: its row said only `tagged` while the Tests tab listed it as new — two tabs, two labels.
SEQ_ALSO = {"added": ("new test", "This branch wrote this test."),
            "modified": ("edited test", "This branch edited this test.")}


#: A tagged test the branch neither wrote nor edited, whose file or a module it imports
#: directly the branch did change. Eval run 11: 'Add a visit to an existing pet…' had its
#: add-visit.dsl.ts edited and its picture moved +20/−20, and its row said only `tagged`.
SEQ_TOUCHED = ("touched", "This branch changed: ")


def _why_chip(kind: str | None, also: str | None = None, via: tuple[str, ...] = ()) -> str:
    if kind not in SEQ_WHY:
        return ""
    face, tip = SEQ_WHY[kind]
    extra = SEQ_ALSO.get(also) if kind == "tagged" else None
    if kind == "tagged" and also == "touched" and via:
        extra = (SEQ_TOUCHED[0], SEQ_TOUCHED[1] + ", ".join(Path(v).name for v in via))
    status = ""
    if extra:
        face, tip = f"{face} \u00b7 {extra[0]}", f"{tip}. {extra[1]}"
        status = f' data-status="{also}"'
    return (f'<span class="seqwhy" data-why="{kind}"{status}'
            f' data-tip="{html.escape(tip, quote=True)}">{html.escape(face)}</span>')


def ledger_status(test_rel: str, scenarios, tests) -> str | None:
    """`added` / `modified` when the ledger (`test-changes.json`) says the branch wrote or
    edited one of the scenarios this picture draws — matched by line, or by name the way
    `picked_for` matches a JUnit display name."""
    found = None
    for t in tests or []:
        if not isinstance(t, dict) or t.get("path") != test_rel:
            continue
        if t.get("status") not in SEQ_ALSO:
            continue
        name = _slug(t.get("name", ""))
        for line, title in scenarios:
            if (t.get("line") and line == t["line"]) or (name and _slug(title).startswith(name)):
                if t["status"] == "added":
                    return "added"
                found = "modified"
    return found


@functools.lru_cache(maxsize=None)
def _branch_changed(root: str, base: str) -> frozenset[str]:
    """Every path the branch changed since its merge-base with `base`, work tree included —
    the same comparison `_moved_since_base` makes. Empty when git cannot say."""
    def git(*args):
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)
    mb = git("merge-base", base, "HEAD").stdout.strip()
    if not mb:
        return frozenset()
    r = git("diff", "--name-only", "--no-renames", mb)
    return frozenset(r.stdout.split()) if r.returncode == 0 else frozenset()


_TS_IMPORT = re.compile(r"""(?:\bfrom\s+|\bimport\s+|\brequire\()['"](\.{1,2}/[^'"]+)['"]""")
_JAVA_IMPORT = re.compile(r"^\s*import\s+(static\s+)?([\w.]+)\s*;", re.M)


def _direct_imports(test_rel: str, root: Path) -> list[str]:
    """The repo files `test_rel` imports itself — one level, never what those import.

    TypeScript/JavaScript: the relative module specifiers, resolved the way Node does.
    Java: each imported class looked up under the test module's `src/test/java` and
    `src/main/java`. A Gherkin file imports nothing."""
    try:
        text = (root / test_rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    found: list[str] = []
    if test_rel.endswith(".java"):
        module = test_rel.split("/src/", 1)[0] + "/src" if "/src/" in test_rel else "src"
        for static, name in _JAVA_IMPORT.findall(text):
            parts = name.split(".")
            if static or parts[-1] == "*":
                parts = parts[:-1]
            for tree in ("test/java", "main/java"):
                rel = f"{module}/{tree}/{'/'.join(parts)}.java"
                if (root / rel).is_file():
                    found.append(rel)
                    break
        return found
    if not re.search(r"\.(?:[cm]?[jt]sx?)$", test_rel):
        return found
    here = Path(test_rel).parent
    for spec in _TS_IMPORT.findall(text):
        base = Path(os.path.normpath(here / spec)).as_posix()
        for cand in (base, *(base + ext for ext in (".ts", ".tsx", ".js", ".mjs")),
                     base + "/index.ts", base + "/index.js"):
            if (root / cand).is_file():
                found.append(cand)
                break
    return found


def touched_via(test_rel: str, root: Path) -> tuple[str, ...]:
    """What the branch changed among the test's own file and the files it imports directly."""
    changed = _branch_changed(str(root), SNIPPET_BASE)
    if not changed:
        return ()
    hits = [test_rel] if test_rel in changed else []
    hits += [f for f in dict.fromkeys(_direct_imports(test_rel, root)) if f in changed]
    return tuple(hits)


def _names(tests, limit: int = 4) -> str:
    said = [f"<i>{html.escape(str(t.get('name', '')))}</i> "
            f"({html.escape(Path(str(t.get('path', ''))).name)})" for t in tests[:limit]]
    more = len(tests) - limit
    return ", ".join(said) + (f" and {more} more" if more > 0 else "")


#: Up to this many names are said inline; a longer list folds under its own count.
SEL_INLINE = 2


def _sel_item(label: str, items: list[str]) -> str:
    """One fact of the selection line: `label: a, b` when short, a closed fold when not.

    Eval run 10 printed this as one italic run-on paragraph — 'Not traced, over the cap of
    6: requestedPage_returns… and 22 more. 28 more of the branch's tests…' — a wall of
    method names standing between the reader and the first picture."""
    if len(items) <= SEL_INLINE:
        return (f'<span class="seqsel-k">{label}'
                + (": " + ", ".join(items) if items else "") + "</span>")
    return (f'<details class="seqsel-k"><summary>{label}</summary><ul>'
            + "".join(f"<li>{x}</li>" for x in items) + "</ul></details>")


def _sel_name(t: dict) -> str:
    return (f"<code>{html.escape(str(t.get('name', '')))}</code> "
            f"<span class=\"seqsel-f\">{html.escape(Path(str(t.get('path', ''))).name)}</span>")


def selection_note_html(selection: dict | None, drew: set[int]) -> str:
    """One compact line at the top of the tab: which of the branch's own tests were traced,
    which drew nothing, and which were left out — over the cap, or in files no traced suite
    runs. Each fact is a count; a list longer than `SEL_INLINE` folds under it.

    `drew` holds the indexes into `picked` that a pair on this tab matched."""
    if not selection:
        return ""
    picked = [t for t in selection.get("picked") or [] if isinstance(t, dict)]
    left = [t for t in selection.get("left") or [] if isinstance(t, dict)]
    capped = [t for t in left if t.get("suite")]
    nosuite = [t for t in left if not t.get("suite")]
    if not picked and not left:
        return ""
    items = []
    if picked:
        # Why they carry no tag, and which rows they are, is the lead's hover: said inline
        # it pushed the counts onto a second line.
        items.append(f'<span class="seqsel-k"><b>Also traced: {len(picked)} '
                     f"test{'s' if len(picked) != 1 else ''} this branch wrote or edited</b>"
                     "</span>")
        missed = [t for i, t in enumerate(picked) if i not in drew]
        if missed:
            items.append(_sel_item(f"{len(missed)} of them came back with no picture",
                                   [_sel_name(t) for t in missed]))
    if capped:
        items.append(_sel_item(
            f"{len(capped)} not traced, over the cap of {int(selection.get('max') or 0)}",
            [_sel_name(t) for t in capped]))
    if nosuite:
        per_file: dict[str, int] = {}
        for t in nosuite:
            f = Path(str(t.get("path", ""))).name
            per_file[f] = per_file.get(f, 0) + 1
        items.append(_sel_item(
            f"{len(nosuite)} in files no traced suite runs",
            [f'<span class="seqsel-f">{html.escape(f)}</span> \u00d7{n}'
             for f, n in per_file.items()]))
    return '<div class="seqsel">' + "".join(items) + "</div>"


def _scenarios_drawn(puml_rel: str, test_rel: str, root: Path) -> list[tuple[int, str]]:
    """Which scenarios this diagram actually drew, as (line, title).

    Read from the committed `.puml` rather than by looking for `@generate_sequence` in the
    test: the tag is a request, the chapter is the record that the request was granted and
    that there is a picture on this page to link to. A scenario the generator skipped has
    no chapter and gets no 🕵️. One per file since the generator started drawing a picture
    per scenario — the list survives because a diagram this page was built before that
    still has several, and reading it is how this page keeps working on both.

    Only the test's own handles count, and only on a `title` or `==` line. The same
    `src://` scheme is on every class and endpoint the diagram names, and a line number
    from `OwnerRepository.java` resolved against a feature file would point at nothing."""
    try:
        text = genseq_file(puml_rel, root).read_text(encoding="utf-8")
    except OSError:
        return []
    found = {}
    for line in text.splitlines():
        m = GENSEQ_HANDLE.match(line.strip())
        if m and m["path"] == test_rel:
            found.setdefault(int(m["line"]), (m["title"] or "").strip())
    return sorted(found.items())


#: The Tests tab's file glyph, and the corner mark that says what the branch did to that
#: file. Copied here from `requirements-map.html` deliberately: one vocabulary for "what
#: happened to this file" across the page, drawn the same way in both places, so a reader
#: who has learnt it on one tab is not taught it again on another. A page with a `+` is a
#: file that did not exist, a page with a pencil is one this branch edited, a bare page is
#: one it left alone — and the words those glyphs replace are on the hover, never dropped.
FILE_PAGE = ('<path class="fm-page" d="M9.5 1.1l3.4 3.5.1.4v2h-1V6H8V2H3v11h4v1H2.5l-.5-.5'
             'v-12l.5-.5h6.7l.3.1zM9 2v3h2.9L9 2z"/>')
FILE_PLUS = '<path class="fm-mark" d="M13 16h-1v-3H9v-1h3V9h1v3h3v1h-3v3z"/>'
FILE_PENCIL = ('<path class="fm-mark" d="M8.65 13.65 13.65 8.65 15.55 10.55 10.55 15.55Z'
               'M8.65 13.65 10.55 15.55 7.9 16.3Z"/>'
               '<path class="fm-mark" d="M12.5 9.8 14.4 11.7 13.75 12.35 11.85 10.45Z"/>')

#: `<span class="code-badge" data-diff="new" data-tip="…">new file</span>` — the words
#: `srcbar_html` prints at the end of a source bar.
CODE_BADGE = re.compile(
    r'<span class="code-badge"[^>]*?(?: data-tip="(?P<tip>[^"]*)")?>(?P<label>[^<]*)</span>')


def _badge_as_glyph(bar: str) -> str:
    """The bar's own `new file` / `2 lines changed` badge, drawn instead of spelled.

    `NEW FILE` in caps beside a file name is read before the name is — a label louder than
    its subject, on a row whose subject is the file. The Tests tab settled this one level
    down already: the same page glyph, marked `+` or pencil in its corner, with the words
    it replaces moved into the hover. This is that decision applied to the row the Sequence
    tab puts above a quoted test, and it is the same drawing, not a lookalike.

    Keyed off the badge's words rather than its `data-diff`, exactly as the Tests tab keys
    it: `new file` and `new code` are both `new` to git and are not the same fact — one is
    a file that did not exist, the other is fresh lines inside one that did.
    """
    def swap(m):
        label = m["label"]
        kind = ("new" if label.startswith("new file")
                else "unchanged" if label.startswith("unchanged") else "edited")
        mark = {"new": FILE_PLUS, "edited": FILE_PENCIL, "unchanged": ""}[kind]
        tip = m["tip"] or ""
        return (f'<span class="filemark" data-kind="{kind}" role="img"'
                f' aria-label="{html.escape(label, quote=True)}"'
                f' data-tip="{label[:1].upper()}{label[1:]}{" &mdash; " + tip if tip else ""}">'
                f'<svg viewBox="0 0 16 16" aria-hidden="true">{FILE_PAGE}{mark}</svg></span>')

    return CODE_BADGE.sub(swap, bar, count=1)


#: The header `extract-snippet.py` puts at the top of every quoted block: the two handles,
#: the file name with the lines it quotes, and what changed in it. Matched rather than
#: rebuilt, because only that module knows what the bar says — the window may have snapped
#: past a leading comment, and the badge is computed against the review's own base. It has
#: no nested `<div>`, so the first `</div>` is its own; anything else would need a parser.
SRCBAR = re.compile(r'<div class="srcbar">.*?</div>', re.S)


def _fold_over(quoted: list[str]) -> tuple[str, list[str]]:
    """Move the first quoted block's source bar out of the block and onto the fold's row.

    The row above a quoted test used to read `the test · lines 60–61,70–94,124–155`, and
    the bar immediately below it read `AddVisitApiTest.java:60-61,70-94,124-155`. The same
    line numbers twice, the second time beside the file they belong to — so the first copy
    was saying nothing the second did not say better, and it cost a row on a tab whose
    whole shape is one row per thing.

    What is left of that row is the only thing it ever said that the bar does not: whether
    the test is open. So the control and the bar become one line — `Show Test`, then the
    file, its lines, and what changed in it — and the block underneath keeps the code
    alone. Only the first block's bar moves: a second excerpt of the same file is a
    different window and still has to name itself.
    """
    if not quoted:
        return "", []
    m = SRCBAR.search(quoted[0])
    if not m:
        return "", list(quoted)
    return (_badge_as_glyph(m.group(0)),
            [quoted[0][: m.start()] + quoted[0][m.end():], *quoted[1:]])


def _folded_pair(puml_rel: str, test_rel: str, pieces: list[str],
                 quoted: list[str] = (),
                 scenarios: list[tuple[int, str]] = (),
                 cat: str | None = None, why: str | None = None,
                 also: str | None = None, via: tuple[str, ...] = ()) -> str:
    """The test and the sequence its run recorded, foldable together — with the quoted
    test folded closed inside it, and the whole pair folded closed too.

    Closed, because a sequence is tall. One of them is three or four screens of arrows, and
    a tab that opens on four of those opens on a wall: the reader scrolls past pictures
    they did not ask for to find out what is even on the tab. Closed, the tab opens on its
    own table of contents — one line per test — and the reader picks. The folding is done
    by `SEQFOLD_JS` rather than by leaving out this `open`, because the click targets
    inside every diagram are measured with `getBBox()` while the page loads, and
    `getBBox()` inside a closed `<details>` returns zeros; see that script.

    Both halves fold, which is the older correction: the fold used to close over the quoted
    test alone and leave the diagram standing underneath, orphaned. A sequence is a drawing
    of one test — without the test above it, it is a picture of nothing.

    The summary names the *scenarios*, not the file — `AddVisitApiTest: remembers the vet
    who attended it`, which is what the generator wrote into the diagram's own title and
    what the Tests tab calls it. The path is not lost: it is the summary's tooltip, and the
    fold's row under it names the file, the lines it quotes and what changed in them. A
    pair whose generator recorded no chapter falls back to the basename, all there is.

    It leads with the kind of test this is, in the Tests tab's own chip. Shut, this tab is
    a list of sentences, and "which of these went through a browser?" was a question
    the reader could only answer by opening each one and looking at the top lifeline —
    which is where the chip reads it from anyway. Leading, not trailing: the chips line up
    into a column the eye can run down, and a kind that arrives after the sentence arrives
    after it was needed.
    """
    titles = [t for _, t in scenarios if t]
    name = (" · ".join(html.escape(t) for t in titles) if titles
            else html.escape(test_rel.rsplit("/", 1)[-1]))
    src = ""
    bar, blocks = _fold_over(list(quoted))
    if blocks:
        src = ('<details class="testsrc">'
               f'<summary><span class="foldlbl"></span>{bar}</summary>'
               + "\n".join(x.strip("\n") for x in blocks)
               + "</details>\n")
    return (f'<details class="testpair" open id="{pair_anchor(puml_rel)}"'
            f' data-test="{html.escape(test_rel)}">'
            f'<summary data-tip="{html.escape(test_rel)}">'
            f'{_cat_chip(cat, test_rel)}{_why_chip(why, also, via)}{name}</summary>'
            + src
            + "\n".join(x.strip("\n") for x in pieces)
            + "</details>")


def _line_spans(ref_tail: str) -> list[tuple[int, int]]:
    """`35-48,52-65` → [(35, 48), (52, 65)]; `12` → [(12, 12)].

    One snippet reference can carry several ranges — that is how a test is quoted without
    the forty lines of setup between its two halves. Anything unparseable yields nothing,
    which is the honest answer: a reference this cannot read is a reference that cannot be
    said to cover any scenario."""
    spans = []
    for part in ref_tail.split(","):
        lo, _, hi = part.strip().partition("-")
        try:
            spans.append((int(lo), int(hi or lo)))
        except ValueError:
            continue
    return spans


def _share_excerpts(test_rel: str, entries, snippets, used: set, root: Path):
    """Hand each of one test file's pictures the excerpts that belong to it.

    One diagram per test file needed no sharing: every excerpt quoting the file went under
    the one picture. Per scenario, four pictures of `owner-search.feature` would each have
    repeated the same thirty lines, or — the way it worked out before this — the first
    would have taken all of them and the rest would have shown none.

    The split is derived, not authored. An excerpt is a set of line ranges in the content
    file, and a range that contains a scenario's declaration line is an excerpt *of* that
    scenario; the generator already told us which line each picture starts at. What matches
    nothing — a Background, a set of imports, a helper below the last scenario — goes under
    the first picture, where a reader meets it before the scenarios that use it.

    One excerpt often quotes two scenarios at once (`35-48,52-65` is one snippet in the
    content file, not two), and then it goes under *both* — but narrowed, each pair
    getting only its own scenario's lines. Whole, both pairs quoted `34-64` and both
    opened on the test declared at 34, so the vet's diagram sat beside the other
    scenario's code; and picking one owner instead leaves the other pair claiming its test
    is "not excerpted here", which is false. `_spans_for` does the cutting.

    The excerpts come back as they went in, unrendered: which excerpt belongs to which
    picture is the decision this function exists to make, and it is one a test can check
    by reading refs rather than by searching rendered HTML for line numbers.
    """
    mine = [x for x in snippets if x["ref"].rpartition(":")[0] == test_rel]
    used.update(id(x) for x in mine)
    quoted = {rel: [] for rel, _ in entries}
    lines_of = {rel: [ln for ln, _ in _scenarios_drawn(rel, test_rel, root)]
                for rel, _ in entries}
    extents = _scenario_extents(lines_of)
    first = entries[0][0]
    for x in mine:
        spans = _line_spans(x["ref"].rpartition(":")[2])
        owners = [rel for rel, _ in entries
                  if any(lo <= ln <= hi for lo, hi in spans for ln in lines_of[rel])]
        if len(owners) > 1:
            for owner in owners:
                quoted[owner].append(_narrowed(x, test_rel,
                                               _spans_for(spans, owner, lines_of, extents)))
            continue
        for owner in (owners or [first]):
            quoted[owner].append(x)
    return quoted


def _scenario_extents(lines_of: dict[str, list[int]]) -> dict[str, list[tuple[int, int]]]:
    """Where each picture's scenario stops, in the test file: the line before the next
    scenario starts — whosever it is.

    A test file gives away where a scenario BEGINS and never where it ends: the generator
    records the declaration line, and nothing records the closing brace. The next
    declaration is the only end the page can know, and it is the right one — everything
    between two scenarios is either the first one's body or the second one's lede, and
    which of the two it is, is settled per span by `_spans_for` rather than guessed here.
    The last scenario runs to the end of the file, which is what a helper below it is
    part of as far as a reader scrolling the quote is concerned. The first one runs back
    to line 1 for the same reason and the one `_share_excerpts` already states: whatever
    stands above the first scenario belongs to nobody in particular and is met under the
    first picture."""
    marks = sorted((ln, rel) for rel, lines in lines_of.items() for ln in lines)
    extents: dict[str, list[tuple[int, int]]] = {rel: [] for rel in lines_of}
    for i, (line, rel) in enumerate(marks):
        end = marks[i + 1][0] - 1 if i + 1 < len(marks) else 10 ** 9
        extents[rel].append((1 if i == 0 else line, end))
    return extents


def _spans_for(spans, owner: str, lines_of, extents) -> list[tuple[int, int]]:
    """The part of a shared excerpt that is this picture's own test.

    An excerpt that quotes two tagged tests of one file used to go to both pairs whole, so
    both quoted `34-64` and both opened on the test declared at 34 — the vet's diagram
    sitting beside the other scenario's code, which is the one mistake this tab exists to
    prevent. The excerpt is still shared (the alternative is a pair claiming its test is
    "not excerpted here", which is false), but each side is handed only the lines that are
    its own.

    Per RANGE first, because the content file already draws the line: `34-47,49-64` is two
    ranges around two tests, and the comment on 49-50 that introduces the second one
    travels with it. Only a range that swallows several declarations is cut, and then at
    the next declaration — see `_scenario_extents`. A range holding no declaration at all
    (imports, a Background, a helper) goes to every picture: it is setup both scenarios
    run through, and dropping it from all but one would quote a test without the fixture
    it stands on."""
    mine: list[tuple[int, int]] = []
    for lo, hi in spans:
        inside = [rel for rel in lines_of
                  if any(lo <= ln <= hi for ln in lines_of[rel])]
        if not inside:
            mine.append((lo, hi))
        elif inside == [owner]:
            mine.append((lo, hi))
        elif owner in inside:
            mine += [(max(lo, a), min(hi, b)) for a, b in extents[owner]
                     if max(lo, a) <= min(hi, b)]
    return sorted(mine)


def _narrowed(snippet: dict, test_rel: str, spans) -> dict:
    """The same excerpt, quoting only the lines `_spans_for` left to this pair.

    A copy, never an edit: the snippet dicts belong to the content file and the same one
    is handed to both pairs. An unchanged set of ranges returns the original object, so a
    file with one picture per excerpt goes through this function byte for byte."""
    if not spans:
        return snippet
    tail = ",".join(str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in spans)
    if tail == snippet["ref"].rpartition(":")[2]:
        return snippet
    return {**snippet, "ref": f"{test_rel}:{tail}"}


def _unquoted_note(test_rel: str, root: Path) -> str:
    """What to say beside a diagram no snippet quotes.

    Silence would read as "this diagram has no test", which is never true — the manifest
    only knows about a diagram *because* a test generated it. The two things that are
    true are said instead, and they are different: nobody chose an excerpt, or the file
    the generator recorded is not in this checkout. The second one is the reader's cue
    that the pairing is real but the source is not here to read."""
    if (root / test_rel).is_file():
        return (f'<p class="testlead"><span>Generated by '
                f'<a class="srcref" href="vscode://file/{(root / test_rel).resolve()}:1:1" '
                f'data-tip="Open in VS Code">{html.escape(test_rel)}</a>, '
                f'not excerpted here.</span></p>')
    return (f'<p class="testlead"><span>Generated by <code>{html.escape(test_rel)}</code>, '
            f'which is not in this checkout — the diagram is the only record of it '
            f'left.</span></p>')


# ── the tests the tab quotes, derived ───────────────────────────────────────────────
#
# Which test to quote beside which picture used to be the content file's `snippets`, typed
# by the model that wrote the review. Eval run 5 (3 Oct 2026) copied them from the reference
# PR: `add-visit.spec.ts:31-43`, `AddVisitApiTest.java:73-98` — line ranges of another branch,
# quoting tests by position. Nothing about that choice needs a model. A picture names the
# scenario that drew it (`title [[src://<test>:<line> …]]`), and a test asks for a picture with
# a tag. So the build reads both: every scenario a diagram names, and every scenario tagged
# for tracing that no diagram names, each quoted from its tag to its last line.

#: A test asking for a sequence: the Gherkin tag, the Java annotation, the Playwright tag
#: (`{tag: [GENERATE_SEQUENCE_TAG]}` or the literal). Read on a line, then placed on the
#: scenario it belongs to — a line that belongs to no scenario (the constant's own
#: declaration, an import, a Javadoc) is dropped there, not here.
GENSEQ_TAG = re.compile(r"^\s*@(?:generate_sequence|GenerateSequence)\b"
                        r"|GENERATE_SEQUENCE_TAG|[\"']@generate_sequence[\"']")
_FEATURE_DECL = re.compile(r"^\s*(?:Scenario(?: Outline| Template)?|Example)\s*:")
_FEATURE_STOP = re.compile(r"^\s*(?:@|Scenario|Example\s*:|Rule\s*:|Background\s*:|Feature\s*:)")
_TS_DECL = re.compile(r"^\s*(?:test|it)(?:\.\w+)?\s*\(")
_JAVA_DECL = re.compile(r"^\s*(?:(?:public|protected|private|static|final|abstract|"
                        r"synchronized)\s+)*[\w<>\[\],.? ]+\s+\w+\s*\(")
_STRINGS = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`')


def _test_kind(rel: str) -> str:
    low = rel.lower()
    return ("feature" if low.endswith(".feature")
            else "java" if low.endswith((".java", ".kt"))
            else "ts" if low.endswith((".ts", ".tsx", ".js", ".mjs")) else "")


def scenario_span(lines: list[str], decl: int, kind: str) -> tuple[int, int]:
    """1-based `(first, last)` of the scenario declared on line `decl`, from its tag.

    From its tag because the tag is why the picture exists: a quote opening below
    `@generate_sequence` or `@GenerateSequence` quotes a test that looks untagged. A Gherkin
    scenario ends before the next scenario, tag or rule; a Java method or a Playwright
    `test(…)` where its brackets close — counted over parentheses and braces together, so
    a `{tag: […]}` options object on the second line does not end the test early."""
    i = decl - 1
    lo = i
    while lo > 0 and lines[lo - 1].strip().startswith("@"):
        lo -= 1
    if kind == "feature":
        hi = i
        for j in range(i + 1, len(lines)):
            if _FEATURE_STOP.match(lines[j]):
                break
            if lines[j].strip() and not lines[j].strip().startswith("#"):
                hi = j
        return lo + 1, hi + 1
    depth, opened = 0, False
    for j in range(i, min(len(lines), i + 400)):
        code = _STRINGS.sub('""', lines[j]).split("//", 1)[0]
        for ch in code:
            if ch in "({":
                depth += 1
                opened = opened or ch == "{"
            elif ch in ")}":
                depth -= 1
                if opened and depth <= 0:
                    return lo + 1, j + 1
    return lo + 1, min(len(lines), i + 60)


def _tagged_decl(lines: list[str], at: int, kind: str) -> int | None:
    """The declaration line (1-based) of the scenario the tag on line `at` (0-based) is on."""
    if kind == "feature":
        for j in range(at + 1, min(len(lines), at + 6)):
            if _FEATURE_DECL.match(lines[j]):
                return j + 1
            if lines[j].strip() and not lines[j].strip().startswith(("@", "#")):
                return None
        return None
    if kind == "java":
        for j in range(at + 1, min(len(lines), at + 12)):
            t = lines[j].strip()
            if not t or t.startswith(("@", "//", "/*", "*")):
                continue
            if re.search(r"\b(?:class|interface|record|enum)\b", t):
                return None            # on the class: every test in it, which is no scenario
            return j + 1 if _JAVA_DECL.match(lines[j]) else None
        return None
    if kind == "ts":
        for j in range(at, max(-1, at - 5), -1):
            if _TS_DECL.match(lines[j]):
                return j + 1
    return None


def tagged_scenarios(root: Path) -> dict[str, list[int]]:
    """`{test: [declaration line, …]}` of every scenario tagged for tracing in the index."""
    found: dict[str, list[int]] = {}
    got = subprocess.run(["git", "-C", str(root), "grep", "-n", "-I", "-E",
                          "@generate_sequence|@GenerateSequence|GENERATE_SEQUENCE_TAG"],
                         capture_output=True, text=True)
    hits: dict[str, list[int]] = {}
    for row in got.stdout.splitlines() if got.returncode == 0 else []:
        rel, _, rest = row.partition(":")
        num, _, text = rest.partition(":")
        if num.isdigit() and _test_kind(rel) and GENSEQ_TAG.search(text) \
                and "/genseq/" not in f"/{rel}":
            hits.setdefault(rel, []).append(int(num) - 1)
    for rel, ats in hits.items():
        try:
            lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        kind = _test_kind(rel)
        decls = {d for d in (_tagged_decl(lines, at, kind) for at in ats) if d}
        if decls:
            found[rel] = sorted(decls)
    return found


def _ref(rel: str, decls, root: Path) -> dict | None:
    try:
        lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    spans: list[list[int]] = []
    for lo, hi in sorted(scenario_span(lines, d, _test_kind(rel)) for d in decls
                         if 0 < d <= len(lines)):
        if spans and lo <= spans[-1][1] + 1:
            spans[-1][1] = max(spans[-1][1], hi)
        else:
            spans.append([lo, hi])
    if not spans:
        return None
    return {"ref": f"{rel}:" + ",".join(f"{a}-{b}" if a != b else str(a) for a, b in spans),
            "derived": True}


def derived_snippets(root: Path) -> tuple[list[dict], list[dict]]:
    """`(drawn, undrawn)` excerpts, one per test file each.

    `drawn` quotes every scenario a diagram on disk names, in the file the diagram says drew
    it; `undrawn` every scenario tagged for tracing that no diagram names — the group the
    tab heads "Tagged for tracing, and no diagram came back". One reference per file, of as
    many spans as it has scenarios, which is the shape `_share_excerpts` hands out."""
    drawn: dict[str, set[int]] = {}
    for test_rel, pumls in genseq_by_test(root).items():
        if not (root / test_rel).is_file():
            continue
        for puml in pumls:
            drawn.setdefault(test_rel, set()).update(
                ln for ln, _ in _scenarios_drawn(puml, test_rel, root))
    undrawn = {rel: [d for d in decls if d not in drawn.get(rel, set())]
               for rel, decls in tagged_scenarios(root).items()}
    pool = [x for x in (_ref(rel, sorted(lines), root)
                        for rel, lines in sorted(drawn.items()) if lines) if x]
    rest = [x for x in (_ref(rel, lines, root)
                        for rel, lines in sorted(undrawn.items()) if lines) if x]
    return pool, rest


#: What the layout puts in a Sequence block's `snippets`: derive them (`derived_snippets`).
AUTO_SNIPPETS = {"auto": "genseq"}


#: What `plan` carries in place of a manifest row for a diagram that has no delta drawn
#: for it and is *not* identical to the base. `None` already means "unchanged", and the
#: two must not be told apart by a boolean beside the plan — the plan is what the render
#: loop reads, so the distinction belongs in it.
STALE = object()


@functools.lru_cache(maxsize=None)
def _moved_since_base(rel: str, root: str, base: str) -> bool:
    """Whether the work tree's copy of `rel` is *not* what the review's base ref has.

    `render_testpairs` learns that a diagram changed by finding a row for it in
    `MANIFEST.tsv`, and the manifest is written by a producer — `puml-diff.sh`, run by the
    `diagrams` and `sequence` steps. Absence from it therefore has two meanings that look
    identical from here: the branch really left the diagram alone, or the manifest is
    older than the diagram. The second one happens in the ordinary way of working: the
    page is rebuilt without re-running the producers (`refresh-report.py` runs none by
    default, because most refreshes are a change to the *page*), and any `.genseq.puml`
    the acceptance suite regenerated in the meantime is then described by a manifest that
    predates it. The pair came out pilled UNCHANGED over a picture that differs from the
    base in every lifeline — a page asserting the opposite of what `git diff` says, with
    nothing on it admitting the claim was second-hand.

    So the claim is checked against the repository instead of inferred from an artifact.
    Against the merge-base with the base ref, not its tip, for the same reason
    `puml-diff.sh` uses it: commits that landed on the base after this branch started are
    not this branch's doing.

    `False` is also the answer when git cannot say — no repository, no such base ref, a
    checkout with no commits — and that is deliberate: the fixture-shaped cases are
    exactly the ones where "unchanged" was never a claim about a base to begin with, and
    turning them into a warning would be inventing a problem.
    """
    def git(*args):
        return subprocess.run(["git", "-C", root, *args],
                              capture_output=True, text=True)
    mb = git("merge-base", base, "HEAD").stdout.strip()
    if not mb:
        return False
    # This run's copy, when the Sequence step drew one: the work tree holds the committed
    # bytes again, and it is the re-traced picture the tab is showing.
    traced = genseq_file(rel, Path(root))
    if traced != Path(root) / rel:
        was = git("show", f"{mb}:{rel}")
        return was.returncode != 0 or was.stdout != traced.read_text(encoding="utf-8")
    # `--quiet` exits 1 on a difference, 0 on none — and 128 when git could not look,
    # which must not be read as "it moved".
    r = git("diff", "--quiet", mb, "--", rel)
    return r.returncode == 1


def _stale_sequence(puml_rel: str, test_rel: str, root: Path, out_dir: Path) -> str:
    """The card for a sequence that has no delta on disk and is not identical to the base.

    It cannot be drawn as a delta — nothing rendered one — and it must not be pilled
    UNCHANGED, because the repository says otherwise. What is true is that the evidence is
    out of date, and that the reader can fix it with the button in the masthead, so that
    is what it says, over the picture the work tree actually holds."""
    rel = puml_rel
    cache, why_not = _context_svg(rel, root, out_dir)
    body = f'<div class="svgbox">{inline_svg(cache, root)}</div>' if cache else why_not
    return (f'<div class="diagram dgm-bare"'
            f' data-test-src="vscode://file/{(root / test_rel).resolve()}:1:1">'
            '<div class="head"><span class="badge sev-med" data-tip="No delta was drawn '
            'for this diagram, but it differs from the base — the diagram manifest is '
            'older than the picture">delta not drawn</span>'
            + _source_link(rel, root) + '</div>'
            '<p class="sub">This sequence differs from the base ref, but no delta was '
            'drawn for it: <code>assets/diagrams/MANIFEST.tsv</code> is older than the '
            'diagram. Press <b>Rerun</b> — or run <code>run-steps.py --only diagrams</code>'
            ' — to redraw it.</p>'
            + genseq_details(rel, root)
            + body + '</div>')


def _unchanged_sequence(puml_rel: str, test_rel: str, root: Path, out_dir: Path) -> str:
    """The card for a sequence the branch left alone, inside its test's pair.

    The same bare card `render_diagrams` draws for a delta — no title, no provenance line,
    the test's href on the card for the scenario links — with two differences that are
    the whole point: the pill says UNCHANGED in the neutral colour a `puml` context block
    wears, and there is no Diff/New/Old bar, because there is nothing to diff against.
    The picture is drawn from the committed `.puml` by the `puml` block's own renderer,
    and the generator's sidecar rides along so the handles in it still expand."""
    rel = puml_rel
    cache, why_not = _context_svg(rel, root, out_dir)
    body = f'<div class="svgbox">{inline_svg(cache, root)}</div>' if cache else why_not
    return (f'<div class="diagram dgm-bare"'
            f' data-test-src="vscode://file/{(root / test_rel).resolve()}:1:1">'
            '<div class="head"><span class="badge sev-info">unchanged</span>'
            + _source_link(rel, root) + '</div>'
            + genseq_details(rel, root)
            + body + '</div>')


def render_testpairs(block, dspec, manifest_rows, root: Path, out_dir: Path,
                     test_changes: list | None = None):
    """Each acceptance test next to the sequence its own run recorded.

    They used to be two lists on the same tab — a gallery of diagrams, then a list of test
    snippets — and the reader had to work out which picture belonged to which test from the
    file names. Nothing was hidden and nothing was reliable. The pairing is not a judgement
    call, either: the manifest says which test file each diagram came from, and the
    diagram's own chapter titles say which scenarios inside that file, at which lines. So it
    is derived, not authored.

    Neither side is ever dropped, and neither is ever given a partner it did not produce.
    The manifest lists only the diagrams this branch *changed*, so a quoted test with no
    row in it has two different stories, and they are told apart on disk: its
    `<test>.genseq.puml` is either there, identical to the base — a sequence the branch
    left alone, paired and marked as such, counted as no delta — or it is not there at
    all, and the test goes to a trailing group that says exactly that. The second is the
    more interesting absence, because a tagged test with no recorded trace is a fact about
    the *evidence* rather than a gap in the page; the first used to be filed under it, and
    every such page had to be corrected by hand. A diagram whose test is not quoted, or
    not in this checkout at all, says so under its own heading rather than sitting there
    looking like it came from nowhere."""
    # A diagram the branch DELETED has no picture to pair with a test, and a frame
    # titled by the file it used to be drawn from is the worst of both: it reads as a
    # test, on a tab whose frames ARE the list of tests. It happens whenever a test
    # loses its `@generate_sequence` / `@GenerateSequence` — a fact about the source,
    # visible where the source is quoted rather than as an empty exhibit here.
    rows = [r for r in select_rows(manifest_rows, block)
            if r["kind"] == "sequence" and r.get("status") != "deleted"]
    # Derived when the layout says so (every content file's Sequence block, since run 5);
    # a list is still read as it stands, for a block built by hand or by a test.
    undrawn: list[dict] = []
    if isinstance(block.get("snippets"), dict) and block["snippets"].get("auto") == "genseq":
        snippets, undrawn = derived_snippets(root)
    else:
        snippets = list(block.get("snippets", []))
    parts, used = [], set()
    # The registry the 🕵️ on the covering-tests rows reads: one entry per scenario the
    # generator drew, keyed the way that map addresses a row, so the jump is a lookup and
    # not a guess. Filled as the pairs are rendered — a pair that is not on this tab must
    # not be linkable from the other one.
    index = []

    def register(puml_rel, scenarios):
        for line, title in scenarios:
            index.append({"test": f"{test_of_genseq(puml_rel, root)}:{line}",
                          "pair": pair_anchor(puml_rel), "title": title})

    # Every diagram on this tab, in page order, grouped by the test it was drawn from —
    # because the excerpts are quoted per test file and have to be shared out among that
    # file's pictures. A changed one is a manifest row; an unchanged one is a `.puml` on
    # disk that no row mentions, found below.
    plan: dict[str, list[tuple[str, object]]] = {}

    def plan_add(puml_rel, what):
        plan.setdefault(test_of_genseq(puml_rel, root), []).append((puml_rel, what))

    merged = dict(dspec)
    merged.pop("only", None)
    for r in rows:
        plan_add(r["source"], r)

    unchanged = 0
    stale = 0
    drawn = genseq_by_test(root)
    for test_rel in dict.fromkeys(x["ref"].rpartition(":")[0] for x in snippets):
        for rel in drawn.get(test_rel, ()):
            if any(rel == q for q, _ in plan.get(test_rel, [])):
                continue          # this branch changed it: it is already a row above
            # No row, so the manifest says nothing changed here — but the manifest is an
            # artifact of a producer that may not have run since this file did. Ask git
            # before repeating it: a diagram that differs from the base is never
            # "unchanged", whatever an older manifest was told. See `_moved_since_base`.
            moved = _moved_since_base(rel, str(root), SNIPPET_BASE)
            plan_add(rel, STALE if moved else None)
            stale += 1 if moved else 0
            unchanged += 0 if moved else 1

    # An author's say on what kind of test a file holds, keyed by the file the snippet
    # quotes — one kind per test file, which is the grain the content file already writes
    # its references at. Absent, `_pair_cat` reads it off the diagram.
    authored_cat = {x["ref"].rpartition(":")[0]: x.get("cat")
                    for x in snippets if x.get("cat")}
    lost_by_rel = {x["diagram"]: x for x in _lost(sequence_verdict(out_dir))}
    selection = sequence_selection(out_dir)
    picked = (selection or {}).get("picked") or []
    drew: set[int] = set()
    tagged = None

    for test_rel, entries in plan.items():
        quoted_by_pair = _share_excerpts(test_rel, entries, snippets, used, root)
        for puml_rel, row in entries:
            scenarios = _scenarios_drawn(puml_rel, test_rel, root)
            quoted = [snippet_html(x["ref"], x.get("caption"), root)
                      for x in quoted_by_pair[puml_rel]]
            # No lead. The scenario names used to be printed here as deep links, and every
            # one of them was said again a few hundred pixels lower: the diagram's own
            # section headers are those same titles, linked to those same lines, drawn by
            # the generator. Two copies of one list, and the one on the picture is the one
            # that sits where the reader is already looking.
            pieces = [] if quoted else [_unquoted_note(test_rel, root)]
            if puml_rel in lost_by_rel:
                pieces.append(lost_note_html(lost_by_rel[puml_rel]))
            pieces.append(
                _stale_sequence(puml_rel, test_rel, root, out_dir) if row is STALE
                else render_diagrams(merged, root, out_dir, [row], bare=test_rel)
                if row is not None
                else _unchanged_sequence(puml_rel, test_rel, root, out_dir))
            hit = picked_for(test_rel, scenarios, selection)
            if hit is not None:
                drew.add(picked.index(hit))
                why = hit.get("status")
            else:
                if tagged is None:
                    tagged = tagged_scenarios(root)
                why = ("tagged" if any(ln in tagged.get(test_rel, ()) for ln, _ in scenarios)
                       else None)
            also = ledger_status(test_rel, scenarios, test_changes) if why == "tagged" else None
            via: tuple[str, ...] = ()
            if why == "tagged" and also is None:
                via = touched_via(test_rel, root)
                also = "touched" if via else None
            parts.append(_folded_pair(puml_rel, test_rel, pieces, quoted, scenarios,
                                      _pair_cat(puml_rel, root,
                                                authored_cat.get(test_rel)), why, also, via))
            register(puml_rel, scenarios)

    orphaned = [x for x in snippets if id(x) not in used] + undrawn
    tail = block.get("unpaired") or {}
    if orphaned:
        pieces = [""] + [snippet_html(x["ref"], x.get("caption"), root).strip("\n")
                         for x in orphaned]
        parts.append(
            f'<h3 id="{html.escape(tail.get("id", "tests-nosequence"))}">'
            f'{html.escape(tail.get("title", "Tests that record no sequence"))}</h3>'
            + (f'<p>{tail["body"]}</p>' if tail.get("body") else "")
        )
        parts.append('<div class="testpair">' + "\n".join(pieces) + "</div>")

    band = sequence_verdict_html(out_dir)
    if not parts:
        # A tab with nothing on it but the reason it is empty is still worth its pill: an
        # absent Sequence tab says nothing, and this one says what to start.
        return (band, 1, 1) if band else ("", 0, 0)
    # `"title": ""` means no heading at all, and is worth having: every pair below already
    # names its own scenarios and carries its own source path, so a heading over them can
    # only restate what the tab label said — and it does it above the fold, where the
    # first picture should be. An absent title still gets the default; only an author who
    # typed an empty one is asking for the space back. The default names what the tab is —
    # the label says *Sequence*, and the heading says of what: the tests' own runs.
    title = block.get("title", "Sequence diagrams of tests")
    head = ((f'<h3 id="{html.escape(block.get("id", "sequences"))}">'
             f'{html.escape(title)}</h3>') if title else "")
    head += f'<p>{block["body"]}</p>' if block.get("body") else ""
    head += selection_note_html(selection, drew)
    # Invisible, and last: nothing to look at, only the map's way back in. `</` cannot
    # appear inside a script element, whatever its type.
    if index:
        parts.append('<script type="application/json" id="hr-genseq">'
                     + json.dumps(index).replace("</", "<\\/") + "</script>")
    # Weight counts every exhibit; changes count only the manifest's rows. An unchanged
    # pair is context, exactly as a `puml` block is, and must not un-strike the tab —
    # unless the suites were not re-traced: a strike says "this branch left the sequences
    # alone", and a run that drew nothing cannot know that.
    return (band + "\n".join(([head] if head else []) + parts) + "\n",
            len(rows) + unchanged + stale + len(orphaned),
            len(rows) or int(sequence_verdict_alarm(out_dir) is not None))


#: What `run-steps.py` `_sequence` writes beside the diagrams whenever the traced suites did
#: not simply pass — read here the way the Demo tab reads `feature.verdict.json`.
SEQ_VERDICT = "assets/sequence.verdict.json"

#: `state` -> (band class, headline, what it means for the pictures under it). The classes
#: are the Review tab's bands (`.rband`, `review.css`): the same kind of statement — read
#: everything below differently — in the same shape, rather than a second vocabulary.
SEQ_VERDICT_FACE = {
    "skipped": ("rband-warn", "Not re-traced on this run.",
                "The traced suites drew no diagram, so every sequence on this tab is the "
                "one committed on the branch — not evidence of this run, and possibly "
                "stale against the code under review."),
    "red": ("rband-alert", "The traced suite was red.",
            "The diagrams below come from that run: a scenario that failed is drawn only "
            "as far as it got."),
    "notests": ("rband-none", "Part of the traced run found nothing to run.",
                "A tag filter matched no test — not a failure, and the diagrams below are "
                "this run's."),
    # Eval run 6: the re-traced AddVisitApiTest lost NotificationService, the SMS gateway
    # and both calls — the backend's in-process test could not reach the traced stack's
    # notification-service — and the band above it said "the diagrams below are this
    # run's", with nothing admitting they contradicted the committed ones.
    "degraded": ("rband-warn", "This run's trace lost what the committed diagrams show.",
                 "The diagrams below are this run's, and the ones named here no longer show "
                 "participants or calls their committed version has."),
}

#: `state` -> the words on the tab pill's accessible name when the band is an alarm.
SEQ_VERDICT_ALARM = {"skipped": "not re-traced on this run", "red": "traced suite red",
                     "degraded": "trace lost calls the committed diagrams show"}


def sequence_verdict(out_dir: Path) -> dict | None:
    try:
        doc = json.loads((Path(out_dir) / SEQ_VERDICT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("state") in SEQ_VERDICT_FACE else None


def sequence_verdict_alarm(out_dir: Path) -> str | None:
    """The pill's label when the tab must be read as not this run's evidence, else None.
    A run that LOST calls is one, whatever else it was: a tag filter that matched nothing
    beside it does not make the pictures it degraded trustworthy."""
    doc = sequence_verdict(out_dir) or {}
    return SEQ_VERDICT_ALARM.get(doc.get("state")) or (
        SEQ_VERDICT_ALARM["degraded"] if _lost(doc) else None)


def _lost(doc: dict | None) -> list[dict]:
    """`run-steps.py` `lost_vs_committed`: per re-traced diagram, what its committed copy
    shows and it no longer does."""
    return [x for x in (doc or {}).get("lost") or []
            if isinstance(x, dict) and isinstance(x.get("diagram"), str)
            and (x.get("participants") or x.get("calls"))]


def lost_note_html(entry: dict) -> str:
    """The warning inside one pair whose re-traced diagram lost what the committed one has.

    Neutral about the cause on purpose — the trace cannot tell a call the code stopped
    making from one the traced stack never reached — except when a whole participant is
    gone, which is far more often the stack (a service not started, or not reachable from
    the JVM that ran the test) than a branch deleting a system from its own picture."""
    parts = [str(x) for x in entry.get("participants") or []]
    calls = [str(x) for x in entry.get("calls") or []]
    said = []
    if parts:
        said.append("participant" + ("s " if len(parts) != 1 else " ")
                    + ", ".join(f"<b>{html.escape(x)}</b>" for x in parts))
    if calls:
        said.append(f"{len(calls)} call{'s' if len(calls) != 1 else ''}: "
                    + "; ".join(f"<code>{html.escape(x)}</code>" for x in calls))
    cause = ("A whole participant missing is usually the traced stack — a service that was "
             "not started, or not reachable from the JVM that ran the test — rather than the "
             "code." if parts else
             "Either the code under review stopped making these calls, or the traced stack "
             "did not reach them; the trace alone cannot tell which.")
    return ('<p class="rband rband-warn seqlost" role="note"><b>Lost vs the committed '
            'diagram:</b> this run\'s trace no longer shows ' + " and ".join(said)
            + f". {html.escape(cause)}</p>")


#: `⚠️ "<scenario>": fetch failed — skipped` — a generator naming what it skipped. Read off
#: the log for a verdict written before `run-steps.py` recorded `skips` itself.
_SKIP_LINE = re.compile(r"(?:\"[^\"]*\"|'[^']*')\s*:\s*(?P<why>.*\bskipped\b.*)$", re.I)


def _drew_nothing(r: dict) -> str:
    """`exit 0, and drew no diagram — “fetch failed — skipped” ×3` for a command that
    exited 0 on a run that drew nothing — never `passed`."""
    if r.get("outcome") == "drew-nothing" and r.get("detail"):
        return str(r["detail"])
    skips = r.get("skips")
    if skips is None:
        skips = [m["why"].strip() for m in map(_SKIP_LINE.search, map(str, r.get("log") or []))
                 if m]
    counts: dict[str, int] = {}
    for why in skips:
        counts[why] = counts.get(why, 0) + 1
    said = ", ".join(f"“{w}”" + (f" ×{n}" if n > 1 else "") for w, n in counts.items())
    return "exit 0, and drew no diagram" + (f" — {said}" if said else "")


def _counted(runs: list[dict], state: str) -> str:
    """One plain sentence for the band: how the commands ended, counted, no shell in it."""
    nothing = state == "skipped"
    tally = {"failed": 0, "no-tests": 0, "empty": 0, "passed": 0}
    for r in runs:
        o = r.get("outcome")
        key = ("failed" if o == "failed" else "no-tests" if o == "no-tests"
               else "empty" if o == "drew-nothing" or (o == "ran" and nothing) else "passed")
        tally[key] += 1
    words = [(tally["failed"], "could not run"),
             (tally["no-tests"], "found no test under its tag filter"),
             (tally["empty"], "exited cleanly with nothing to draw"),
             (tally["passed"], "passed")]
    total = len(runs)
    # One command is "The traced command found no test…", never "…command 1 found".
    said = [what if total == 1 else f"{n} {what}" for n, what in words if n]
    if not said:
        return ""
    head = f"Of the {total} traced command{'s' if total != 1 else ''}, " if total > 1 else "The traced command "
    return head + ", ".join(said[:-1]) + (" and " if len(said) > 1 else "") + said[-1] + "."


def sequence_verdict_html(out_dir: Path) -> str:
    """Why the Sequence tab is empty, red or stale, said at its top — or nothing.

    The reason used to exist only as a row of the producers' status table, which reached
    the page if and when a model copied it into the guide, on the Review tab. The Sequence
    tab itself was merely struck through, which reads as "this branch did not touch its
    sequences" — while it was showing committed pictures nobody had re-traced.

    It leads with plain sentences — what the tab is, then how the commands ended, counted —
    and everything in shell (the commands, their exits, their last lines) goes in one fold
    under them. Eval run 5 printed `npm run trace:diagram: passed` in the open for a
    generator that had skipped every scenario and drawn nothing: a command that drew
    nothing is not `passed`, and the raw lines were the first thing a judge read."""
    doc = sequence_verdict(out_dir)
    if not doc:
        return ""
    state = doc["state"]
    cls, title, why = SEQ_VERDICT_FACE[state]
    lost = _lost(doc)
    if lost and state == "notests":
        # The loss leads: a tag filter that matched nothing is said below, as its cause.
        cls, title, why = SEQ_VERDICT_FACE["degraded"]
    parts = [f'<p><b>{html.escape(title)}</b> {html.escape(why)}</p>']
    if lost:
        gone = list(dict.fromkeys(p for x in lost for p in x.get("participants") or []))
        names = ", ".join(Path(x["diagram"]).name.split(".genseq.")[0] for x in lost)
        parts.append(
            '<p class="rb-sub"><b>Lost vs the committed diagrams</b> in ' + html.escape(names)
            + (": " + ", ".join(f"<b>{html.escape(g)}</b>" for g in gone) if gone else "")
            + " — flagged on each picture below. Do not read a missing call as the code "
              "no longer making it until the trace is re-run with the whole stack "
              "reachable.</p>")
    # The plain cause of each command that found nothing, said in the open: Maven prints a
    # tag filter that matched nothing as `[ERROR] … MojoFailureException`, and the fold
    # below would otherwise be a red dump under a reassuring headline (eval run 6).
    for r in doc.get("runs") or []:
        if isinstance(r, dict) and r.get("outcome") == "no-tests" and r.get("detail"):
            parts.append(f'<p class="rb-sub">The cause, in plain words: '
                         f'{html.escape(str(r["detail"]))}. The <code>[ERROR]</code> lines '
                         'in the fold below are that, not a broken test.</p>')
    missing = [m for m in doc.get("missing") or [] if isinstance(m, str)]
    if missing:
        parts.append("<p class=\"rb-sub\">Nothing answers at: "
                     + " · ".join(f"<code>{html.escape(m)}</code>" for m in missing)
                     + ". Start them, or configure <code>steps.sequence.app</code>, and "
                       "rerun this tab.</p>")
    runs = [r for r in doc.get("runs") or [] if isinstance(r, dict)]
    if runs:
        counted = _counted(runs, state)
        if counted:
            parts.append(f'<p class="rb-sub">{html.escape(counted)}</p>')

        def said(r):
            outcome = r.get("outcome")
            text = (_drew_nothing(r) if outcome == "drew-nothing"
                    or (outcome == "ran" and state == "skipped")
                    else "passed" if outcome == "ran"
                    else str(r.get("detail") or "") if outcome == "no-tests"
                    else f'exit {r.get("exit")} — {r.get("detail", "")}')
            return (f'<li><code>{html.escape(str(r.get("command", "")))}</code>: '
                    f'{html.escape(text)}</li>')
        log = [f'$ {r.get("command", "")}\n' + "\n".join(map(str, r.get("log") or []))
               for r in runs if (r.get("outcome") != "ran" or state == "skipped")
               and r.get("log")]
        # Folded, like the recorder's last words over the film: the answer for whoever
        # is fixing the environment, furniture for everybody else.
        parts.append('<details class="toolcommits"><summary>What each command said'
                     '</summary><ul>' + "".join(said(r) for r in runs) + "</ul>"
                     + ('<pre>' + html.escape("\n\n".join(log)) + "</pre>" if log else "")
                     + "</details>")
    elif doc.get("reason") and not missing:
        parts.append(f'<p class="rb-sub">{html.escape(str(doc["reason"]))}</p>')
    role = "alert" if state in SEQ_VERDICT_ALARM or lost else "status"
    cls = "rband-warn" if lost and cls == "rband-none" else cls
    return f'<div class="rband {cls} seqverdict" role="{role}">' + "".join(parts) + "</div>"
