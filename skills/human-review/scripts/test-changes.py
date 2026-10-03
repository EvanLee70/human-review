#!/usr/bin/env python3
"""Which tests this change set wrote, which it edited, and which it stopped running.

The Tests tab hangs a list of tests under each requirement, and a reviewer reads a *new*
test very differently from a tweaked one: the first is evidence the requirement was
pinned, the second is evidence an existing pin was moved. That distinction is a fact
about the diff, not a judgement, so it is computed here rather than asserted by whoever
writes the content file. The model's only job is to say which requirement a test belongs
to; this script says what happened to it.

The question underneath all of it is whether the branch left fewer tests running than it
found, and that has three answers, not one: a test can be deleted, commented out, or
left in place under an `@Disabled`. All three cost the run the same test, and only the
first shows up as a removal in a diff -- the other two read as "still there" to anyone
skimming. So each side is scanned for what is *declared* and for what is *switched off*,
and the page states one reconciled pair of numbers: how many tests entered the run and
how many left it, by whatever route.

The unit is a **test case**, not a file. A file that shows up as `M` in `--name-status`
usually holds one new test and nine untouched ones, and reporting the whole file as
"modified" would bury exactly the row the reviewer came for. So each side of the diff is
parsed for its test declarations, and the two name sets decide:

    in the new tree only            -> added      (a test that did not exist before)
    in the base only                -> deleted
    in both, and the diff touched
    its body                        -> modified
    in both, untouched              -> unchanged

and carries, alongside that, whether it runs now and whether it ran before:

    @Disabled / it.skip / t.Skip() -> silenced: "disabled"
    declaration only inside a comment -> silenced: "commented" (reported as deleted,
                                       because the run has lost it either way)

Line numbers are always in *working-tree* coordinates, so every row can be opened in the
editor. A deleted test has no line of its own any more, so it carries the line where its
removal landed — the point in the surviving file where the reader can see the gap. A test
in a file that was deleted outright carries no line at all and says so (`gone`). Because
unrelated code sits at that landing line, the page does not link a deleted test there: it
carries `baseLine` (its declaration at the base) and, with a GitHub `origin`, `baseUrl` —
the blob at the base commit — and that is what the page opens.

A test renamed in place (same spot, same tags, mostly the same body, a new title) is one
`modified` row with `renamedFrom`, not one deleted and one added; see `pair_renames`.

Usage:
    test-changes.py --base origin/main [path ...] [--out assets/test-changes.json]

With no paths, the whole repository is scanned; anything that is not recognisably a test
file is skipped either way.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# what counts as a test file
# --------------------------------------------------------------------------- #
# By path, not by content: a file is a test because of where it sits and what it is
# called, which is the same rule the build systems in this repository already apply.
TEST_FILE = (
    re.compile(r"(?:^|/)src/test/"),
    re.compile(r"(?:^|/)tests?/"),
    re.compile(r"[A-Za-z0-9]Tests?\.java$"),
    re.compile(r"[A-Za-z0-9]IT\.java$"),
    re.compile(r"\.(?:spec|test)\.(?:ts|tsx|js|jsx)$"),
    re.compile(r"(?:^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.go$"),
    re.compile(r"\.feature$"),
)


def is_test_file(rel: str) -> bool:
    return any(p.search(rel) for p in TEST_FILE)


# --------------------------------------------------------------------------- #
# what counts as a test case
# --------------------------------------------------------------------------- #
# One declaration pattern per language. Each returns the *name* a human would use for
# the test, because that is what the content file names and what the page prints.
JAVA_METHOD = re.compile(r"^\s*(?:(?:public|private|protected|static|final|default)\s+)*"
                         r"(?:<[^>]+>\s*)?[\w.<>\[\], ?]+\s+(\w+)\s*\(")
JAVA_TEST_ANNOTATION = re.compile(r"^\s*@(?:Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)\b")
JAVA_TYPE = re.compile(r"^\s*(?:(?:public|protected|private|static|final|abstract|sealed)\s+)*"
                       r"(?:class|interface|record|enum)\s+\w+")
# `it(...)`, `test(...)`, and their modifiers -- `it.only`, `test.skip`, `xit`, and the
# table form `it.each([...])('name', ...)`, whose title sits in the *second* call.
JS_CASE = re.compile(r"""^\s*(?P<head>x?(?:it|test)(?:\.\w+)*)\s*(?:\([^;]*?\)\s*)?\(\s*(?P<q>['"`])(?P<name>.+?)(?P=q)""")
JS_SUITE = re.compile(r"""^(?P<indent>\s*)(?P<head>x?(?:describe|context|suite)(?:\.\w+)*)\s*\(""")
PY_CASE = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)\s*\(")
PY_CLASS = re.compile(r"^(\s*)class\s+\w+")
GO_CASE = re.compile(r"^func\s+((?:Test|Benchmark|Fuzz|Example)\w*)\s*\(")
GHERKIN_CASE = re.compile(r"^\s*(?:Scenario|Scenario Outline|Scenario Template|Example)\s*:\s*(?P<name>\S.*?)\s*$")

# --------------------------------------------------------------------------- #
# what counts as a silenced test
# --------------------------------------------------------------------------- #
# A test stops running for three reasons, and only one of them is deletion. The other two
# leave the code in the file -- an `@Disabled` on top of it, or a `//` in front of every
# line -- and both read as "still there" in a diff a human skims. The reviewer needs the
# same warning for all three, so silencing is read off the syntax exactly like the
# declarations above, and nobody has to remember to declare it.
JAVA_SKIP = re.compile(r"^\s*@(?:Disabled|Ignore)\b")
# Applied to the matched call head, not to the line: `xit`, `it.skip`, `test.todo`.
JS_SKIP = re.compile(r"^x|\.(?:skip|todo|failing)\b")
PY_SKIP = re.compile(r"^\s*@(?:\w+\.)*(?:skip|skipif|skipIf|skipUnless|xfail)\b")
# Go has no annotation for it: the test runs, and the first thing it does is bail out.
GO_SKIP = re.compile(r"\.Skip(?:Now|f)?\s*\(")
GHERKIN_TAGS = re.compile(r"^\s*@\S")
GHERKIN_SKIP = re.compile(r"(?i)@(?:ignore|skip|wip|disabled|manual)\b")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _java_scan(lines: list[str]) -> dict[str, tuple[int, str | None]]:
    """A JUnit method is a test because it carries a test annotation, not because it is
    `void`. Helper methods in the same class are `void` too, and counting them would put
    a `setUp` in a requirement's coverage list.

    The `@Disabled` that silences one may sit on the method or on the class around it, so
    a disabled type declaration hands its state down to everything indented under it --
    which is also what keeps a `@Disabled` on a `@Nested` class from silencing its
    siblings."""
    out: dict[str, tuple[int, str | None]] = {}
    annotated = skip = False
    off_from: int | None = None      # indent of the innermost type declaration turned off
    for i, line in enumerate(lines, start=1):
        if JAVA_TEST_ANNOTATION.match(line):
            annotated = True
            continue
        if JAVA_SKIP.match(line):
            skip = True
            continue
        if not line.strip() or line.lstrip().startswith(("//", "*", "/*", "@")):
            continue
        indent = _indent(line)
        if JAVA_TYPE.match(line):
            if off_from is not None and indent <= off_from:
                off_from = None      # that class closed before this one opened
            if skip and off_from is None:
                off_from = indent
            annotated = skip = False
            continue
        m = JAVA_METHOD.match(line)
        if m:
            if annotated:
                inside = off_from is not None and indent > off_from
                out.setdefault(m.group(1), (i, "disabled" if skip or inside else None))
            annotated = skip = False
        elif line.strip().endswith(("{", "}", ";")):
            # Anything else that closes a statement ends the annotation's reach.
            annotated = skip = False
    return out


def _js_scan(lines: list[str]) -> dict[str, tuple[int, str | None]]:
    """`xdescribe` / `describe.skip` silences every case nested in it, so a suite that is
    switched off is tracked by the indent it opened at, the same way Java's class is."""
    out: dict[str, tuple[int, str | None]] = {}
    off_from: int | None = None
    for i, line in enumerate(lines, start=1):
        s = JS_SUITE.match(line)
        if s:
            indent = len(s.group("indent"))
            if off_from is not None and indent <= off_from:
                off_from = None
            if JS_SKIP.search(s.group("head")) and off_from is None:
                off_from = indent
            continue
        m = JS_CASE.match(line)
        if m:
            inside = off_from is not None and _indent(line) > off_from
            out.setdefault(m.group("name"),
                           (i, "disabled" if JS_SKIP.search(m.group("head")) or inside else None))
    return out


def _py_scan(lines: list[str]) -> dict[str, tuple[int, str | None]]:
    out: dict[str, tuple[int, str | None]] = {}
    skip = False
    off_from: int | None = None
    for i, line in enumerate(lines, start=1):
        if PY_SKIP.match(line):
            skip = True
            continue
        if not line.strip():
            continue
        c = PY_CLASS.match(line)
        if c:
            indent = len(c.group(1))
            if off_from is not None and indent <= off_from:
                off_from = None
            if skip and off_from is None:
                off_from = indent
            skip = False
            continue
        m = PY_CASE.match(line)
        if m:
            inside = off_from is not None and _indent(line) > off_from
            out.setdefault(m.group(1), (i, "disabled" if skip or inside else None))
            skip = False
        elif not line.lstrip().startswith(("@", "#")):
            skip = False
    return out


def _go_scan(lines: list[str]) -> dict[str, tuple[int, str | None]]:
    """`t.Skip()` is a statement, not a marker, so the whole body has to be read. A test
    that skips itself conditionally is still reported as silenced: the reviewer is the
    one who should decide whether the condition holds on CI."""
    starts = [(i, m.group(1)) for i, line in enumerate(lines, start=1)
              if (m := GO_CASE.match(line))]
    out: dict[str, tuple[int, str | None]] = {}
    for n, (i, name) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines) + 1
        body = "\n".join(lines[i - 1:end - 1])
        out.setdefault(name, (i, "disabled" if GO_SKIP.search(body) else None))
    return out


def _gherkin_scan(lines: list[str]) -> dict[str, tuple[int, str | None]]:
    """A `@wip` on the Feature turns off every scenario under it; one on a Scenario turns
    off that scenario. Whether the runner is told to exclude the tag is a build-file
    question this script cannot see -- but a tag whose whole job is to exclude is worth
    the reviewer's attention either way."""
    out: dict[str, tuple[int, str | None]] = {}
    tagged = feature_off = False
    for i, line in enumerate(lines, start=1):
        if GHERKIN_TAGS.match(line):
            tagged = tagged or bool(GHERKIN_SKIP.search(line))
            continue
        if not line.strip():
            continue
        if re.match(r"^\s*Feature\s*:", line):
            feature_off, tagged = tagged, False
            continue
        m = GHERKIN_CASE.match(line)
        if m:
            out.setdefault(m.group("name"),
                           (i, "disabled" if tagged or feature_off else None))
        tagged = False
    return out


def scan_cases(rel: str, text: str) -> dict[str, tuple[int, str | None]]:
    """`{test name: (1-based declaration line, why it does not run | None)}`."""
    lines = text.splitlines()
    if rel.endswith(".java"):
        return _java_scan(lines)
    if rel.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs")):
        return _js_scan(lines)
    if rel.endswith(".py"):
        return _py_scan(lines)
    if rel.endswith(".go"):
        return _go_scan(lines)
    if rel.endswith(".feature"):
        return _gherkin_scan(lines)
    return {}


def test_cases(rel: str, text: str) -> dict[str, int]:
    """`{test name: 1-based declaration line}` for one file's source."""
    return {name: line for name, (line, _) in scan_cases(rel, text).items()}


# --------------------------------------------------------------------------- #
# a test that is still there, only commented out
# --------------------------------------------------------------------------- #
# Commenting a test out is deleting it with the body left behind as an alibi: the run
# loses it exactly as if the lines were gone, but the file still contains every word of
# it, so a reviewer skimming the diff reads "kept, just parked". The scanners above are
# therefore run a second time over the source with one layer of comment marker stripped,
# and a declaration that turns up there and nowhere in the live code is a test that was
# switched off in the quietest way there is.
UNCOMMENT_SLASH = re.compile(r"^(\s*)(?://+|\*(?!/))[ \t]?")
UNCOMMENT_HASH = re.compile(r"^(\s*)#+[ \t]?")


def _uncomment(rel: str, text: str) -> str:
    """The same source with its comment markers taken off, line for line -- so anything
    found in it keeps the line number it has on disk and stays clickable."""
    if rel.endswith((".py", ".feature")):
        pat = UNCOMMENT_HASH
    elif rel.endswith((".java", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go")):
        pat = UNCOMMENT_SLASH
    else:
        return text
    return "\n".join(pat.sub(r"\1", line, count=1) for line in text.splitlines())


def commented_cases(rel: str, text: str) -> dict[str, int]:
    """`{test name: line}` for the cases that exist in this file only inside a comment."""
    live = scan_cases(rel, text)
    return {n: ln for n, (ln, _) in scan_cases(rel, _uncomment(rel, text)).items()
            if n not in live}


# --------------------------------------------------------------------------- #
# the diff, at line granularity
# --------------------------------------------------------------------------- #
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def hunk_lines(diff: str) -> tuple[set[int], dict[int, int]]:
    """`(new-file lines this diff added, {old-file line removed: where it was removed})`.

    The second half is what lets a deleted test still be clickable: the removal has no
    line of its own in the working tree, but it has a *place* in it — the line the
    reader's caret should land on to see the gap.
    """
    added: set[int] = set()
    removed: dict[int, int] = {}
    old = new = 0
    for line in diff.splitlines():
        m = HUNK.match(line)
        if m:
            old, new = int(m.group(1)), int(m.group(3))
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.add(new)
            new += 1
        elif line.startswith("-"):
            removed[old] = new
            old += 1
        elif line.startswith(" "):
            old += 1
            new += 1
    return added, removed


def _row(name: str, rel: str, status: str, line: int | None,
         silenced: str | None, was_silenced: str | None) -> dict:
    """One row of the manifest. The two silence fields are written only when they say
    something -- an absent key means "it ran before and it runs now", which is true of
    almost every row and does not need repeating on all of them."""
    row = {"name": name, "path": rel, "status": status, "line": line}
    if silenced:
        row["silenced"] = silenced
    if was_silenced:
        row["wasSilenced"] = was_silenced
    return row


# --------------------------------------------------------------------------- #
# a test that was renamed, not replaced
# --------------------------------------------------------------------------- #
# Names are the key above, so a scenario retitled in place — `Searching with an empty last
# name lists every owner` → `… shows the first page of every owner`, same line, same tag,
# its last step rewritten — came out as one test gone and one test new. That is two lies
# on the chip: a test the run never lost, and a test nobody wrote. So the leftovers of
# the name match get a second, stricter look, and a pair that sits in the same place, wears
# the same tags and still says mostly the same thing is one test, EDITED.
#
# Conservative on purpose. A wrong pairing hides a real deletion behind a pencil, which is
# worse than the double count it replaces, so every condition has to hold: the same file
# (by construction), the base declaration landing within RENAME_DRIFT lines of the new one
# once the diff's own shifts are applied, the same tag/annotation lines above it, and
# either a similar name over a body that kept something, or a near-identical body of some
# substance under a new name. Two empty `void x() {}` bodies are similar to everything and
# prove nothing, so a body has to have RENAME_MIN_BODY real lines before it can carry a
# pairing on its own. One-to-one, best match first.
RENAME_DRIFT = 3
RENAME_NAME_SIM = 0.6
RENAME_BODY_WITH_NAME = 0.3
RENAME_BODY_ALONE = 0.8
RENAME_MIN_BODY = 3
RENAME_BODY_MAX = 40


def _old_to_new(old: int, added: set[int], removed: dict[int, int]) -> int:
    """Where base line `old` sits in the working tree: its removal's landing place if the
    diff took it out, otherwise shifted by every removal before it and every addition
    before where it lands."""
    if old in removed:
        return removed[old]
    new = old - sum(1 for r in removed if r < old)
    while True:
        moved = old - sum(1 for r in removed if r < old) + sum(1 for a in added if a <= new)
        if moved == new:
            return new
        new = moved


def _tags_above(lines: list[str], line: int) -> tuple[str, ...]:
    """The `@…` lines directly over a declaration: Gherkin tags, Java annotations, Python
    decorators. A test that changed them changed what it is, not just what it is called."""
    out, i = [], line - 2
    while i >= 0 and lines[i].strip().startswith("@"):
        out.append(" ".join(lines[i].split()))
        i -= 1
    return tuple(sorted(out))


def _body(lines: list[str], line: int, starts: list[int]) -> list[str]:
    """A declaration's body: the lines after it, up to the next declaration, with the
    punctuation-only lines (`}`, `});`, blank) left out — they are in every body alike."""
    end = next((s for s in starts if s > line), len(lines) + 1)
    end = min(end, line + 1 + RENAME_BODY_MAX)
    return [" ".join(x.split()) for x in lines[line:end - 1] if re.search(r"\w", x)
            and not x.lstrip().startswith("@")]


def _similar(a, b) -> float:
    import difflib
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def pair_renames(rel: str, before: str, after: str, rows: list[dict],
                 added: set[int], removed: dict[int, int]) -> list[dict]:
    """`rows` with every rename folded into one `modified` row that names its old title
    (`renamedFrom`) — the added half keeps its line, the deleted half is dropped."""
    gone = [r for r in rows if r["status"] == "deleted" and not r.get("silenced")]
    new = [r for r in rows if r["status"] == "added"]
    if not gone or not new:
        return rows
    b_lines, a_lines = before.splitlines(), after.splitlines()
    b_cases, a_cases = scan_cases(rel, before), scan_cases(rel, after)
    b_starts = sorted(ln for ln, _ in b_cases.values())
    a_starts = sorted(ln for ln, _ in a_cases.values())
    scored = []
    for g in gone:
        old_line = b_cases[g["name"]][0]
        at = _old_to_new(old_line, added, removed)
        b_tags, b_body = _tags_above(b_lines, old_line), _body(b_lines, old_line, b_starts)
        for n in new:
            if abs(n["line"] - at) > RENAME_DRIFT:
                continue
            if _tags_above(a_lines, n["line"]) != b_tags:
                continue
            a_body = _body(a_lines, n["line"], a_starts)
            name = _similar(g["name"].lower(), n["name"].lower())
            body = _similar(b_body, a_body) if b_body and a_body else 0.0
            ok = ((name >= RENAME_NAME_SIM and body >= RENAME_BODY_WITH_NAME)
                  or (body >= RENAME_BODY_ALONE
                      and min(len(b_body), len(a_body)) >= RENAME_MIN_BODY))
            if ok:
                scored.append((name + body, g["name"], n["name"]))
    taken_old, taken_new, pairs = set(), set(), {}
    for _, old, nw in sorted(scored, reverse=True):
        if old in taken_old or nw in taken_new:
            continue
        taken_old.add(old)
        taken_new.add(nw)
        pairs[nw] = old
    if not pairs:
        return rows
    was = {r["name"]: r for r in gone}
    out = []
    for r in rows:
        if r["status"] == "deleted" and r["name"] in taken_old:
            continue
        if r["status"] == "added" and r["name"] in pairs:
            old = was[pairs[r["name"]]]
            r = dict(r, status="modified", renamedFrom=old["name"])
            if old.get("wasSilenced"):
                r["wasSilenced"] = old["wasSilenced"]
        out.append(r)
    return out


# --------------------------------------------------------------------------- #
# where a test case's own lines end
# --------------------------------------------------------------------------- #
# "Modified" means the diff touched a line of the test's own: its tags or annotations, its
# declaration, its body. The body used to be "up to the next declaration", which for the
# last test of a file was nothing at all — `next(…, line + 1)`, the declaration line alone —
# so eval run 8's OwnerSearchThroughLatencyProxyTest, one test with its assertion rewritten,
# came out `unchanged` and the ✍️ chip read one short. It also charged a changed
# `@ValueSource` to the test *above* it, and a helper edited between two tests to the first.
_BRACED = (".java", ".kt", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go")
_STRINGS = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`")
_GHERKIN_STOP = re.compile(r"^\s*(?:Scenario|Scenario Outline|Scenario Template|Example|"
                           r"Rule|Feature|Background)\s*:|^\s*@")


def case_span(rel: str, lines: list[str], line: int, next_start: int | None) -> tuple[int, int]:
    """`(first, last)` 1-based lines, inclusive, that are this test case's own: the
    `@…` lines right above its declaration, down to the end of its body — the brace that
    closes it, the end of its indented block (Python), the line before the next scenario
    or tag (Gherkin). Never past the line before the next declaration, and never short of
    the declaration itself."""
    cap = (next_start - 1) if next_start else len(lines)
    first = line
    while first > 1 and lines[first - 2].strip().startswith("@"):
        first -= 1
    last = cap
    if rel.endswith(_BRACED):
        depth, opened = 0, False
        for n in range(line, cap + 1):
            code = _STRINGS.sub("", lines[n - 1]).split("//", 1)[0]
            for ch in code:
                if ch == "{":
                    depth, opened = depth + 1, True
                elif ch == "}":
                    depth -= 1
            if opened and depth <= 0:
                last = n
                break
    elif rel.endswith(".py"):
        own = _indent(lines[line - 1])
        last = line
        for n in range(line + 1, cap + 1):
            text = lines[n - 1]
            if text.strip() and _indent(text) <= own:
                break
            if text.strip():
                last = n
    elif rel.endswith(".feature"):
        last = line
        for n in range(line + 1, cap + 1):
            if _GHERKIN_STOP.match(lines[n - 1]):
                break
            if lines[n - 1].strip():
                last = n
    return first, max(last, line)


def _spans(rel: str, text: str, cases: dict) -> dict[str, tuple[int, int]]:
    lines = text.splitlines()
    starts = sorted(ln for ln, _ in cases.values())
    return {name: case_span(rel, lines, ln, next((s for s in starts if s > ln), None))
            for name, (ln, _) in cases.items()}


# --------------------------------------------------------------------------- #
# a test edited through a helper it calls
# --------------------------------------------------------------------------- #
# Eval run 10: AddVisitApiTest's one test kept every line of its own, while the private
# `anOwnerWithAPet()` it calls was rewritten (+14/−6) from one unpaged GET into a loop that
# walks pages and reads `.content`. The test now exercises different code, and the page
# filed it under "left exactly as they were". So a test whose own lines are untouched but
# which *directly* calls a function declared in the same file whose body the diff touched
# is `modified` too, carrying `viaHelper` — which helper, where, and how much of it moved.
#
# Conservative on purpose, the same way `pair_renames` is: same file only, a call written
# in the test's own lines only (`helper(`, `this.helper(`, `self.helper(` — not a method
# reference, not a helper reached through another helper, not a `@BeforeEach` nobody calls
# by name), and the helper must be a declaration outside every test's own lines.
_NOT_A_DECL = frozenset(("return", "new", "else", "throw", "if", "for", "while", "switch",
                         "catch", "synchronized", "do", "try", "case", "assert", "yield",
                         "await"))
JS_HELPER = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*\("
                       r"|^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?"
                       r"(?:function\b|\([^)]*\)\s*(?::[^=]*)?=>|\w+\s*=>)")
PY_HELPER = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")
GO_HELPER = re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)\s*\(")


def _helper_name(rel: str, line: str) -> str | None:
    """The function a line of a test file declares, if it declares one."""
    s = line.strip()
    if not s or s.startswith(("//", "*", "/*", "@", "#")):
        return None
    if rel.endswith((".java", ".kt")):
        m = JAVA_METHOD.match(line)
        if not m or s.endswith(";"):
            return None
        head = line[:m.start(1)]
        if "=" in head or m.group(1) in _NOT_A_DECL or set(head.split()) & _NOT_A_DECL:
            return None
        return m.group(1)
    if rel.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs")):
        m = JS_HELPER.match(line)
        return (m.group(1) or m.group(2)) if m else None
    if rel.endswith(".py"):
        m = PY_HELPER.match(line)
        return m.group(1) if m else None
    if rel.endswith(".go"):
        m = GO_HELPER.match(line)
        return m.group(1) if m else None
    return None


def _helper_span(rel: str, lines: list[str], line: int) -> tuple[int, int]:
    """A helper's own lines. In a braced language a statement that ends (`;`) before any
    brace opens is a one-line helper (`const page = () => x;`) — the brace scan would
    otherwise take the next `{` in the file for its body."""
    if rel.endswith(_BRACED):
        for n in range(line, len(lines) + 1):
            code = _STRINGS.sub("", lines[n - 1]).split("//", 1)[0]
            if "{" in code:
                break
            if ";" in code:
                return line, n
    return case_span(rel, lines, line, None)


def changed_helpers(rel: str, after: str, test_spans: dict, added: set[int],
                    removed: dict[int, int]) -> dict[str, list[dict]]:
    """`{name: [{"name", "line", "added", "removed"}]}` — every function declared in this
    test file, outside the tests' own lines, whose body the diff touched. A list per name,
    because an overload is a second body under the same call."""
    lines = after.splitlines()
    spans = list(test_spans.values())
    tests = set(test_spans)
    out: dict[str, list[dict]] = {}
    skip_to = 0
    for i, line in enumerate(lines, start=1):
        if i <= skip_to or any(a <= i <= b for a, b in spans):
            continue
        name = _helper_name(rel, line)
        if not name or name in tests:
            continue
        first, last = _helper_span(rel, lines, i)
        # Whatever is declared inside this body is part of it, not a helper of its own.
        skip_to = last
        plus = sum(1 for n in added if first <= n <= last)
        minus = sum(1 for n in removed.values() if first <= n <= last)
        if plus or minus:
            out.setdefault(name, []).append({"name": name, "line": i,
                                             "added": plus, "removed": minus})
    return out


def helpers_called(lines: list[str], first: int, last: int,
                   helpers: dict[str, list[dict]]) -> list[dict]:
    """The changed helpers a test calls by name in its own lines, in the order it calls
    them first."""
    if not helpers:
        return []
    code = "\n".join(_STRINGS.sub('""', x).split("//", 1)[0]
                     for x in lines[first - 1:last])
    found = []
    for name, decls in helpers.items():
        m = re.search(r"(?:^|[^\w$.]|\bthis\.|\bself\.)" + re.escape(name) + r"\s*\(", code,
                      re.M)
        if m:
            found.append((m.start(), decls))
    return [d for _, decls in sorted(found, key=lambda x: x[0]) for d in decls]


def classify_file(rel: str, status: str, before: str | None, after: str | None,
                  added: set[int], removed: dict[int, int]) -> list[dict]:
    """Every test case in one changed file, with what happened to it."""
    before_cases = scan_cases(rel, before) if before is not None else {}
    after_cases = scan_cases(rel, after) if after is not None else {}
    commented = commented_cases(rel, after) if after is not None else {}
    touched = added | set(removed.values())
    rows: list[dict] = []

    after_spans = _spans(rel, after, after_cases) if after is not None else {}
    helpers = (changed_helpers(rel, after, after_spans, added, removed)
               if before is not None and after is not None else {})
    after_lines = after.splitlines() if after is not None else []
    for name, (line, silenced) in sorted(after_cases.items(), key=lambda kv: kv[1][0]):
        first, last = after_spans[name]
        was = before_cases.get(name)
        via = []
        if was is None:
            state = "added"
        elif any(first <= t <= last for t in touched):
            state = "modified"
        else:
            via = helpers_called(after_lines, first, last, helpers)
            state = "modified" if via else "unchanged"
        row = _row(name, rel, state, line, silenced, was[1] if was else None)
        if via:
            row["viaHelper"] = via
        rows.append(row)

    before_spans = _spans(rel, before, before_cases) if before is not None else {}
    for name, (line, was_silenced) in sorted(before_cases.items(), key=lambda kv: kv[1][0]):
        if name in after_cases:
            continue
        first, last = before_spans[name]
        anchors = [new for old, new in removed.items() if first <= old <= last]
        # Commented out rather than removed: the body is still in the file, so the row
        # still has somewhere to go -- the comment itself, which is the thing the
        # reviewer has to judge.
        parked = commented.get(name)
        row = _row(name, rel, "deleted",
                   parked if parked is not None else (min(anchors) if anchors else None),
                   "commented" if parked is not None else None, was_silenced)
        # Where it was declared at the base. The working-tree `line` is only where its
        # removal landed — unrelated code sits there now — so the page links a deleted
        # test to this line at the base commit instead (`baseUrl`, filled in by `collect`).
        row["baseLine"] = line
        if status == "D":
            # The file itself is gone, so there is nothing to open. Saying that is
            # better than emitting a link that dead-ends in the editor.
            row["gone"] = True
            row["line"] = None
        rows.append(row)
    if before is not None and after is not None:
        rows = pair_renames(rel, before, after, rows, added, removed)
    return rows


def totals(rows: list[dict]) -> dict:
    """The arithmetic the chip at the top of the page states: how many tests this change
    set put into the run, and how many it took out of it.

    A test counts as *running* when it is declared and not silenced, which is what makes
    the three ways of losing one commensurable: deleting it, commenting it out and
    hanging an `@Disabled` on it all cost the run exactly one test, and a reviewer told
    only about the first has been told the smallest of the three truths. It also stops
    the count from moving on a test that was already switched off before the branch
    touched it -- deleting a test nobody was running changes nothing.

    `gained - lost == runningAfter - runningBefore` by construction, and the tests hold
    it to that: two numbers on a chip that do not reconcile with the rows behind them
    are worse than no chip."""
    t = dict.fromkeys(("added", "modified", "deleted", "unchanged", "commented",
                       "disabled", "reenabled", "runningBefore", "runningAfter",
                       "gained", "lost", "renamed", "viaHelper"), 0)
    for r in rows:
        t[r["status"]] += 1
        # A rename is one of the `modified`: the run kept the test under a new title.
        t["renamed"] += bool(r.get("renamedFrom"))
        # So is a test edited only through a same-file helper it calls (`helpers_called`).
        t["viaHelper"] += bool(r.get("viaHelper"))
        ran_before = r["status"] != "added" and not r.get("wasSilenced")
        runs_now = r["status"] != "deleted" and not r.get("silenced")
        t["runningBefore"] += ran_before
        t["runningAfter"] += runs_now
        if r.get("silenced") == "commented":
            t["commented"] += 1
        if runs_now and not ran_before:
            t["gained"] += 1
            if r["status"] != "added":
                t["reenabled"] += 1
        elif ran_before and not runs_now:
            t["lost"] += 1
            if r["status"] != "deleted":
                t["disabled"] += 1
    return t


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def github_repo(remote: str) -> str:
    """`https://github.com/owner/repo` for an `origin` remote on github.com, or "" — the
    ssh form, the https form, with or without `.git`. Anything else has no blob URL this
    script can be sure of, and a guessed link is worse than none."""
    m = re.match(r"^(?:git@github\.com:|ssh://git@github\.com/|https?://(?:[^@/]+@)?github\.com/)"
                 r"([^/\s]+/[^/\s]+?)(?:\.git)?/?$", remote.strip())
    return f"https://github.com/{m.group(1)}" if m else ""


def base_url(repo: str, sha: str, rel: str, line: int | None) -> str:
    """The test as it was at the base commit, on github.com — the only place a deleted
    test still exists. Empty without a GitHub remote or a resolved commit."""
    if not repo or not sha:
        return ""
    return f"{repo}/blob/{sha}/{rel}" + (f"#L{line}" if line else "")


def link_deleted_to_base(root: Path, base: str, rows: list[dict]) -> str:
    """Stamp every deleted row with `baseUrl` (and `baseSha`) — its declaration at the
    base commit. Returns the resolved base commit, "" when it cannot be resolved."""
    sha = git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").stdout.strip()
    repo = github_repo(git(root, "remote", "get-url", "origin").stdout)
    for r in rows:
        if r["status"] != "deleted" or r.get("silenced") == "commented":
            continue
        if sha:
            r["baseSha"] = sha
        url = base_url(repo, sha, r["path"], r.get("baseLine"))
        if url:
            r["baseUrl"] = url
    return sha


def collect(root: Path, base: str, paths: list[str]) -> list[dict]:
    names = git(root, "diff", "--name-status", base, "--", *paths)
    if names.returncode != 0:
        raise SystemExit(f"[test-changes] git diff failed against {base}:\n{names.stderr.strip()}")
    rows: list[dict] = []
    for entry in names.stdout.splitlines():
        parts = entry.split("\t")
        if len(parts) < 2:
            continue
        status, rel = parts[0][0], parts[-1]
        if not is_test_file(rel):
            continue
        before = None if status == "A" else git(root, "show", f"{base}:{rel}").stdout
        after = None
        if status != "D":
            f = root / rel
            after = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else None
        diff = git(root, "diff", "--unified=0", base, "--", rel).stdout
        added, removed = hunk_lines(diff)
        rows.extend(classify_file(rel, status, before, after, added, removed))
    link_deleted_to_base(root, base, rows)
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[], help="limit the scan to these paths")
    ap.add_argument("--base", required=True, help="the branch or commit to compare against")
    ap.add_argument("--out", help="write the JSON here (default: stdout)")
    args = ap.parse_args(argv)

    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").stdout.strip() or ".")
    rows = collect(root, args.base, args.paths)
    t = totals(rows)
    doc = {"base": args.base, "totals": t, "tests": rows}
    text = json.dumps(doc, indent=2) + "\n"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        detail = ", ".join(f"{t[k]} {k}" for k in
                           ("added", "modified", "renamed", "viaHelper", "deleted", "commented",
                            "disabled", "reenabled")
                           if t[k])
        print(f"[test-changes] {len(rows)} test cases in changed test files -> {args.out}"
              + (f" ({detail})" if detail else "")
              + f"; the run goes from {t['runningBefore']} to {t['runningAfter']} "
                f"(+{t['gained']} / -{t['lost']})",
              file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
