"""The diagram gallery: manifests, view switchers, draw.io widgets, .puml renders."""
from __future__ import annotations

import html
import json
import re
import subprocess
import sys
from pathlib import Path

from .commands import rerun_html
from .genseq import genseq_details_at_base, genseq_details_at_render, test_of_genseq
from .svg import inline_svg
from .util import _pretty

def read_manifest(path: Path):
    rows = []
    if not path.is_file():
        return rows
    lines = path.read_text(encoding="utf-8").splitlines()
    header = lines[0].split("\t")
    for line in lines[1:]:
        if line.strip():
            rows.append(dict(zip(header, line.split("\t"))))
    return rows


VIEW_WORDS = {"new": "New", "old": "Old"}


def dgm_views_html(panes, initial: str = "diff") -> str:
    """The Diff / New-Old control and its panes — the ONE implementation of it.

    `panes` is an ordered list of `(view, inner-html)` with `"diff"` first. Three states
    off two buttons: the second holds both words and toggles between them, with the live
    one underlined, because a third pill costs as much room as the two that carry the
    argument and this control has to sit above a tall sequence diagram as comfortably as
    above a small structural one.

    `initial` is which of the three the widget opens on. It is "diff" for a *structural*
    diagram, where the delta is a clean two-colour drawing and the whole point of the
    section. Two callers pass "new" instead, for the same reason in two guises: the delta
    is not trustworthy enough to be the first thing read. The UX audit's is a pixel mask
    over a screenshot, and a half-ghosted photo of a form has to be decoded before it says
    anything; a sequence diagram's is drawn from traces, and a run that reorders two
    concurrent calls — or a generator that relabels an arrow — is reported as a change
    nobody made. In both cases the undiffed picture is the one that reads at a glance, and
    the delta stays one click away for when the question is what moved.

    Callers: `render_diagrams` below, for every PlantUML delta, and `drawio_widget_html`,
    for the hand-drawn conceptual model. Behaviour and styling live in `DGM_VIEWS_JS` / the
    stylesheet and are delegated off `document`, so a widget that lands in a section body
    is driven by the same code as one emitted here — a second implementation of this would
    drift within a week.
    """
    pair = [v for v, _ in panes if v in VIEW_WORDS]
    # Nothing to switch to: a lone "Diff" button is a control that does nothing, and a
    # frame colour-coding one state is a legend for a single entry. Emit the picture.
    if not pair:
        return "".join(body for _, body in panes)
    # A caller asking to open on a side that did not render would open on nothing at all,
    # so an absent side falls back to the delta rather than blanking the widget.
    if initial not in {v for v, _ in panes}:
        initial = "diff"
    buttons = ['<button type="button" class="dgm-diff" data-go="diff" '
               f'aria-pressed="{str(initial == "diff").lower()}" '
               'data-tip="Green added, red removed">Diff</button>']
    if pair:
        buttons.append(
            '<button type="button" class="dgm-newold" data-go="newold" '
            f'aria-pressed="{str(initial != "diff").lower()}" '
            'data-tip="The diagram without diff. Click again to swap.">'
            + "/".join(f'<u data-view="{v}"{' class="on"' if v == initial else ""}>'
                       f'{VIEW_WORDS[v]}</u>' for v in pair)
            + "</button>")
    panels = "".join(
        f'<div class="dgmpane" data-view="{v}"{"" if v == initial else " hidden"}>{body}</div>'
        for v, body in panes)
    return (f'<div class="dgmviews" data-state="{initial}"><div class="dgmbar">'
            + "".join(buttons) + "</div>" + panels + "</div>")


def _diagram_views(row, assets: Path, full_svg: Path, root: Path):
    """One diagram's panes: the delta (with its focus chooser inside it) plus whichever
    of the undiffed pair `puml-diff.sh` managed to render. Returns the markup and whether
    a New/Old pair exists — the header only advertises itself as a toggle when it does.

    A sequence diagram opens on `New`, a structural one on `Diff`. Not a preference: a
    structural delta is derived from two files a human wrote, so every mark in it is a
    change somebody made, while a sequence delta is derived from two *recordings*. The
    order of concurrent calls is not stable between runs and the generator's own labels
    move under it, so the differ reliably reports arrows nobody touched — which is what
    the New/Old button's tooltip has always warned about, and what the guide keeps having
    to say out loud next to the picture. Opening on the recording itself puts the reader
    in front of something true first; the delta is one click away, where the claim it
    makes can be taken with the caveat it needs."""
    panes = [("diff", _focus_views(row, assets, full_svg, root))]
    for view, column in (("new", "new_svg"), ("old", "old_svg")):
        name = (row.get(column) or "").strip()
        if name and (assets / name).is_file():
            panes.append((view, f'<div class="svgbox">{inline_svg(assets / name, root)}</div>'))
    if len(panes) == 1:
        return panes[0][1], False
    return dgm_views_html(panes, initial="new" if row.get("kind") == "sequence" else "diff"), True


# `{{drawio:conceptual}}` — the hand-drawn diagram's three pictures, read off
# `.human-review/assets/` at build time.
#
# It exists because this one diagram is the only thing on the page the report *asks the
# reader to go and change*: red is automation's to-do, and the picture is supposed to look
# different once a human has re-laid it out in draw.io. Markup pasted into content.json
# freezes it — the reader re-draws the map, rebuilds the page, and still sees the drawing
# that was current when a model last wrote the section, with a legend still promising a
# layout that has since been drawn. The token keeps the pictures in the files
# `drawio-diff.py` writes, so re-running that step and rebuilding is the whole refresh.
DRAWIO_TOKEN = re.compile(r"\{\{drawio:(?P<name>[A-Za-z0-9_.-]+)\}\}")

# Meanings, not colours: the swatch is already the colour, so the bold goes on the one
# thing the reader cannot see.
#
# The to-do row names the state and stops. It used to carry the whole instruction as well
# — what the red is, and to go open it in draw.io and turn every line black — and that
# sentence read as a puzzle at the size a legend is read at: the reader is standing in
# front of a picture, and the legend was explaining a workflow. The instruction is not
# lost, it is written on the map itself, in red, by `conceptual-model-patch.py`, where
# the person who can act on it is already looking. Both rows are read off the verdict, so
# the page stops saying it the moment it stops being true.
#
# The green row stops at "added by this PR" for the same reason. "New against the base
# branch" was the definition of "added by this PR" — the same fact, restated for a reader
# who has evidently understood it, since they are reading a legend on a diff.
CM_LEGEND_NEW = '<span class="new"><i></i><b>added by this PR</b></span>'

CM_LEGEND_TODO = ('<span class="todo"><i></i>'
                  "<b>still waiting for a manual re-layout</b></span>")


def drawio_unchanged(verdict: dict) -> bool:
    """Did `drawio-diff.py` find nothing — no box or line added, removed, changed or
    moved, and nothing left red for a re-layout? An absent verdict is not "unchanged":
    it is a step that has not run, and the widget says that instead."""
    return bool(verdict) and not any(
        verdict.get(k) for k in ("added", "removed", "changed", "moved", "red"))


def drawio_unchanged_at(name: str, assets: Path) -> bool:
    vfile = assets / f"{name}-diff.json"
    try:
        return drawio_unchanged(json.loads(vfile.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return False


def _drawio_unchanged_card(name: str, verdict: dict, assets: Path, root: Path) -> str:
    """The hand-drawn diagram, when the branch did not touch it: the card a `puml`
    context diagram gets — title, UNCHANGED, the file, the whole picture.

    Everything else the widget carries is about a change. Diff / New-Old switches
    between two identical drawings; the red frame marks a delta that is empty; and the
    offer above it — relayout in draw.io, update the report, revert the diagram — is
    the to-do list for a re-layout nobody owes."""
    svg = next((assets / f"{name}-{suffix}.svg" for suffix in ("new", "original", "diff")
                if (assets / f"{name}-{suffix}.svg").is_file()), None)
    if svg is None:
        return (f'<p class="sub">not rendered — run the <code>diagrams</code> step to '
                f'write <code>{html.escape(name)}-new.svg</code></p>')
    rel = verdict.get("diagram") or ""
    title = _pretty(Path(rel).name.split(".")[0]) if rel else _pretty(name)
    return ('<div class="diagram"><div class="head">'
            f'<b>{html.escape(title)}</b>{UNCHANGED_BADGE}'
            + (_source_link(rel, root) if rel else "") + '</div>'
            f'<div class="svgbox">{inline_svg(svg, root)}</div></div>')


def drawio_widget_html(name: str, assets: Path, root: Path, rebuild: str = "") -> str:
    """The Diff / New / Old widget for one `drawio-diff.py` output set.

    Which pane it opens on is not a style choice, it is a reading of the verdict: while
    anything in the drawing is still red, the delta is a picture of automation's routing
    rather than of the change, and `New` is the pane that answers first. Once the layout
    has been drawn by hand there is no red left, and `Diff` earns the open — which is the
    same rule the author used to have to remember and re-type after every re-layout.
    """
    verdict = {}
    vfile = assets / f"{name}-diff.json"
    if vfile.is_file():
        verdict = json.loads(vfile.read_text(encoding="utf-8"))
    if drawio_unchanged(verdict):
        return _drawio_unchanged_card(name, verdict, assets, root)
    red = bool(verdict.get("red"))
    green = any(not a.get("already_red") for a in verdict.get("added") or [])

    legend = (f'<p class="cmlegend">{CM_LEGEND_NEW if green else ""}'
              f'{CM_LEGEND_TODO if red else ""}</p>') if (green or red) else ""
    # Repeated under `New`, where the red is on screen with nothing else to explain it.
    # The green is not: nothing is coloured green in the undiffed drawing.
    todo_only = f'<p class="cmlegend">{CM_LEGEND_TODO}</p>' if red else ""

    panes = []
    for view, suffix, tail in (("diff", "diff", legend),
                               ("new", "new", todo_only),
                               ("old", "original", "")):
        svg = assets / f"{name}-{suffix}.svg"
        if svg.is_file():
            panes.append((view, f'<div class="svgbox">{inline_svg(svg, root)}</div>{tail}'))
    if not panes:
        print(f"[review] no {name}-*.svg under {assets} — run the diagrams step",
              file=sys.stderr)
        return (f'<p class="sub">not rendered — run the <code>diagrams</code> step to '
                f'write <code>{html.escape(name)}-diff.svg</code></p>')
    # `rerun_html` first, `dgm_views_html` after: this is where the caption sentence used
    # to sit, above the picture, naming the artefact and how honest it is. That prose is
    # gone from `content.json` now — the edit offer and its two buttons are the one thing
    # above the picture instead, so the block reads title, then what to do about the
    # drawing, then the drawing itself.
    return (rerun_html(verdict.get("rerun"), rebuild, name,
                       verdict.get("drawio_url") or "",
                       verdict.get("drawio_web_url") or "",
                       verdict.get("redraw"), verdict.get("revert"),
                       verdict.get("reveal"), verdict.get("tested_against") or "")
            + dgm_views_html(panes, initial="new" if red else "diff"))


def expand_drawio(text: str, out_dir: Path, root: Path, rebuild: str) -> str:
    return DRAWIO_TOKEN.sub(
        lambda m: drawio_widget_html(m["name"], out_dir / "assets", root, rebuild), text)


DGM_SRC_ANCHOR = re.compile(r'<a class="dgm-src"(?P<attrs>[^>]*)>(?P<face>[^<]+)</a>')


def shorten_dgm_src(markup: str) -> str:
    """A diagram header names its file by **name**, with the path on hover.

    `petclinic-backend/docs/ConceptualModel.drawio.png` spends two segments on where the
    repository keeps its documents before reaching the one word that answers "which
    drawing is this?" — and it does it in the header of a card whose title already said
    *Conceptual Model*. The name alone is the same answer in a quarter of the width; the
    path is still one hover away, which is where a reader goes only when they want to
    find the file rather than read the picture.

    This is the rule `srcbar_html` already applies to every quoted block on the page,
    down to the wording of the tip, so the two rows read the same way. It runs over
    rendered markup rather than at each call site because the conceptual model's header
    is written by hand into `content.json` — a rule enforced only in `_source_link`
    would hold for the generated PlantUML cards and quietly not for the one card the
    reader is being asked to go and edit.

    Idempotent: a face with no slash left in it is already short (or is a file at the
    repository root, which has no path to move)."""
    def one(m: re.Match) -> str:
        rel = html.unescape(m["face"])
        if "/" not in rel:
            return m.group(0)
        attrs = re.sub(r'\s+data-tip="[^"]*"', "", m["attrs"])
        tip = html.escape(f"Open in VS Code: {rel}", quote=True)
        return (f'<a class="dgm-src"{attrs} data-tip="{tip}">'
                f'{html.escape(Path(rel).name)}</a>')
    return DGM_SRC_ANCHOR.sub(one, markup)


def _source_link(rel: str, root: Path) -> str:
    """The path already shown on the right of the header, made the link to the file.

    It used to be plain text with a second `<a>name.puml</a>` under the title — two
    controls for one destination, and the shorter of the two said less."""
    if (root / rel).is_file():
        return shorten_dgm_src(
            f'<a class="dgm-src" href="vscode://file/{(root / rel).resolve()}:1:1">'
            f'{html.escape(rel)}</a>')
    return f'<span>{html.escape(rel)}</span>'


def _provenance(rel: str, root: Path) -> str:
    """Links back to what produced a diagram: the test that generated it, and the .puml.

    A sequence diagram is evidence only if the reviewer can reach the scenario behind it.
    `test_of_genseq` is what derives that — from the picture's own `src://` handle, or
    failing that from its name — so the guide never has to be told, and the link survives
    both the per-scenario naming and the generator filing its output away from the test."""
    links = []
    if rel.endswith('.genseq.puml'):
        test = test_of_genseq(rel, root)
        if (root / test).is_file():
            links.append(f'<a class="srcref" href="vscode://file/{(root / test).resolve()}:1:1">'
                          f'generated by {html.escape(Path(test).name)}</a>')
    return ('<p class="prov">' + " ".join(links) + '</p>') if links else ''


# Which radius a reviewer meets first — the change and nothing else.
#
# It opened on the whole diagram for a while, on the reasoning that a pruned view is a
# claim that the rest does not matter. In practice the whole DB and DomainModel deltas are
# a wall of forty unchanged entities with the change somewhere inside, so it moved to one
# hop of context, and then to none: the first question a delta has to answer is *what
# changed*, and every element beside it is a candidate the eye still has to rule out. Zero
# is the only level that cannot mislead about that. Each wider radius is one click away,
# and the reader takes it the moment the change alone does not explain itself.
DEFAULT_FOCUS = "0"


def _focus_views(row, assets: Path, full_svg: Path, root: Path) -> str:
    """The delta at each focus level, one visible at a time, with the chooser above them.

    All of them are inlined rather than fetched on demand: the guide has to survive being
    emailed as a single file, and a chooser whose other options 404 is worse than none.
    """
    levels = []
    for pair in (row.get("focus") or "").split(","):
        level, sep, name = pair.partition(":")
        svg = assets / name
        if sep and svg.is_file():
            levels.append((level, svg))
    levels.append(("all", full_svg))

    if len(levels) == 1:                       # sequence diagrams, and anything unpruned
        return f'<div class="svgbox">{inline_svg(full_svg, root)}</div>'

    default = DEFAULT_FOCUS if any(l == DEFAULT_FOCUS for l, _ in levels) else "all"
    buttons = "".join(
        f'<button type="button" data-level="{html.escape(level)}" '
        f'aria-pressed="{"true" if level == default else "false"}">{html.escape(level)}</button>'
        for level, _ in levels
    )
    boxes = "".join(
        f'<div class="svgbox" data-level="{html.escape(level)}"'
        f'{"" if level == default else " hidden"}>{inline_svg(svg, root)}</div>'
        for level, svg in levels
    )
    return (
        '<div class="focus"><span class="lbl">Diff + extra neighbours:</span>'
        + buttons + "</div>" + boxes
    )


def select_rows(rows, block) -> list:
    """The manifest rows one diagram block is responsible for.

    Tabs split the gallery by what a diagram *answers* — sequence diagrams sit next to
    the tests that generated them, DB and DomainModel next to each other — so a block
    names either the families it takes (`kind`) or the diagrams themselves (`only`)."""
    kinds = block.get("kind")
    if kinds:
        kinds = [kinds] if isinstance(kinds, str) else kinds
        rows = [r for r in rows if r["kind"] in kinds]
    only = block.get("only")
    if only:
        rows = [r for r in rows if r["name"] in only]
    if block.get("except"):
        rows = [r for r in rows if r["name"] not in block["except"]]
    return rows


def _why_not_drawn(row, assets: Path) -> str:
    """What a pair says where its picture should be — in PlantUML's words, not ours.

    This used to read `not rendered — see <file>.diff.puml`, which says nothing a reader
    can act on: it sounds like a step that has not run yet, and it points at a source file
    that looks perfectly fine, because the fault is one line of it. PlantUML does not fail
    on a bad line — it draws a picture of the complaint and exits 0 — so the pipeline
    spots the words "Syntax Error", drops that picture, and leaves the complaint in a
    `.err` sidecar. Read it back here: the message, the line, and a click that opens the
    source on it."""
    puml = row.get("diff_puml") or ""
    err = assets / f"{puml}.err"
    reason = {}
    if puml and err.is_file():
        try:
            reason = json.loads(err.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            reason = {}
    line = reason.get("line") or 0
    src = assets / puml if puml else None
    face = html.escape(puml or "the delta source")
    link = (f'<a class="dgm-src" href="vscode://file/{src.resolve()}:{line or 1}:1">{face}</a>'
            if src and src.is_file() else f"<code>{face}</code>")
    if not reason:
        return f'<p class="sub">not rendered — see {link}</p>'
    where = f" at line {line}" if line else ""
    offending = (f'<br><code>{html.escape(reason.get("source") or "")}</code>'
                 if reason.get("source") else "")
    return (f'<p class="sub">diagram could not be drawn: '
            f'{html.escape(reason.get("message") or "PlantUML could not parse it")}'
            f'{where} of {link}{offending}</p>')


#: The status of a diagram the branch did not change: on a manifest row (`puml-diff.sh`
#: files a structural diagram whose text moved but whose elements and relationships did
#: not as `unchanged`; the C2 projection files its own the same way), and on a row made
#: up for a diagram an `only` names that no manifest row covers.
UNCHANGED = "unchanged"

#: The one look "this branch left it alone" has, wherever a diagram is drawn: the badge
#: the Structure tab's `puml` cards have always carried. A C2 row filed as unchanged used
#: to wear the grey `sev-low` badge a *deleted* diagram wears, inside the full Diff /
#: New-Old frame — two looks for one fact on the same tab.
UNCHANGED_BADGE = f'<span class="badge sev-info">{UNCHANGED}</span>'

#: An unchanged PICTURE over a source that did change where the picture cannot show it —
#: `_unseen`, the line `schema_unseen_note` writes under an ERD whose dump gained indexes.
#: It used to wear UNCHANGED, and the line under it said `DB.sql changed: indexes added`:
#: two judges of eval run 5 read the pair as the page contradicting itself. Its own word,
#: in the amber a finding-to-look-at wears, and the hover says what the two halves mean.
SCHEMA_ONLY = "schema only"
SCHEMA_ONLY_BADGE = (f'<span class="badge sev-med" data-tip="Schema changed outside the '
                     f'diagram, see below">{SCHEMA_ONLY}</span>')


def _unchanged_body(row, assets: Path, root: Path, out_dir: Path) -> str:
    """An unchanged diagram is the whole current picture and nothing else.

    No Diff / New-Old switch, no red frame, no focus chips: each of those is a control
    over a delta, and there is no delta. The whole diagram, not focus 0 — the radius
    chooser exists to find the change inside a large picture, and with no change the
    focus-0 view is an empty note saying so. The undiffed `new` render is preferred
    (`puml-diff.sh` already drew it); a row made up from source has none, and is drawn
    the way a `puml` context card is."""
    for column in ("new_svg", "svg"):
        name = (row.get(column) or "").strip()
        if name and (assets / name).is_file():
            return f'<div class="svgbox">{inline_svg(assets / name, root)}</div>'
    cache, why_not = _context_svg(row["source"], root, out_dir)
    return f'<div class="svgbox">{inline_svg(cache, root)}</div>' if cache else why_not


def find_diagram_source(name: str, root: Path) -> str | None:
    """The repository's `.puml` for a diagram an `only` names and no manifest row has.

    `puml-diff.sh` files only the diagrams whose text changed, so a block written as
    `only: ["DomainModel", "DB"]` used to show just the half that moved — and on a branch
    whose migration only added indexes, which the ERD does not draw, the Data tab had no
    DB at all. A name the author asked for is a promise to show that diagram; when it did
    not change it is shown as it stands, with the UNCHANGED badge. Looked up by file
    name, outside the review directory; an ambiguous name takes the shallowest path and
    says so."""
    out = subprocess.run(["git", "ls-files", "--", f"{name}.puml", f"*/{name}.puml"],
                         cwd=root, capture_output=True, text=True)
    hits = sorted((p for p in out.stdout.splitlines()
                   if p and not p.startswith(".human-review/")),
                  key=lambda p: (p.count("/"), p))
    if len(hits) > 1:
        print(f"[review] {name}.puml is ambiguous ({', '.join(hits)}) — showing {hits[0]}",
              file=sys.stderr)
    return hits[0] if hits else None


def unchanged_row(name: str, rel: str) -> dict:
    """A manifest-shaped row for a diagram this branch left alone, drawn from source."""
    return {"name": name, "source": rel, "kind": "structural", "status": UNCHANGED,
            "diff_puml": "", "svg": "", "focus": "", "new_svg": "", "old_svg": ""}


_SQL_TABLE = re.compile(r"^CREATE TABLE (?:\w+\.)?(?P<t>\w+) \($")
_SQL_COLUMN = re.compile(r"^\s+(?P<c>\w+) (?P<d>.+?),?$")
_SQL_INDEX = re.compile(r"^CREATE (?P<u>UNIQUE )?INDEX (?P<n>\w+) ON (?:ONLY )?(?:\w+\.)?"
                        r"(?P<t>\w+)(?: USING \w+)? (?P<cols>\(.*\));?$")
_SQL_COLLATE = re.compile(r"\s+COLLATE\s+\S+")


def _sql_shape(text: str):
    """`{table.column: definition}` and `{index: "table (columns)"}` out of a pg_dump."""
    cols, idx, table = {}, {}, None
    for line in text.splitlines():
        m = _SQL_TABLE.match(line)
        if m:
            table = m["t"]
            continue
        if table:
            if line.startswith(")"):
                table = None
                continue
            m = _SQL_COLUMN.match(line)
            if m and m["c"].upper() != "CONSTRAINT":
                cols[f'{table}.{m["c"]}'] = m["d"]
            continue
        m = _SQL_INDEX.match(line)
        if m:
            idx[m["n"]] = f'{"unique " if m["u"] else ""}{m["t"]} {m["cols"]}'
    return cols, idx


def schema_unseen_note(rel: str, root: Path, merge_base: str | None,
                       columns: bool = True) -> str:
    """What changed in the schema dump an ERD is drawn from that the ERD cannot show.

    An ERD `.puml` converted from a dump (`DB.sql` beside `DB.puml`) draws tables,
    columns and keys; an index or a column's collation lives only in the dump. So a
    migration that adds indexes leaves the picture byte-identical, and the card would say
    UNCHANGED about a schema that did change. Read the dump at the merge-base and in the
    work tree, and name the difference in one line under the card. `columns` is off for a
    diagram that changed: a column whose type moved is already drawn in its delta.

    Empty when there is no sibling dump, no base, or nothing the picture misses."""
    sql = Path(rel).with_suffix(".sql")
    if not merge_base or not (root / sql).is_file():
        return ""
    base = subprocess.run(["git", "show", f"{merge_base}:{sql.as_posix()}"], cwd=root,
                          capture_output=True, text=True)
    if base.returncode != 0:
        return ""
    old_cols, old_idx = _sql_shape(base.stdout)
    new_cols, new_idx = _sql_shape((root / sql).read_text(encoding="utf-8"))
    said = []
    added = [new_idx[n] for n in sorted(new_idx) if n not in old_idx]
    gone = [old_idx[n] for n in sorted(old_idx) if n not in new_idx]
    if added:
        said.append("indexes added on " + ", ".join(added))
    if gone:
        said.append("indexes dropped on " + ", ".join(gone))
    if columns:
        moved = [c for c in new_cols if c in old_cols and new_cols[c] != old_cols[c]]
        collation = [c for c in moved
                     if _SQL_COLLATE.sub("", new_cols[c]) == _SQL_COLLATE.sub("", old_cols[c])]
        other = [c for c in moved if c not in collation]
        if collation:
            said.append("collation changed on " + ", ".join(collation))
        if other:
            said.append("definition changed on " + ", ".join(other))
    if not said:
        return ""
    return "Also changed, not drawn: " + "; ".join(said) + "."


def render_diagrams(spec, root: Path, out_dir: Path, rows=None, bare: str = "") -> str:
    """`bare` is the test file a pair's heading already names.

    A sequence diagram inside a test pair used to print three answers to one question in
    four centimetres: a title that was the test's file name with `.genseq` on the end, a
    `generated by <test>` line under it, and the `.puml` path on the right — above a fold
    whose summary was that same file name. Only the last is news. So the title and the
    provenance line go, and the href they carried for the scenario links rides on the card
    as `data-test-src` instead."""
    manifest = out_dir / spec.get("manifest", "assets/diagrams/MANIFEST.tsv")
    if rows is None:
        rows = read_manifest(manifest)
        if spec.get("only"):
            rows = [r for r in rows if r["name"] in spec["only"]]
    if not rows:
        return '<p class="sub">No PlantUML diagram changed on this branch.</p>'
    notes = spec.get("notes", {})
    order = {"structural": 0, "sequence": 1}
    # An explicit `only` is a running order, not just a filter: an author who writes
    # ["DomainModel", "DB"] means the domain first. Alphabetical only decides the rest.
    wanted = spec.get("only") or []
    rows = sorted(rows, key=lambda r: (order.get(r["kind"], 9),
                                       wanted.index(r["name"]) if r["name"] in wanted else 99,
                                       r["name"]))
    parts = []
    for r in rows:
        note = notes.get(r["name"], "")
        svg_rel = manifest.parent / r["svg"] if r.get("svg") else None
        if r.get("status") == UNCHANGED:
            body, toggles = _unchanged_body(r, manifest.parent, root, out_dir), False
        elif svg_rel and svg_rel.is_file():
            body, toggles = _diagram_views(r, manifest.parent, svg_rel, root)
        else:
            body, toggles = _why_not_drawn(r, manifest.parent), False
        test_src = (f' data-test-src="vscode://file/{(root / bare).resolve()}:1:1"'
                    if bare and (root / bare).is_file() else "")
        parts.append(
            f'<div class="diagram{" dgm-toggles" if toggles else ""}{" dgm-bare" if bare else ""}"'
            f'{test_src}>'
            f'<div class="head">'
            + ("" if bare else f'<b>{html.escape(_pretty(r["name"]))}</b>')
            # A badge earns its place by saying something surprising. "modified" is what
            # a diagram in a delta gallery always is, and "structural" is legible from the
            # picture — so only the states that carry information get one.
            + (SCHEMA_ONLY_BADGE if r["status"] == UNCHANGED and r.get("_unseen") else
               UNCHANGED_BADGE if r["status"] == UNCHANGED else
               f'<span class="badge {"sev-high" if r["status"] == "added" else "sev-low"}">'
               f'{html.escape(r["status"])}</span>' if r["status"] != "modified" else "")
            + _source_link(r["source"], root) + '</div>'
            + (f"<p>{note}</p>" if note else "")
            + ("" if bare else _provenance(r["source"], root))
            + genseq_details_at_render(r, manifest.parent, root)
            + ("" if r["status"] == UNCHANGED
               else genseq_details_at_base(r, manifest.parent, root))
            + body
            + (f'<p class="sub dgm-unseen">{html.escape(r["_unseen"])}</p>'
               if r.get("_unseen") else "")
            # A producer's own line about the row (the C2 projection: "projected from the
            # committed sequences, not re-traced"). Text, escaped: a manifest is not HTML.
            + (f'<p class="sub dgm-stale">{html.escape(r["note"])}</p>'
               if (r.get("note") or "").strip() else "")
            + '</div>'
        )
    return "\n".join(parts)


def render_puml(block, root: Path, out_dir: Path) -> str:
    """A diagram this branch did *not* change, drawn as context rather than as a delta.

    Package structure is the case that asks for it: a reviewer wants to see the shape
    the change landed in even on the \u2014 common, and good \u2014 branches that left it alone.
    Rendered here from the committed source, so the page carries no stale SVG."""
    src = root / block["src"]
    cache, why_not = _context_svg(block["src"], root, out_dir)
    if not cache:
        return why_not
    return (
        '<div class="diagram">'
        f'<div class="head"><b>{html.escape(block.get("name", src.stem))}</b>'
        f'<span class="badge sev-info">{html.escape(block.get("status", "unchanged"))}</span>'
        + _source_link(block["src"], root) + '</div>'
        + (f'<p>{block["note"]}</p>' if block.get("note") else "")
        + _provenance(block["src"], root)
        + f'<div class="svgbox">{inline_svg(cache, root)}</div></div>'
    )


def _context_svg(rel: str, root: Path, out_dir: Path):
    """The committed `.puml` drawn as it stands, cached under assets/ by mtime.

    Returns `(svg_path, "")`, or `(None, <the paragraph to print instead>)`: a missing
    file and a missing PlantUML are the two ways there is no picture, and each is named
    rather than swallowed. Shared by the `puml` block and by a test pair whose sequence
    exists but carried no delta \u2014 the same picture, drawn the same way."""
    src = root / rel
    if not src.is_file():
        return None, f'<p class="sub">no diagram at <code>{html.escape(rel)}</code></p>'
    cache = out_dir / "assets" / (Path(rel).stem + ".context.svg")
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.is_file() or cache.stat().st_mtime < src.stat().st_mtime:
        out = subprocess.run(["plantuml", "-tsvg", "-pipe"],
                             input=src.read_bytes(), capture_output=True)
        if out.returncode != 0 or not out.stdout:
            return None, (f'<p class="sub">plantuml could not render '
                          f'<code>{html.escape(rel)}</code> \u2014 is it installed?</p>')
        cache.write_bytes(out.stdout)
    return cache, ""
