"""The Review tab: findings, assumptions, auto-fixes, the aftermath band."""
from __future__ import annotations

import html
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

from ..shared.actions import ACTIONS, declare_action, RERUN_ACTION
from ..shared.bands import _lede_above, _flush_top_bands
from ..shared.commands import command_html
from ..shared.snippets import DIFF_CONTEXT, diff_html

# The report's contract lives next to the scripts, beside the parser that writes it: the
# directory this package sits in is on sys.path whenever the package is importable.
import review_points_schema

SEVERITIES = {
    "high": ("sev-high", "must look"),
    "medium": ("sev-med", "worth a look"),
    "low": ("sev-low", "nit"),
    "info": ("sev-info", "context"),
}


#: Where `review-points.py` leaves what it parsed off the branch. A content file asks for
#: it with `{"auto": "review-points"}` on `findings` / `autofixes` / `assumptions` — the
#: same convention `diffstat` and `tests` already use for a number nobody should type.
REVIEW_POINTS_JSON = "review-points.json"

#: Which pile each `{"auto": …}` key fills, and the heading `review-points.md` writes it
#: under. Kept here rather than imported from the parser: this is the build's side of the
#: contract, and a rename in either file has to be a deliberate change in both.
POINTS_PILES = {"autofixes": "Fixed", "findings": "Ignored", "assumptions": "Assumptions"}


def _points_parser():
    """`review-points.py` — hyphenated, so loaded by path, once."""
    import importlib.util
    name = "hr_review_points_parser"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, Path(review_points_schema.__file__).with_name("review-points.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules[name] = mod
    return sys.modules[name]


def unglue_refs(item: dict) -> None:
    """Split, in place, any ref of a report item that is two refs glued with a comma.

    The parser splits `- file: a.ts:12, b.ts:30` now; a report written before it did —
    or edited by hand — carries `a.ts:12, b.ts:30` as one path, and the build used to abort
    the whole page on a file by that name not existing. A ref the parser's own check still
    refuses is dropped with a warning instead: one card missing, not the page."""
    rp = _points_parser()
    title = re.sub(r"<[^>]+>", "", str(item.get("title") or ""))[:60]

    def parts(ref: str) -> list[str]:
        got, bad = rp.split_refs(str(ref))
        if bad:
            print(f"[review] WARNING: {title!r}: ref {ref!r} dropped — "
                  + "; ".join(bad), file=sys.stderr)
            return []
        return got

    if isinstance(item.get("refs"), list):
        item["refs"] = [p for ref in item["refs"] for p in parts(ref)]
    if isinstance(item.get("snippets"), list):
        out = []
        for s in item["snippets"]:
            if not isinstance(s, dict) or "ref" not in s:
                out.append(s)
                continue
            for i, p in enumerate(parts(s["ref"])):
                if rp.RANGED.search(p):
                    one = {**s, "ref": p}
                    if i:
                        one.pop("caption", None)
                    out.append(one)
        item["snippets"] = out
    if isinstance(item.get("diffs"), list):
        out = []
        for d in item["diffs"]:
            if not isinstance(d, dict) or "path" not in d:
                out.append(d)
                continue
            out.extend({**d, "path": re.sub(rp.RANGED, "", p)} for p in parts(d["path"]))
        item["diffs"] = out


def resolve_review_points(spec: dict, out_dir: Path) -> dict | None:
    """Make the Review tab's inputs the branch's and the page's, not the content file's.

    The piles come off `review-points.md` (`resolve_piles`); each Fixed card is dealt the
    hunks of the fix commit its anchors reach (`attribute_fix_hunks`); the grade's reasons
    are computed from what the page measured and its number capped by them
    (`grade_signals`, `cap_grade`); and the content file's prose `summary`, which opened
    this tab above the grade, is dropped (`drop_model_summary`)."""
    drop_model_summary(spec)
    points = resolve_piles(spec, out_dir)
    attribute_fix_hunks(spec, out_dir)
    grade_signals(spec, out_dir)
    cap_grade(spec)
    return points


def drop_model_summary(spec: dict) -> None:
    """Drop `summary` when it would open the Review tab, and say so on stderr.

    It rendered as a bordered paragraph of the model's prose above the grade, where the
    reader arriving from the score expects the computed reasons. Everything it can say
    honestly the page now measures; what it says beyond that nobody checked."""
    tabs = spec.get("tabs") or []
    first = tabs[0] if tabs else {}
    if spec.get("summary") and any(b.get("type") in POINTS_PILES
                                    for b in first.get("blocks") or []):
        print("[review] content.json's `summary` is not rendered — the Review tab opens on "
              "the computed grade reasons, not on prose", file=sys.stderr)
        spec.pop("summary", None)


def resolve_piles(spec: dict, out_dir: Path) -> dict | None:
    """Fill the piles the content file delegated to `review-points.md`, in place.

    `content.json` stops being the judgement here. Before this, a model read the passes,
    decided what to fix and what to leave, wrote the three piles into the content file,
    and the page rendered its prose — so the record of the review was produced by the same
    conversation that produced the page, minutes after the fact, and nothing outside that
    file could corroborate a word of it. Now the coding agent writes `review-points.md`
    and commits it with the fixes, and this reads it: the content file keeps the layout and
    the ledes, which are the page's, and hands over the three piles, which are the
    branch's.

    The answer is recorded on the spec as `_reviewPoints` as well as returned, because
    every reader of it is downstream of one dict being threaded through eight call layers:
    the ledes, the piles and the band all have to agree about whether the record exists,
    and the spec is the thing all three already have in hand.

    None when no pile asked for it — an older content file with its piles written out is
    rendered exactly as it always was. Otherwise the dict says what the page now has to be
    honest about: `missing` when the file is not on the branch at all (and the piles are
    emptied, so nothing renders items nobody recorded), `sections` so an empty pile can
    say whether the file had no such section or an empty one.

    An absent file empties the piles rather than leaving whatever the content file happened
    to carry. A page that renders last week's findings under this week's diff is the one
    failure mode worse than a page that renders none.
    """
    asked = {k for k in POINTS_PILES
             if isinstance(spec.get(k), dict) and spec[k].get("auto") == "review-points"}
    path = out_dir / (spec.get("reviewPoints") or REVIEW_POINTS_JSON)
    doc = None
    if path.is_file():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as bad:
            raise SystemExit(f"[review] {path} is not JSON ({bad}) — regenerate it with "
                             "run-steps.py --only reviewpoints")
        # The report is the Review tab's only source, so its shape is checked before a
        # single pile renders — loudly, as `review-points.py` checks it on the way out. A
        # report from before the schema has no `schema` key and is refused the same way:
        # the reviewpoints step regenerates it from the committed file in a second.
        bad = review_points_schema.problems(doc)
        if bad:
            raise SystemExit(
                f"[review] {path} does not match "
                f"{review_points_schema.SCHEMA_PATH.name} — regenerate it with "
                "run-steps.py --only reviewpoints (or review-points.py):\n  "
                + "\n  ".join(bad[:20]))
        # When the branch carries a report, it is the Review tab: all three piles come
        # from it whatever the content file says. A content file that typed its own
        # piles beside a report is the old two-sources page, and the record wins.
        for key in POINTS_PILES:
            if isinstance(spec.get(key), list) and spec[key]:
                print(f"[review] WARNING: content.json writes its own `{key}`, ignored — "
                      f"the Review tab is rendered from {path.name} only", file=sys.stderr)
        asked = set(POINTS_PILES)
    if not asked:
        spec["_reviewPoints"] = None
        return None
    if not isinstance(doc, dict):
        for key in asked:
            spec[key] = []
        # Mode C, in the layout itself rather than at render time, so the counts line and
        # the pile agree. With no record on the branch the conversation that wrote the code
        # genuinely could not be asked — the thing that would have answered was never
        # written down — and the lede has to say that instead of counting an empty pile to
        # zero. `0 assumptions` is the good news ("it was asked, and named nothing"); this
        # is the other one.
        block = _assumptions_block(spec)
        if block is not None:
            block["mode"] = "C"
        print(f"[review] no {path.name} — the Review tab will say that nothing on this "
              "branch records what was reviewed, fixed or declined. Run "
              "run-steps.py --only reviewpoints, or accept the band: a branch nobody "
              "reviewed is a real state.", file=sys.stderr)
        spec["_reviewPoints"] = {"missing": True, "asked": asked, "sections": {},
                                 "path": path.name}
        own_review_tab(spec)
        return spec["_reviewPoints"]
    for key in asked:
        items = doc.get(key)
        spec[key] = items if isinstance(items, list) else []
        for item in spec[key]:
            if isinstance(item, dict):
                unglue_refs(item)
    for warning in doc.get("warnings") or []:
        print(f"[review] review-points: {warning}", file=sys.stderr)
    spec["_reviewPoints"] = {
        "missing": False, "asked": asked, "sections": doc.get("sections") or {},
        "source": doc.get("source") or "review-points.md",
        "fixed_in": doc.get("fixed_in"), "meta": doc.get("meta") or {},
        "provenance": doc.get("provenance") or {},
        "note": doc.get("note") if isinstance(doc.get("note"), dict) else None,
        "path": path.name}
    own_review_tab(spec)
    return spec["_reviewPoints"]


#: What each pile is called and what its intro says. The builder's, not the content
#: file's: a model writing content.json used to name the piles itself, and one run called
#: them "Candidates retained for human judgement", "Corrections from the recorded audit"
#: and "Implementation decisions" — the same three piles, unrecognisable from one page to
#: the next. With the piles read off the report, their titles are fixed here.
PILE_TITLES = {"assumptions": "Implementation assumptions",
               "findings": "Open review issues", "autofixes": "Auto-fixed"}

#: The tab's hover, owned for the same reason as the titles.
REVIEW_TAB_TIP = ("What the coding agent left open, fixed and assumed — its own "
                  "record, committed with the code.")


def pile_intro(kind: str, points: dict | None) -> str:
    """The one paragraph under a pile's heading, from the report rather than from prose.

    The fixed pile names the commit its diffs are measured against, because that is the
    one fact about it a reader cannot see: the left side of every diff below."""
    if kind == "assumptions":
        return ("Where the ticket was ambiguous, the reading that was taken — and under "
                "<b>Read the other way</b>, the reading that was not. Nothing here is a "
                "defect, and nothing here is in the diff: it is the only pile no pass, "
                "script or reviewer could reconstruct afterwards.")
    if kind == "findings":
        return ("Findings the agent read and said no to, with its reason. These are closed "
                "decisions, not a queue: your job here is to agree or disagree.")
    src = html.escape((points or {}).get("source") or "review-points.md")
    impl = ((points or {}).get("provenance") or {}).get("implementation", "")
    against = (f"<code>{html.escape(impl[:8])}</code>, the implementation commit"
               if impl else "the implementation commit")
    return (f"Read off <code>{src}</code>, committed with the fixes. Each one names the "
            f"reviewer that raised it and shows the hunks of the fix commit its "
            f"<code>file:line</code> reaches, against {against} — so what the review "
            "changed is separable from what the feature changed. Hunks no card reaches "
            "follow the pile.")


def own_review_tab(spec: dict) -> None:
    """Drop what the content file wrote over the piles: titles, intros, the tab's hover.

    Named on stderr, the way `own_layout` names what it drops from the script-owned
    tabs, so a content file that keeps typing them is told so instead of wondering why
    its words never reach the page."""
    for tab in spec.get("tabs") or []:
        blocks = [b for b in tab.get("blocks") or [] if b.get("type") in POINTS_PILES]
        if not blocks:
            continue
        for b in blocks:
            for key in ("title", "body"):
                if b.get(key) and b[key] != (PILE_TITLES[b["type"]] if key == "title"
                                             else None):
                    print(f"[review] content.json's {b['type']} {key} "
                          f"({re.sub('<[^>]+>', '', str(b[key]))[:50]!r}) is ignored — "
                          "the Review tab's headings are the builder's", file=sys.stderr)
                b.pop(key, None)
        tab["tip"] = REVIEW_TAB_TIP


def points_note_band(points: dict | None, repo: str | None = None) -> str:
    """The file's takeover note, as one amber row above the piles it qualifies:
    `▸ 32 commits made after the reviewed version (ce56d912)`, which unfolds into the
    commits themselves, each hash a link to it on github.com.

    Amber, like the aftermath band for generated-only drift, because it is the same kind
    of statement: the piles below describe the branch at an earlier commit, and here is
    what was folded in since without anyone re-reading them. The note's paragraph of
    reasons is not on the page any more — a reader needs the count and the commit the
    count starts from, and the heading (who decided, when) rides in the hover. The
    reviewed commit is a link too, so "what came after it" is one click into the branch's
    history on GitHub. A note with no commit list to fold stays the prose it was."""
    note = (points or {}).get("note")
    if not note:
        return ""
    body = note.get("html", "")
    commits = re.findall(r"<li>\s*([0-9a-f]{7,40})\s+(.*?)</li>", body, flags=re.S)
    reviewed = re.search(r"<code>([0-9a-f]{7,40})</code>", body)
    if not commits or not reviewed:
        return (f'<div class="rband rband-warn" role="status">'
                f'<p><b>{html.escape(note.get("heading", ""))}</b></p>'
                f'<div class="rb-sub">{_fold_note_lists(body)}</div></div>')

    def sha(h: str) -> str:
        code = f"<code>{h}</code>"
        return (f'<a href="{repo}/commit/{h}" target="_blank" rel="noopener">{code}</a>'
                if repo else code)
    n = len(commits)
    rows = "".join(f"<li>{sha(h)} {subject}</li>" for h, subject in commits)
    return (f'<div class="rband rband-warn" role="status">'
            f'<details class="takeover" title="{html.escape(note.get("heading", ""))}">'
            f'<summary><b>{n} commit{"s" if n != 1 else ""}</b> made after the reviewed '
            f'version ({sha(reviewed.group(1))})</summary><ul>{rows}</ul></details></div>')


def aftermath_reads_takeover(out_dir: Path) -> bool:
    """Whether the aftermath band already carries the takeover, read off `git`.

    When it does, `points_note_band` stands down: its list is the one an agent typed into
    the note, counted from the same commit, and two lists of the same commits a screen
    apart — one frozen, one live — was exactly the confusion. The note band stays the
    fallback for a page with no aftermath measurement."""
    try:
        doc = json.loads((out_dir / AFTERMATH_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and isinstance(doc.get("takeover"), dict)


def _fold_note_lists(body: str) -> str:
    """Each list in the note folded to one row: the sentence is what the reader needs,
    the thirty commit subjects are there to check, not to read before the piles."""
    def fold(m: re.Match) -> str:
        n = m.group(0).count("<li>")
        return (f'<details class="toolcommits"><summary>{n} commit{"s" if n != 1 else ""}'
                f'</summary>{m.group(0)}</details>')
    return re.sub(r"<ul>.*?</ul>", fold, body, flags=re.S)


#: The band that goes where the piles would have been. Not `render_findings([])`'s
#: "Nothing outstanding — the automated passes came back clean", which is the confident-
#: wrong page: no file is not a clean review, it is no review recorded.
POINTS_MISSING_BAND = (
    '<div class="rband rband-none" role="status">'
    '<p>No <code>review-points.md</code> on this branch — nothing records what was '
    'reviewed or declined.</p>'
    '<p class="rb-sub">The piles below are empty because the record is absent, not '
    'because the review was clean. <code>/record-review</code> is what writes the '
    'file; a branch it never ran on has nothing to read.</p></div>')


def points_empty_html(kind: str, points: dict) -> str:
    """The sentence an empty pile gets when the pile is `review-points.md`'s to fill.

    Three different silences, and the reader has to be able to tell them apart: the file is
    not there, the file has no such section, or the section is there and empty. Only the
    third is news about the review; the first two are news about the record. The renderers
    are left alone — their empty states are about a *content file* that listed nothing,
    which is a fourth thing again — so the substitution happens here, at the block.
    """
    if points.get("missing"):
        return {
            "findings": '<p class="sub">Not recorded — with no <code>review-points.md</code>'
                        ' on this branch, nothing says which findings were read and '
                        'declined.</p>',
            "autofixes": '<p class="sub">Not recorded — with no <code>review-points.md</code>'
                         ' on this branch, nothing says which findings were accepted and '
                         'fixed.</p>',
        }.get(kind, "")
    heading = POINTS_PILES.get(kind, kind)
    src = html.escape(points.get("source") or "review-points.md")
    if heading not in (points.get("sections") or {}):
        return (f'<p class="sub"><code>{src}</code> has no <b>{html.escape(heading)}</b> '
                f'section, so nothing on this branch records this pile either way.</p>')
    return {
        "findings": f'<p class="sub"><code>{src}</code> declines nothing — every finding '
                    'the review raised was accepted.</p>',
        "autofixes": f'<p class="sub"><code>{src}</code> records no fix — the review '
                     'raised nothing the agent accepted.</p>',
        "assumptions": f'<p class="sub"><code>{src}</code> records no assumption: the '
                       'agent was asked what it had to decide for itself, and named '
                       'nothing.</p>',
    }.get(kind, "")


# Three piles, three lists, each numbered from 1: what the reviewer has to judge
# (findings), what is already done (applied fixes), what only they can answer
# (assumptions). They were one list numbered straight through, on the theory that "how much
# is there for me here?" wants one answer — but the counts line already gives that answer
# per pile, and a pile opening on 7 under "3 auto-fixed" made the reader hunt for the six
# that were not there. What separates the piles is their heading, the card's colour and one
# badge.
#
# The counter below no longer numbers anything; it only records how many items have been
# rendered so far, which is how the counts line knows it is at the top of the list. It is
# module state rather than a number threaded through `render_block`, which renders blocks
# one at a time by type and has no notion that three of them sit together.
_LIST_OFFSET = 0

#: Whether the counts line has already been printed on this page. The offset used to
#: answer that question on its own — it is zero exactly until the first pile renders an
#: `<ol>` — but a pile with no items renders no list and leaves it at zero, so all three
#: piles got the line. With one empty pile that was a repeated sentence; with all three
#: empty (a branch carrying no `review-points.md`, which is the common case for a branch
#: nobody ran the flow on) it was the line three times down one short tab.
_LEDE_SHOWN = False


def reset_list() -> None:
    """Start the numbering over, once per page.

    The offset is module state, so without this the second page built in one process
    continues the first one's numbering — which no build does, and every test that renders
    a pile directly would otherwise have to know about."""
    global _LIST_OFFSET, _LEDE_SHOWN
    _LIST_OFFSET = 0
    _LEDE_SHOWN = False


def _open_list(n: int) -> str:
    """The `<ol>` for the next pile, numbered from 1.

    The piles used to be one list numbered straight through, so "Auto-fixed" opened on 7
    under a counts line that said *3 auto-fixed* — and the reader's first question was
    where the other six had gone. Each pile is its own section with its own heading and its
    own count, so each counts from 1 and its last number is the count above it. The offset
    still advances: it is how `review_lede` knows the top of the list has been printed."""
    global _LIST_OFFSET
    _LIST_OFFSET += n
    return '<ol class="findings">'


#: Where a reader can go to find out what a pass actually does. Only the two commands this
#: skill runs are in it, because those are the two it stamps — a source it does not
#: recognise (`assumption`, a human name, a linter) renders as the plain stamp it always
#: was rather than being sent somewhere that does not describe it.
PASS_DOCS = {
    "/code-review": "https://code.claude.com/docs/en/code-review#review-a-diff-locally",
    "/simplify": "https://code.claude.com/docs/en/commands#all-commands",
}


def _finding_source(f) -> str:
    """Which pass raised it, when the content file says so.

    Optional by design: an item that does not claim a source renders without one rather
    than being attributed to a guess. Provenance exists only where the decision was made,
    which is why it now arrives from the branch: `review-points.md`'s `source:` field is
    written by the agent that read the finding and accepted or declined it, in the session
    where the pass that raised it was the one running.

    The assumptions pile does not come through here at all: its provenance never varies,
    so it is the card's one purple chip rather than a grey stamp behind a second badge.

    A stamp naming a documented pass is the link to that documentation. The stamp already
    is the question — *what is `/code-review`?* — and answering it in place costs the page
    nothing, where answering it in prose costs a line under the verdict that every reader
    who already knows has to read past.

    `review-points.md`'s `source:` is one field carrying up to three facts the parser
    never splits apart — the pass, the effort it filed the item at, and which of several
    same-titled findings this one is (`/code-review high (the PUT-clears-the-vet
    scenario)`) — because splitting them is a rendering decision, not a parsing one, and
    `review-points.py` is not this tab's to change. Only the pass name is the link: effort
    is a fact for the tooltip, not the face, and the parenthesised detail is prose that
    happens to follow a chip, not part of the chip itself."""
    src = (f.get("source") or "").strip()
    if not src:
        return ""
    # A known pass may be followed by its effort and a parenthesised detail; an unknown
    # source (`assumption`, a human name, a linter) never matches and falls straight to
    # the plain stamp below, whatever it says after the first word.
    name = next((n for n in PASS_DOCS if src == n or src.startswith(n + " ")), None)
    if not name:
        return f'<span class="f-src">{html.escape(src)}</span>'
    rest = src[len(name):].strip()
    detail_m = re.search(r"\(([^()]*)\)\s*$", rest)
    detail = detail_m.group(1).strip() if detail_m else ""
    effort = (rest[:detail_m.start()] if detail_m else rest).strip()
    tip = f"What {name} does, in the Claude Code docs"
    if effort:
        tip += f" — filed at {effort} effort"
    # The face is the command, which says nothing about where the link goes or how hard
    # the pass looked; the tooltip spends itself on the first, as every other tooltip on
    # this page does, and the detail after the chip spends itself on the second, in plain
    # text rather than crowded into a face that is also a link.
    chip = (f'<a class="f-src" href="{html.escape(PASS_DOCS[name])}" target="_blank" '
            f'rel="noopener" data-tip="{html.escape(tip)}">{html.escape(name)}</a>')
    return chip + (f' <span class="f-src-detail">({html.escape(detail)})</span>' if detail else "")


def _raised_by(items, total: int) -> str:
    """`12 raised — 9 by /code-review, 3 by /simplify`: the review chip's hover.

    Counted off each item's own `source`, the same string the stamp beside it renders, so
    a reader who hovers the chip and then counts the stamps gets the same answer twice.
    Passes appear in the order the content file first mentions them.

    `source` is optional (see `_finding_source`), and an item without one is counted as
    itself rather than folded into whichever pass happens to be first — an unattributed
    finding is a real state, and a hover that hides it is a hover that lies by rounding.
    With nothing attributed at all the breakdown is dropped entirely: `12 raised, 12 of
    them unattributed` is the total said twice."""
    counts: dict[str, int] = {}
    for it in items:
        src = (it.get("source") or "").strip()
        counts[src] = counts.get(src, 0) + 1
    named = [f"{n} by {src}" for src, n in counts.items() if src]
    if not named:
        return f"{total} raised"
    if counts.get(""):
        named.append(f'{counts[""]} with no pass named')
    return f"{total} raised — " + ", ".join(named)


def _finding_refs(f) -> str:
    """The bare `file:line` links — only when nothing else already carries them.

    An item that shows a snippet or a diff already links the file, with a line RANGE, from
    that block's own header. Repeating a bare `file:line` link above it says the same thing
    twice and worse."""
    if f.get("_snippets") or f.get("_diffs") or f.get("_fixDiffs"):
        return ""
    return "".join(_ref_link(r) for r in f.get("_refs", []))


def _ref_link(r) -> str:
    """`VetRestController.java:96-100`, with the path it came from on hover.

    The same trade a diff header makes: a repo-relative Java path spends five segments on
    module, `src/main/java` and the org package before it reaches the one word that says
    which file this is, and a line of three such references is a wall no reader parses.
    The path is not dropped, it is moved to the tooltip — and a file at the repo root has
    no path to move, so it gets no tooltip repeating its own name."""
    label = r["label"]
    rel, _, lines = label.rpartition(":")
    name = f"{Path(rel).name}:{lines}" if rel else label
    tip = f' data-tip="{html.escape(rel)}"' if "/" in rel else ""
    return (f'<a class="srcref" href="vscode://file/{r["abs"]}"{tip}>'
            f'{html.escape(name)}</a> ')


def _assumptions_block(spec):
    """The `assumptions` block as the layout declared it, or None if the page declares none.

    The lede counts the coder's pile even when it is empty, and the only sentence it can
    honestly print about an empty one depends on the mode — so it has to find the block
    itself, not infer the pile from the items that happen to be in it."""
    for t in spec.get("tabs") or []:
        for b in t.get("blocks", []):
            if b.get("type") == "assumptions":
                return b
    return None


def _pile_anchor(spec, kind, fallback):
    """The id the pile's own heading will carry, or None when no such block is laid out.

    Read from the layout rather than assumed: a block may name itself, and a lede whose
    links point at ids no heading has is worse than a lede with no links at all."""
    for t in spec.get("tabs") or []:
        for b in t.get("blocks", []):
            if b.get("type") == kind:
                return b.get("id", fallback)
    return None


def pile_numbers(spec) -> tuple[int, int, int]:
    """`(open, fixed, assumed)` — the three counts every summary of this tab reads off the
    same three arrays, so a number cannot drift between the sticky line under the header
    and the masthead's review chip above it — including the third, which the chip prints
    as the coder's `N assumptions` and the line as `N implementation assumptions`. It used
    to be the other way round: a hand-typed `/code-review 8 findings` outlived the ninth
    finding being added, and nothing caught it, because nothing was looking. Both callers
    now count nothing twice."""
    return (len(spec.get("findings", [])), len(spec.get("autofixes", [])),
            len(spec.get("assumptions", [])))


def review_tab_badge(spec) -> dict:
    """What the number on the **Review** pill counts, and what it says it counts.

    It used to count nothing a reader could find. The pill said **10** while the sticky
    row under it said `6 open · 3 auto-fixed · 7 assumptions` and the masthead said
    `9 raised` — because the badge fell back to the tab's render *weight*, which is the
    "is there anything at all to show here" number every tab is dropped or kept by. On
    this tab that weight is findings + auto-fixes + one: the assumptions pile weighs a
    fixed 1 whatever is in it, so that its "which kind of empty this is" sentence keeps
    the tab alive. A layout sentinel was being read as a count of something, and it even
    collided with the `6 /10` score chip beside it — 10 read as the score's denominator.

    The tab's own comment says a number on a tab is a promise that it means something. The
    one number on this tab that a reader can point at is the open pile, which is what the
    header chip leads with (`6 open, 3 auto-fixed`) and what the sticky row's first clause
    counts — so it is that, off `pile_numbers`, the same arrays both of those read. The
    other two piles are not hidden by leaving them out: they are one line down, and they
    are work already dealt with, which is exactly what a pill on a tab should not be
    adding to a count of what is left to do.

    And it carries its own hover, because a bare number beside a score chip is a number a
    reader has to guess at.
    """
    open_n, fixed_n, assumed_n = pile_numbers(spec)
    rest = " and ".join(x for x in (
        f"the {fixed_n} auto-fixed" if fixed_n else "",
        f'the {assumed_n} assumption{"" if assumed_n == 1 else "s"}' if assumed_n else "",
    ) if x)
    label = f'{open_n} open review issue{"" if open_n == 1 else "s"}'
    if rest:
        label += f". {rest[:1].upper()}{rest[1:]} are further down the tab, already dealt " \
                 "with, and this number leaves them out"
    return {"count": open_n, "label": label}


#: Kept only because `build-review-html.py` imports it by name. It used to be the width
#: at which the chip's third number stopped fitting and was cut whole — back when that
#: number rode along as `, N assumptions` on the same clause as the fixes. The third pile
#: is no longer an optional extra on the review's own sentence: it is a second sentence,
#: about a different agent, and a chip that drops it for want of two characters drops the
#: only trace on the masthead of what the coder guessed at. Nothing measures the face
#: against this any more.
SCOPE_CHIP_MAX_LEN = 34


def scope_chip_face(spec, reviewer: str | None = None) -> str:
    """`\U0001f916Code: <b>7 unsure</b>; \U0001f916Review: <b>6 open</b>, <b>3 fixed</b>`
    — the masthead's review chip, whole, off the same `pile_numbers` the counts line
    under the header reads, so the two cannot drift.

    Two agents, two clauses, one typography: each clause is `Agent: counts`, the coder's
    first because its assumptions were made before the review ran. `unsure` is the
    coder's own pile in one word — what it had to guess at — where the counts line has
    the width for `implementation assumptions`. Every count is bold with what it counts.
    With no assumptions the coder's clause is absent rather than zeroed. The reviewer's
    model is not on the face (`reviewer` is accepted and ignored here): the pill names
    the role, the hover names the model."""
    open_n, fixed_n, assumed_n = pile_numbers(spec)
    face = f"\U0001f916Review: <b>{open_n} open</b>, <b>{fixed_n} fixed</b>"
    if assumed_n:
        face = f"\U0001f916Code: <b>{assumed_n} unsure</b>; " + face
    return face


#: What lights up the counts line as the reader scrolls past the chapter each clause
#: names — the alternative to freezing the section heading itself, which is the one this
#: page settled on: the line already sits still (it is sticky), so it is the line that
#: gains a mark rather than a second element competing for the same job.
#:
#: An inline script, not a file added to `hrbuild/assets/` and wired through
#: `shared/assets.py`: that pipeline, and the script list it feeds `build-review-html.py`,
#: is `shared/`'s to change, and two other agents were mid-edit in exactly those files
#: while this was written. A paragraph-sized behaviour that belongs to one tab's one
#: paragraph is safer self-contained than borrowed into a home somebody else is using.
#:
#: The trigger line is read off the row's own resolved `top` (the masthead's height,
#: however `--strip-h` is currently expressed) plus its own `offsetHeight` — not a second
#: copy of those numbers, so the mark cannot land a pile-width off from where the row is
#: actually pinned. Recomputed on resize, because the strip wraps to a second row exactly
#: when the tab count or the viewport does. No `IntersectionObserver`, no mark: the links
#: stay exactly as clickable as they were before this existed.
#:
#: Watching the three headings themselves, directly, was the first attempt and it read
#: wrong: a heading is one line tall, so `isIntersecting` on it alone is true only while
#: it is crossing the trigger, which is the top of a scroll through a section a thousand
#: pixels long and false for the rest of the read — the mark would go dark the moment a
#: reader actually started reading. `IntersectionObserver` still drives it (it wakes the
#: check only when a heading nears the line, never on every scroll frame), but what
#: decides the mark is a position check across all three: whichever heading is the last
#: one to have scrolled above the trigger is the section the reader is standing in, and
#: that stays true for as long as the next heading has not arrived.
PILELEDE_SPY_JS = """<script>(function(){
// The row prints above its own tab's first heading (`_lede_above` puts the lede before
// the head, on purpose — the line describes the whole list, not the pile under it), so
// this script's own tag sits in the document *before* `#first`/`#fixed`/
// `#assumed` have been parsed. Read at the top level, `getElementById` on any of them
// returns null every time, `pairs` comes up empty, and the whole thing silently no-ops
// — the bug this file shipped with once already. Deferred to `DOMContentLoaded` (or run
// immediately if that has already fired, for a script that lands after it), the same
// three ids exist wherever else on the page they are.
function whenReady(fn){
  if(document.readyState==='loading'){
    document.addEventListener('DOMContentLoaded', fn);
  }else{
    fn();
  }
}
whenReady(function(){
var lede=document.querySelector('.pilelede');
if(!lede||!('IntersectionObserver' in window))return;
var links={};
lede.querySelectorAll('a[href^="#"]').forEach(function(a){
  links[a.getAttribute('href').slice(1)]=a;
});
var ids=Object.keys(links);
var pairs=ids.map(function(id){return {id:id, el:document.getElementById(id)};})
  .filter(function(p){return p.el;});
if(!pairs.length)return;
var io=null;
function triggerY(){
  return (parseFloat(getComputedStyle(lede).top)||0)+lede.offsetHeight;
}
// A heading counts as reached once its top is at the trigger line -- or at its own
// `scroll-margin-top`, whichever is lower on the page. The two are not the same line: a
// deep link (`#fixed`, from the row itself) parks the heading exactly at its scroll
// margin, which the stylesheet sets to strip + lede + .6rem so the heading clears the
// pinned row, and that .6rem left it just *under* the trigger. The mark then stayed on
// the previous chapter after a click on this one, which is the one moment a reader is
// certain which chapter they asked for.
//
// Before any heading has got there -- the page as it opens, scrolled to the top -- the
// first chapter is the one lit, not none. The reader is about to read it; a row with no
// mark at all read as broken until the first scroll lit it up.
function paint(){
  var t=triggerY(), current=null, first=null, firstTop=Infinity;
  pairs.forEach(function(p){
    var margin=parseFloat(getComputedStyle(p.el).scrollMarginTop)||0;
    var top=p.el.getBoundingClientRect().top;
    if(top<firstTop){firstTop=top;first=p.id;}
    if(top<=Math.max(t,margin)+1)current=p.id;
  });
  if(!current)current=first;
  ids.forEach(function(id){links[id].classList.toggle('here', id===current);});
}
function setup(){
  if(io)io.disconnect();
  var t=Math.max(triggerY(),0);
  // A band, not a line. A 1-2px trigger line is exact on paper and wrong in a browser:
  // `IntersectionObserver` only reports what it sampled on a rendered frame, and a fast
  // flick of the wheel can move a heading clean across two pixels between one frame and
  // the next without either frame catching it mid-crossing — the mark then never wakes
  // up and stays lit on whatever section it last saw. A few hundred pixels of band is
  // cheap to observe and near-impossible for an ordinary scroll to jump over unseen.
  var band=Math.min(300, Math.max(window.innerHeight-t-40, 40));
  var bottom=Math.max(window.innerHeight-t-band,0);
  io=new IntersectionObserver(paint,
    {rootMargin:'-'+t+'px 0px -'+bottom+'px 0px', threshold:0});
  pairs.forEach(function(p){io.observe(p.el);});
  paint();
}
setup();
window.addEventListener('resize',setup);
// A belt beside the band's braces: once scrolling actually stops, `paint()` runs once
// more off the headings' real positions regardless of whether the band caught every
// frame in between, so the mark is never left stuck on a section the reader scrolled
// straight past. Unknown to a browser (`scrollend` is recent), `addEventListener`
// silently ignores the event name and the band above is what carries the behaviour.
window.addEventListener('scrollend', paint, {passive:true});
});
})();</script>"""


#: Where a short clause ends inside a verdict bullet: the first stop, colon, semicolon,
#: comma or dash. The verdict's bullets are paragraphs written for the band that used to
#: sit under the score; their first clause is the claim, the rest is the evidence for it.
_CLAUSE_END = re.compile(r"(?:[.:;,]\s|\s[\u2014\u2013-]\s|[.:;]$)")


def _first_clause(text: str) -> str:
    """`No build proved this commit` out of `No build proved this commit: <code>…</code>
    failed for … .` — tags stripped, entities kept, cut at the first clause boundary."""
    plain = html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()
    m = _CLAUSE_END.search(plain)
    return (plain[:m.start()] if m else plain).strip().rstrip(".")


#: Where `preflight.py` leaves the CI gate's verdict on the commit under review.
GATE_JSON = ".gate.json"
#: The API tab's verdict band, as `openapi-compat.py` wrote it.
API_VERDICT_HTML = "assets/openapi-verdict.html"
#: The two producers that leave a verdict when their tab could not be measured this run.
SEQUENCE_VERDICT_JSON = "assets/sequence.verdict.json"
FILM_VERDICT_JSON = "assets/feature.verdict.json"
#: How many lines of its own the content file's `verdict` may add under the computed ones.
MODEL_GRADE_LINES = 2

#: The highest grade each signal allows. A grade is the model's number, lowered to the
#: lowest ceiling any signal on the page sets — never raised. One table, so the reader can
#: be told in one hover why an 8 became a 6, and a reviewer can argue with a row of it.
GRADE_CAPS = {
    "ci-failed": 4,         # CI ran on the reviewed commit and failed
    "ci-unproven": 6,       # no CI run proved the reviewed commit (skipped, cancelled, none)
    "open-high": 5,         # an open review issue the reviewer filed as `high`
    "api-breaking": 7,      # the REST contract breaks a client
    "no-evidence": 7,       # one tab carries a reason instead of this run's evidence
    "no-evidence-2": 6,     # two or more do
    "after-review": 7,      # code moved on the branch after the review was recorded
    "out-of-range": 7,      # commits in the PR that the review never read
}


def _plain_text(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _git_out(root: Path | None, *args: str) -> str | None:
    """`git <args>` in `root`, stdout stripped — or None when it fails or there is no root."""
    if root is None:
        return None
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _git_root(out_dir: Path) -> Path | None:
    top = _git_out(Path(out_dir), "rev-parse", "--show-toplevel") if Path(out_dir).is_dir() \
        else None
    return Path(top) if top else None


def _signal(key: str, short: str, full: str, cap: int | None = None) -> dict:
    return {"key": key, "short": short, "full": full, "cap": cap}


def _ci_signal(out_dir: Path) -> dict | None:
    """What CI said about the reviewed commit — green or not, either way on the panel.

    Silence about CI read as "nothing to worry about" on a page whose CI was green, and as
    the same on a page whose CI never ran. No `.gate.json` at all (a page from before the
    gate, or a test) says nothing rather than guessing."""
    gate = _read_json(Path(out_dir) / GATE_JSON)
    if not isinstance(gate, dict) or not gate.get("verdict"):
        return None
    sha = str(gate.get("sha") or "")[:8]
    caveat = _plain_text(str(gate.get("caveat") or ""))
    runs = [w for w in gate.get("workflows") or [] if isinstance(w, dict)]
    if gate["verdict"] == "green":
        run = next((w for w in runs if w.get("runId")), {})
        return _signal("ci-green", f"CI green on {sha}" if sha else "CI green",
                       caveat or f"{run.get('name', 'CI')} run {run.get('runId', '')} passed")
    if any(w.get("verdict") == "failure" for w in runs) or gate["verdict"] == "failure":
        return _signal("ci-failed", f"CI failed on {sha}" if sha else "CI failed",
                       caveat or "a CI workflow concluded failure on the reviewed commit",
                       GRADE_CAPS["ci-failed"])
    return _signal("ci-unproven", "No build proved this commit",
                   caveat or f"the CI gate says {gate['verdict']!r}, not green",
                   GRADE_CAPS["ci-unproven"])


def _api_signal(out_dir: Path) -> dict | None:
    """The API tab's own verdict band, read rather than recomputed: red means breaking."""
    try:
        band = (Path(out_dir) / API_VERDICT_HTML).read_text(encoding="utf-8")
    except OSError:
        return None
    if 'class="apiverdict red"' not in band:
        return None
    text = _plain_text(re.sub(r"<style>.*?</style>", "", band, flags=re.S))
    text = re.sub(r"\s+", " ", re.sub(r"\(report\s*\u2197\)", "", text)).strip()
    m = re.search(r"(\d+)\s+breaking", text)
    n = int(m.group(1)) if m else 0
    short = (f"{n} breaking API change{'' if n == 1 else 's'}" if n
             else "The API contract breaks")
    return _signal("api-breaking", short, text or short, GRADE_CAPS["api-breaking"])


def _evidence_signal(spec, out_dir: Path) -> dict | None:
    """The tabs that carry a reason instead of evidence from this run, as one line.

    Read off the verdicts their producers leave — the Sequence tab's when the traced
    suites drew nothing or were red, the Demo tab's when the film did not complete. The
    C2 view on Structure is projected from the sequence diagrams, so a Sequence tab that
    was not re-traced takes it along."""
    names = []
    detail = []
    seq = _read_json(Path(out_dir) / SEQUENCE_VERDICT_JSON)
    if isinstance(seq, dict) and seq.get("state") in ("skipped", "red"):
        what = ("not re-traced on this run" if seq["state"] == "skipped"
                else "the traced suite was red")
        names.append("Sequence")
        c2 = (Path(out_dir) / "assets" / "c2").is_dir()
        detail.append(f"Sequence: {what}"
                      + (" — and the C2 view on Structure is drawn from those same "
                         "diagrams" if c2 else ""))
    film = _read_json(Path(out_dir) / FILM_VERDICT_JSON)
    if isinstance(film, dict) and film.get("exit"):
        names.append("Demo")
        detail.append("Demo: " + {3: "the feature did not hold on film",
                                  2: "nothing was filmed"}.get(
            film.get("exit"), f"the recorder failed (exit {film.get('exit')})"))
    if not names:
        return None
    n = len(names)
    short = (f"{'One tab carries' if n == 1 else f'{n} tabs carry'} a reason instead of "
             f"evidence: {', '.join(names)}")
    return _signal("no-evidence", short, "; ".join(detail),
                   GRADE_CAPS["no-evidence" if n == 1 else "no-evidence-2"])


def _base_ref(spec, root: Path | None) -> str | None:
    """The branch the PR merges into, as a ref this clone can resolve."""
    base = str((spec.get("pr") or {}).get("base") or "main")
    for ref in (base if base.startswith("origin/") else f"origin/{base}", base):
        if _git_out(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
            return ref
    return None


def _after_review_signal(out_dir: Path, root: Path | None, base_ref: str | None) -> dict | None:
    """Commits that moved code after the review was recorded — the aftermath band's
    count, minus what that band folds away as tooling from the base and merge seams."""
    doc = _read_json(Path(out_dir) / AFTERMATH_JSON)
    if not isinstance(doc, dict):
        return None
    commits = [c for c in doc.get("commits") or []
               if isinstance(c, dict) and not c.get("takeover") and not c.get("taken_over")
               and any(not f.get("generated") for f in c.get("files") or [])]
    shas = [c["sha"] for c in commits if c.get("sha")]
    drop = _merge_seam_shas(root, shas) if root else set()
    drop |= _tooling_commit_shas(root, base_ref, [s for s in shas if s not in drop]) \
        if root else set()
    commits = [c for c in commits if c.get("sha") not in drop]
    if not commits:
        return None
    n = len(commits)
    return _signal("after-review",
                   f"{n} commit{'' if n == 1 else 's'} landed after the review",
                   "Code moved after review-points.md was recorded, and the piles have not "
                   "seen it: " + "; ".join(f"{c.get('short', '')} {c.get('subject', '')}"
                                           for c in commits[:4]),
                   GRADE_CAPS["after-review"])


def _out_of_range_signal(spec, root: Path | None, base_ref: str | None) -> dict | None:
    """Commits the PR carries that sit before the range the reviewers read.

    The review reads `audited-base..implementation`; the PR is everything since it left
    its base. When the review started later than that, the commits in between are in the
    diff a merge would ship, and nobody read them."""
    prov = ((spec.get("_reviewPoints") or {}).get("provenance") or {})
    audited = prov.get("auditedBase") or prov.get("base")
    if not (root and base_ref and audited):
        return None
    mb = _git_out(root, "merge-base", base_ref, "HEAD")
    if not mb or not _git_out(root, "merge-base", "--is-ancestor", mb, audited) == "":
        return None
    listed = _git_out(root, "log", "--no-merges", "--format=%h %s", f"{mb}..{audited}")
    rows = [line for line in (listed or "").splitlines() if line.strip()]
    if not rows:
        return None
    n = len(rows)
    return _signal("out-of-range",
                   f"{n} commit{'' if n == 1 else 's'} in the PR before the reviewed range",
                   f"The reviewers read {audited[:8]}..{str(prov.get('auditedHead', 'HEAD'))[:8]}"
                   f"; these sit between {base_ref} and that range and were never reviewed: "
                   + "; ".join(rows[:5]) + (" …" if n > 5 else ""),
                   GRADE_CAPS["out-of-range"])


#: The signals `_pile_signals` produces, which `grade_reasons` recounts at render time.
PILE_SIGNALS = ("open", "open-high", "assumptions")


def _pile_signals(spec) -> list[dict]:
    """The open pile by severity and the unconfirmed assumptions — the two signals a spec
    carries on its own, with no build around it."""
    out = []
    findings = [f for f in spec.get("findings") or [] if isinstance(f, dict)] \
        if isinstance(spec.get("findings"), list) else []
    if findings:
        by: dict[str, int] = {}
        for f in findings:
            by[f.get("severity", "info")] = by.get(f.get("severity", "info"), 0) + 1
        split = ", ".join(f"{by[k]} {SEVERITIES[k][1]}{'s' if k == 'low' and by[k] > 1 else ''}"
                          for k in ("high", "medium", "low", "info") if by.get(k))
        n = len(findings)
        out.append(_signal(
            "open-high" if by.get("high") else "open",
            f"{n} open review issue{'' if n == 1 else 's'}: {split}",
            "; ".join(_plain_text(f.get("title", "")) for f in findings
                      if f.get("severity") in ("high", "medium")) or "the open pile below",
            GRADE_CAPS["open-high"] if by.get("high") else None))
    assumed = [a for a in spec.get("assumptions") or [] if isinstance(a, dict)] \
        if isinstance(spec.get("assumptions"), list) else []
    if assumed:
        unsure = sum(1 for a in assumed
                     if isinstance(a.get("confidence"), (int, float)) and a["confidence"] < .7)
        n = len(assumed)
        out.append(_signal(
            "assumptions",
            f"{n} implementation assumption{'' if n == 1 else 's'} unconfirmed"
            + (f", {unsure} under 70% sure" if unsure else ""),
            "What the coder guessed at and nobody confirmed — the last pile below"))
    return out


def grade_signals(spec, out_dir: Path, root: Path | None = None) -> list[dict]:
    """What the page measured that bears on the grade, one dict per reason, in the order
    the panel prints them — and, as `spec["_gradeSignals"]`, what `grade_reasons` reads.

    Every reason is computed: CI, the open pile by severity, the unconfirmed assumptions,
    a breaking API change, tabs left without evidence, code that moved after the review,
    commits the review never read. A content file's verdict adds at most
    `MODEL_GRADE_LINES` lines under these (`grade_reasons`), and its number is lowered to
    the lowest ceiling the signals set (`cap_grade`)."""
    root = root if root is not None else _git_root(out_dir)
    base_ref = _base_ref(spec, root) if root else None
    out = []
    ci = _ci_signal(out_dir)
    if ci:
        out.append(ci)
    out.extend(_pile_signals(spec))
    for sig in (_api_signal(out_dir), _evidence_signal(spec, out_dir),
                _after_review_signal(out_dir, root, base_ref),
                _out_of_range_signal(spec, root, base_ref)):
        if sig:
            out.append(sig)
    spec["_gradeSignals"] = out
    return out


def cap_grade(spec) -> int | None:
    """Lower `verdict.score` to the lowest ceiling a computed signal sets, in place.

    The model's own number is kept as `verdict.modelScore` so the panel can say what it was
    and which signals brought it down. Never raises a grade: the ceilings say how good a
    page with this evidence can be, not how good it is. Returns the ceiling that bound, or
    None when the model's number stands."""
    v = spec.get("verdict")
    if not isinstance(v, dict) or "score" not in v:
        return None
    caps = [s["cap"] for s in spec.get("_gradeSignals") or [] if s.get("cap")]
    if not caps:
        return None
    model = int(v.get("modelScore", v["score"]))
    ceiling = min(caps)
    if ceiling >= model:
        return None
    v["modelScore"] = model
    v["score"] = ceiling
    return ceiling


def grade_reasons(spec) -> list[tuple[str, str]]:
    """`[(short, full), …]` — why the score is what it is, in a few words each.

    The computed signals first (`grade_signals`, or — for a spec that never went through
    it — the two piles counted here), then at most `MODEL_GRADE_LINES` of the content
    file's own: `verdict.why` if it has one, else `verdict.bullets`, each cut to its first
    clause with the whole kept for the hover. A model used to write the whole list, and
    run 5 shipped a green 8/10 whose two reasons were counts — nothing about the breaking
    API change, the tab nobody re-traced, or the CI run. The page states what it measured;
    the model gets two lines for what it alone knows."""
    v = spec.get("verdict") or {}
    # The piles are counted here, at render time, not off the list the build computed:
    # the build drops unanchored assumptions after the signals were taken, and the panel
    # has to count the pile the reader sees under it.
    measured = [s for s in spec.get("_gradeSignals") or [] if s["key"] not in PILE_SIGNALS]
    signals = ([s for s in measured if s["key"].startswith("ci-")] + _pile_signals(spec)
               + [s for s in measured if not s["key"].startswith("ci-")])
    model_score = v.get("modelScore")
    out = []
    for s in signals:
        short = s["short"]
        if s.get("cap") and model_score is not None and s["cap"] < model_score:
            short += f" (caps the grade at {s['cap']})"
        out.append((short, s.get("full") or short))
    own = list(v.get("why") or v.get("bullets") or [])
    if len(own) > MODEL_GRADE_LINES:
        print(f"[review] verdict carries {len(own)} lines of its own; the grade panel shows "
              f"the first {MODEL_GRADE_LINES} — the rest of its reasons are computed",
              file=sys.stderr)
    for b in own[:MODEL_GRADE_LINES]:
        # A `why` line is written to be short and kept whole unless it runs long; a
        # `bullet` is a paragraph, and its first clause is the claim.
        short = (_first_clause(b) if not v.get("why") or len(_plain_text(b)) > 80
                 else _plain_text(b))
        if short:
            out.append((short, _plain_text(b)))
    return out


def grade_reasons_html(spec) -> str:
    """The panel above the three piles that the score in the masthead links to: a bullet
    per reason on the left, and the grade itself, large, in the right-hand space the short
    bullets leave empty. Empty when there is no verdict.

    The grade was a small `Why graded 6/10` heading over the bullets, which made the
    number the least visible thing in a panel that exists to explain it, and left half the
    panel blank beside a column of five-word lines. Now the bullets start at the top and
    the number sits beside them, where the eye lands after reading them. When a signal
    lowered the model's number, the panel says from what (`was 8`)."""
    v = spec.get("verdict")
    if not v or "score" not in v:
        return ""
    reasons = grade_reasons(spec)
    if not reasons:
        return ""
    n = int(v["score"])
    band = "v-good" if n >= 8 else ("v-mid" if n >= 5 else "v-bad")
    items = "".join(
        f'<li data-tip="{html.escape(full, quote=True)}">{html.escape(short)}</li>'
        if full and full != short else f"<li>{html.escape(short)}</li>"
        for short, full in reasons)
    was = v.get("modelScore")
    capped = (f'<span class="gradewhy-was" title="The model graded it {was}/10; the '
              f'signals marked beside the reasons cap it at {n}">was {was}</span>'
              if was is not None and int(was) != n else "")
    return (f'<aside class="gradewhy {band}" id="grade-why" aria-label="Why graded {n}/10">'
            f'<ul>{items}</ul>'
            f'<p class="gradewhy-score" title="Why graded {n}/10: the reasons beside it">'
            f'<span class="gradewhy-l">graded</span>'
            f'<span class="gradewhy-n"><b>{n}</b>/10</span>{capped}</p></aside>')


def opening_lede(spec) -> str:
    """The shape of the whole list, for whichever pile opens it — and only for that one.

    Computed, and deliberately a line. What stood here was three sentences of prose
    restating the shape of the list directly beneath it ("They are one list: the nine that
    need your judgement first, then the three I applied, greyed out and numbered straight
    on"), which a reader can see. The reader is a developer who came for the findings; the
    counts are the only part of that paragraph they could not have got by looking.

    It is asked for by all three piles and answers only the first, because the piles are
    one list and their order is the content file's to choose. Pinned to `findings`, a lede
    describing three piles renders underneath one the reader has already walked past.
    `_LIST_OFFSET` is still zero exactly until the first pile renders, so the question
    "am I the top of the list?" is already answered and does not need a second flag.
    """
    global _LEDE_SHOWN
    if _LIST_OFFSET or _LEDE_SHOWN:
        return ""
    # Counts, and nothing else. Every clause that described how the list *looks* has been
    # cut — "greyed out", "yours to confirm", and finally "worst first" itself: the
    # applied fixes are visibly grey, an assumption visibly wears its purple chip, and an
    # ordering is the one thing a reader can see without being told. Each was the
    # paragraph-the-reader-can-see rule reappearing one clause at a time, inside the line
    # that replaced the paragraph.
    block = _assumptions_block(spec)
    parts = []

    def clause(text, kind, fallback):
        """A count, and the way to the pile it counts.

        The line is the first thing read in the tab and names three chapters further down
        it, so every clause is the jump to its own — the reader was going to scroll looking
        for them anyway. A pile with no block laid out keeps its count as plain text: a
        dead anchor that silently does nothing is worse than a number that never claimed
        to be clickable."""
        at = _pile_anchor(spec, kind, fallback)
        return f'<a href="#{html.escape(at)}">{text}</a>' if at else text

    # Two vocabularies, because the two sources mean different things by the same pile.
    # With the piles written into the content file, `findings` is what a review pass raised
    # and nobody has answered yet — *open*. Read out of `review-points.md`, the same array
    # is what the agent read and said no to, which is not open at all: it is closed, by the
    # agent, and the reader's job is to disagree or agree. Calling that "open" would ask
    # the reviewer to triage a decision that has already been made, and would hide the one
    # fact the file exists to carry.
    points = spec.get("_reviewPoints")
    if points:
        # Open first, same as the other vocabulary below — the two sources disagree about
        # what to *call* the undecided pile (a content file's `findings` are untriaged, a
        # branch's are declined-by-the-agent) but agree on where it goes: first, because
        # it is the one a human still owes a decision to. What was fixed without asking
        # comes next, and what nobody could be asked about is always the tail — see the
        # `block is not None` clause below.
        if spec.get("findings"):
            n_open = len(spec["findings"])
            parts.append(clause(
                f"{n_open} open review issue{'' if n_open == 1 else 's'}",
                "findings", "first"))
        if spec.get("autofixes"):
            parts.append(clause(f"{len(spec['autofixes'])} auto-fixed", "autofixes", "fixed"))
    else:
        if spec.get("findings"):
            # "open LLM review issues", not "open, worst first": the ordering fact was the
            # one clause here that a reader could not have counted themselves, and it was
            # also the one nobody acts on — the list is in front of them, worst first or
            # not. What they do act on is *who raised these*, because the page carries two
            # piles a machine produced and one a human owns, and the clause that opens the
            # line is the one that has to say which of them it is counting.
            n_open = len(spec["findings"])
            parts.append(clause(
                f"{n_open} open LLM review issue{'' if n_open == 1 else 's'}",
                "findings", "first"))
        if spec.get("autofixes"):
            # `auto-fixed`, the same word the badge on every one of those items already
            # wears. "auto-applied" was a second name for one thing, and a reader who
            # scrolls to the pile has to satisfy themselves the two words mean the same
            # before they can trust the count.
            parts.append(clause(f"{len(spec['autofixes'])} auto-fixed",
                                "autofixes", "fixed"))
    # Last, because the first two clauses count what a review pass produced and this one
    # counts what it could not: a reader who has just been told how many items are open
    # and how many were applied is at exactly the point where "and here is what nobody
    # checked" lands. Leading with it puts the softest pile in front of the defects.
    if block is not None:
        # Zero is a number the reader came for, so this clause renders at zero too. A pile
        # that appears only when it is non-empty disappears exactly where it matters most:
        # "the page says nothing about what the coder guessed at" and "the coder was asked
        # and guessed at nothing" are the same blank line, and only one of them is good
        # news. Mode C is the case where a zero would be the lie instead — nobody was in a
        # position to be asked — so it says that rather than counting an empty pile.
        assumed = len(spec.get("assumptions", []))
        # `6 implementation assumptions`, flat. `coder` named who produced them, which the
        # card's own purple `assumption` chip says where the reader is standing, and `to
        # check` named the work — in a line whose other two clauses are bare counts, so the
        # asymmetry read as a fourth fact rather than as the same shape said three times.
        # `implementation` is the one word carried over from that trimming: on a page that
        # also runs `/code-review` and `/simplify`, "assumptions" alone reads as ambiguous
        # about *whose* — this pile is what the coder assumed while implementing, not a
        # reviewer's. Mode C is still the exception: there is no count to give, only the
        # reason there is none.
        # First, since the piles read as rounds: what was assumed while coding (I) came
        # before anything the review raised (II) or fixed (III).
        parts.insert(0, clause(
            "coder could not be asked"
            if block.get("mode") == "C" and not assumed
            else f"{assumed} implementation assumption{'' if assumed == 1 else 's'}",
            "assumptions", "assumed"))
    if not parts:
        return ""
    _LEDE_SHOWN = True
    # The stamp clause went the same way as "greyed out" and "yours to confirm": every
    # item carries its source beside its own title, so a line announcing that they do
    # describes the thing directly under it. What is left is three counts and the jump to
    # each — the only part of the list that counting it yourself would not have told you.
    # `pilelede` is what the stylesheet pins: the line names three chapters that are
    # thousands of pixels apart, so it has to still be on screen when the reader is inside
    # one of them and wants the next. Sticky under the masthead, never over it.
    # The grade's reasons go above the counts line, not under it: the line is sticky and
    # has to stay the topmost thing in the tab once the reader scrolls, and the panel is
    # read once, on arrival from the score, and then left behind.
    # The takeover row ("32 commits made after the reviewed version") goes between the
    # two: under the grade, which is read first on arrival from the masthead, and directly
    # above the counts line it qualifies — every number on that line was counted at the
    # reviewed commit, not at the branch's head.
    return (grade_reasons_html(spec) + _flush_top_bands()
            + '<p class="sub counts pilelede">' + " &middot; ".join(parts)
            + push_pr_button(spec) + "</p>" + push_pr_dialog(spec) + PILELEDE_SPY_JS)


def render_findings(findings) -> str:
    if not findings:
        return '<p class="sub">Nothing outstanding \u2014 the automated passes came back clean.</p>'
    items = []
    # Worst first, whatever order the source listed them in: a pile that read "worth a
    # look, nit, worth a look" made the reader sort it in their head. Stable, so equal
    # severities keep the author's order.
    rank = {k: i for i, k in enumerate(SEVERITIES)}
    findings = sorted(findings, key=lambda f: rank.get(f.get("severity", "info"), len(rank)))
    for f in findings:
        cls, label = SEVERITIES.get(f.get("severity", "info"), SEVERITIES["info"])
        refs = _finding_refs(f)
        items.append(
            f'<li class="{cls.replace("sev-", "n-")}">'
            f'<span class="badge {cls}">{html.escape(label)}</span>'
            + _finding_source(f)
            + f' <span class="f-title">{f["title"]}</span>'
            + gh_comment_link(f)
            + (f'<p class="f-obs"><b>Reviewer:</b> {f["observation"]}</p>'
               if f.get("observation") else "")
            + (f'<p>{f["body"]}</p>' if f.get("body") else "")
            + (f'<p class="f-why">{f["why"]}</p>' if f.get("why") else "")
            + (f"<p>{refs}</p>" if refs else "")
            + (f.get("_snippets", "") or "")
            + (f.get("_diffs", "") or "")
            + "</li>"
        )
    return _open_list(len(findings)) + "\n".join(items) + "</ol>"


#: The confidence chip's tooltip, fixed rather than composed per item — Victor's own
#: words, with the scale in the unit the chip now shows. It names the scale, not the one
#: number already on the chip's own face; the number does not need saying twice.
CONFIDENCE_TIP = "Confidence ∈ [10% .. 90%]"


def _confidence_chip(f) -> str:
    """The number beside the purple `assumption` chip, read verbatim off
    `review-points.json`'s `confidence` — how sure the agent that wrote the code is that
    this reading of the ticket is the right one, not a severity: absent when the item
    declares none, because a scale a model was never asked to fill in is not the same fact
    as a model that filled it in at the middle. `.sev-med`'s amber marks anything under
    0.5, the same hue the rest of the page already spends on "worth a second look" — a
    confidence low enough to flag is exactly that, not a new colour to learn. Shown as a
    percentage (`0.45` → `45%`), the stored value stays a rate."""
    c = f.get("confidence")
    if c is None:
        return ""
    # A percentage, not a rate: `45%` is read at a glance, `0.45` is read as arithmetic.
    # The lede already says "3 under 70% sure"; the chips now speak the same unit.
    shown = f"{round(c * 100)}% confident"
    cls = "f-confidence sev-med" if c < 0.5 else "f-confidence"
    return (f'<span class="{cls}" title="{html.escape(CONFIDENCE_TIP, quote=True)}">'
            f'{shown}</span>')


#: The word on every assumption card — fixed, never the item's own `source`. The report
#: once carried `source` through to this badge verbatim, so one run's cards read
#: `implementation decision` and `human` where every other page reads `assumption`.
ASSUMPTION_BADGE = "assumption"


def _decided_by(f) -> str:
    """`chosen by the human`, after the chip, on the one kind of assumption the agent did
    not make: a call the human made in the conversation, recorded so the reader knows it
    was not a guess. Nothing for the agent's own — that is what the badge already says."""
    if f.get("decidedBy") == "human":
        return ' <span class="f-src">chosen by the human</span>'
    return ""


def _assumption_why(f) -> str:
    """`Why 55%: …` — the agent's reason for this reading *and* for how sure it is of it.

    The confidence chip says how sure; nothing on the card used to say why, so a 50–65%
    reading could not be argued with from the one clause beside it. `record-review` now
    asks for one or two sentences that say what holds the number where it is, and the
    label names the number the sentence answers."""
    if not f.get("why"):
        return ""
    c = f.get("confidence")
    label = f"Why {round(c * 100)}%:" if isinstance(c, (int, float)) else "Why:"
    return f'<p class="f-why"><b>{label}</b> {f["why"]}</p>'


def render_assumptions(items, mode: str = "") -> str:
    """What the agent that wrote the code decided without being told — and its alternative.

    This is the one pile on the page no pass can produce. A finding is found by reading the
    diff; an assumption is knowable only from the side that made it, and it lives in exactly
    one place: the transcript of the conversation that did the work. `authoring-sessions.py`
    says whether that conversation is the one running (mode A), an older one on disk whose
    transcript a subagent reads verbatim (mode B), or gone (mode C).

    An empty pile still renders, because the three ways of being empty are not the same
    fact and a blank space would read as the friendliest of them. "Nothing was assumed" is
    a claim; "nobody could be asked" is an admission; and the reviewer has to be able to
    tell which one they are looking at."""
    if not items:
        return {
            "A": '<p class="sub">The conversation that wrote this code was asked what it '
                 'had to guess at, and named nothing.</p>',
            "B": '<p class="sub">The transcript of the conversation that wrote this code '
                 'was read back in full, and it recorded no open question.</p>',
            "C": '<p class="sub">No transcript of the conversation that wrote this code '
                 'survives, so it could not be asked. This is not the agent saying it was '
                 'sure \u2014 it is nobody having been in a position to ask.</p>',
        }.get(mode, '<p class="sub">Nothing was assumed.</p>')
    # Least sure first. A confidence is the one number on this pile that ranks the
    # cards by how much they need the reader's judgement rather than by anything about
    # when the agent happened to write them down, and the reader's attention is worth
    # spending on the ones the agent itself was least sure about before the ones it
    # already trusted. An item that named no confidence at all is neither sure nor
    # unsure — it is unmeasured — so it goes after every measured one, in the order the
    # file already put them in: `sorted` is stable, and comparing `(False, 0.4)` against
    # `(True, None)` never touches `None` against a number.
    items = sorted(items, key=lambda f: (f.get("confidence") is None, f.get("confidence")))
    out = []
    for f in items:
        refs = _finding_refs(f)
        # One chip, not two. Every card here used to open with a purple `your call` badge
        # and then a grey monospaced `assumption` stamp — the badge naming what the reader
        # owes, the stamp naming where the item came from, and the two of them together
        # spending the whole first line of every card on the one thing all of them have in
        # common. The provenance is the word worth keeping (`assumption` is what
        # distinguishes this pile from `/code-review` and `/simplify`, which is exactly the
        # distinction the counts line above now draws), and it wears the badge's purple so
        # the pile still reads as the one the human owns.
        out.append(
            '<li class="n-assumed">'
            f'<span class="badge sev-assumed">{ASSUMPTION_BADGE}</span>'
            + _confidence_chip(f)
            + _decided_by(f)
            + f' <span class="f-title">{f["title"]}</span>'
            + gh_comment_link(f)
            + (f'<p>{f["body"]}</p>' if f.get("body") else "")
            + (f'<p class="f-alt"><b>Read the other way:</b> {f["alternative"]}</p>'
               if f.get("alternative") else "")
            + _assumption_why(f)
            + (f"<p>{refs}</p>" if refs else "")
            + (f.get("_snippets", "") or "")
            + (f.get("_diffs", "") or "")
            + "</li>"
        )
    return _open_list(len(items)) + "\n".join(out) + "</ol>"


def render_autofixes(fixes, badge: str = "auto-fixed") -> str:
    """What the agent already fixed \u2014 the tail of the same list.

    It continues the open findings' numbering on purpose. The two piles are one decision
    split in two: everything with a single obvious right answer was applied, everything a
    second engineer could reasonably disagree about was left. A reviewer who cannot see the
    first pile has to take the size of the second on trust \u2014 and a reviewer shown two lists
    that both start at 1 has to add them up by hand.

    Each item shows its diff rather than describing it. That is the whole difference between
    this and a changelog: the reader sees what was done to their code without leaving the
    page or trusting a sentence about it.

    `badge` is the word on each card, and it is a parameter because the same pile now
    arrives two ways. `auto-fixed` is right for a pass that applied its own findings with
    nobody in between. Read off `review-points.md` it would be a small lie in the one place
    a reader looks first: the agent read each finding and *chose* to accept it, which is the
    fact the pile exists to record, so there the word is `fixed`."""
    if not fixes:
        return '<p class="sub">Nothing was applied automatically \u2014 every finding needed a human.</p>'
    items = []
    for f in fixes:
        refs = _finding_refs(f)
        items.append(
            '<li class="fixed">'
            f'<span class="badge sev-fixed">{html.escape(badge)}</span>'
            + _finding_source(f)
            + f' <span class="f-title">{f["title"]}</span>'
            + gh_comment_link(f)
            + (f'<p class="f-obs"><b>Reviewer:</b> {f["observation"]}</p>'
               if f.get("observation") else "")
            + (f'<p class="f-why">{f["why"]}</p>' if f.get("why") else "")
            + (f'<p>{f["body"]}</p>' if f.get("body") else "")
            + (f"<p>{refs}</p>" if refs else "")
            + (f.get("_fixDiffs") or f.get("_diffs", "") or "")
            + (f'<p class="f-fix"><b>Fix:</b> {f["fix"]}</p>' if f.get("fix") else "")
            + (f.get("_snippets", "") or "")
            + "</li>"
        )
    return _open_list(len(fixes)) + "\n".join(items) + "</ol>"


# --------------------------------------------------------------------------- #
# Each fix's own hunks, not the whole file
# --------------------------------------------------------------------------- #

#: How far, in lines, a hunk's change may sit from a Fixed card's `file:line` and still be
#: that card's. Nearer than this, the nearest card takes it; further, nobody does, and it
#: is listed under the pile as another change in the fix commit.
FIX_HUNK_REACH = 15

#: Files a fix commit carries that are the review's bookkeeping rather than a fix. The
#: points file itself is added from the report's own `source`.
FIX_BOOKKEEPING = ("review-cost.json",)


def fix_hunks(rel: str, base: str, head: str | None, root: Path) -> list[tuple[int, int]]:
    """`[(lo, hi), …]` — the new-side lines each hunk of `rel` changed, one pair per hunk,
    in the order `diff_html(…, hunks=…)` indexes them (same command, same context).

    Context lines are not counted: a hunk reaches as far as what it changed. A pure deletion
    sits at the new-side line it was cut before."""
    proc = subprocess.run(
        ["git", "-C", str(root), "diff", f"-U{DIFF_CONTEXT}", "--no-color", base]
        + ([head] if head else []) + ["--", rel], capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    out = []
    for part in re.split(r"(?m)^(?=@@ )", proc.stdout)[1:]:
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", part)
        new_no = int(m.group(1)) if m else 0
        touched = []
        for line in part.split("\n")[1:]:
            if line.startswith("+"):
                touched.append(new_no)
                new_no += 1
            elif line.startswith("-"):
                touched.append(new_no)
            elif line.startswith("\\"):
                continue
            else:
                new_no += 1
        if touched:
            out.append((min(touched), max(touched)))
        else:
            out.append((new_no, new_no))
    return out


def _ref_spans(ref: str) -> tuple[str, list[tuple[int, int]] | None]:
    """`path:12-30,40` → `("path", [(12, 30), (40, 40)])`; a bare path → `(path, None)`."""
    m = re.match(r"^(.*?):(\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*)$", ref)
    if not m:
        return ref, None
    spans = []
    for part in m.group(2).split(","):
        lo, _, hi = part.partition("-")
        spans.append((int(lo), int(hi or lo)))
    return m.group(1), spans


def _gap(a: tuple[int, int], b: tuple[int, int]) -> int:
    """Lines between two inclusive ranges; 0 when they touch or overlap."""
    return max(0, a[0] - b[1], b[0] - a[1])


def _fix_range(item: dict, points: dict) -> tuple[str | None, str | None]:
    """`(base, head)` of the commit(s) a Fixed item's diff is read from.

    The base is the implementation commit the item's diffs already name. The head is the
    rev the item pins (`fixed-in: <sha>`), else the commit that recorded the report — the
    `[auto-fix]` commit, which carries every fix of the round — else the working tree."""
    d = (item.get("diffs") or [{}])[0]
    prov = points.get("provenance") or {}
    return (d.get("base") or prov.get("implementation"),
            d.get("head") or prov.get("reviewCommit"))


def attribute_fix_hunks(spec: dict, out_dir: Path, root: Path | None = None) -> None:
    """Show each Fixed card the hunks its own anchors reach, and list the rest after the pile.

    Each card used to render the whole file against the implementation commit, once per
    file it named. One `[auto-fix]` commit usually carries every fix, so a file two fixes
    touched appeared under both cards with both changes, a one-line fix showed +22 because
    another fix's tests sat in the same spec, and a file no card named was on no card at
    all. Now the fix commit's hunks are dealt out by position: a hunk goes to the card
    whose `file:line` it overlaps or comes nearest to, within `FIX_HUNK_REACH` lines. What
    no card reaches is rendered once, under the pile, as *other changes in the fix commit*
    — so the pile still adds up to the whole commit.

    Rendered here, as `_fixDiffs` on each item and `fixOther` on the report, rather than
    through the item's `diffs`: those the build would draw whole. A card whose anchor got
    a hunk loses the snippet of the same lines, which the hunk already shows."""
    points = spec.get("_reviewPoints") or {}
    fixes = [f for f in spec.get("autofixes") or [] if isinstance(f, dict) and f.get("diffs")]
    if not fixes or points.get("missing"):
        return
    root = root if root is not None else _git_root(out_dir)
    if root is None:
        return
    skip = {points.get("source") or "review-points.md"}
    groups: dict[tuple, list[dict]] = {}
    for f in fixes:
        base, head = _fix_range(f, points)
        if base and _git_out(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}") \
                and (not head or _git_out(root, "rev-parse", "--verify", "--quiet",
                                          f"{head}^{{commit}}")):
            groups.setdefault((base, head), []).append(f)
    other_html = []
    for (base, head), items in groups.items():
        listed = _git_out(root, "diff", "--name-only", "--no-renames", base,
                          *([head] if head else [])) or ""
        files = [p for p in listed.splitlines()
                 if p and p not in skip and Path(p).name not in FIX_BOOKKEEPING]
        owned: list[dict[str, list[int]]] = [{} for _ in items]
        unowned: dict[str, list[int]] = {}
        for rel in files:
            for idx, span in enumerate(fix_hunks(rel, base, head, root)):
                near, whole = [], []
                for i, f in enumerate(items):
                    for ref in f.get("refs") or []:
                        path, spans = _ref_spans(ref)
                        if path != rel:
                            continue
                        if spans is None:
                            whole.append(i)
                            continue
                        gap = min(_gap(s, span) for s in spans)
                        if gap <= FIX_HUNK_REACH:
                            near.append((gap, i))
                if near:
                    best = min(g for g, _ in near)
                    takers = sorted({i for g, i in near if g == best})
                else:
                    takers = sorted(set(whole))
                for i in takers:
                    owned[i].setdefault(rel, []).append(idx)
                if not takers:
                    unowned.setdefault(rel, []).append(idx)
        for f, mine in zip(items, owned):
            # The card's own files first, in the order it named them; then any other file
            # its anchors reached (a whole-file ref), in diff order.
            order = []
            for ref in f.get("refs") or []:
                path = _ref_spans(ref)[0]
                if path in mine and path not in order:
                    order.append(path)
            order += [p for p in mine if p not in order]
            f["_fixDiffs"] = "".join(diff_html(p, base, root, None, head, hunks=mine[p])
                                     for p in order)
            if f.get("snippets"):
                f["snippets"] = [s for s in f["snippets"]
                                 if _ref_spans(str(s.get("ref", "")))[0] not in mine]
            f["diffs"] = []
        if unowned:
            rng = (f"<code>{html.escape(base[:8])}..{html.escape(head[:8])}</code>" if head
                   else f"<code>{html.escape(base[:8])}</code>..the working tree")
            n = sum(len(v) for v in unowned.values())
            other_html.append(
                '<div class="fixother">'
                f'<p class="fixother-h"><b>Other changes in the fix commit</b> · {rng}: '
                f'{n} hunk{"" if n == 1 else "s"} no card\'s <code>file:line</code> reaches '
                f'(within {FIX_HUNK_REACH} lines), shown so nothing the fixes changed is '
                'off the page.</p>'
                + "".join(diff_html(p, base, root, None, head, hunks=v)
                          for p, v in unowned.items())
                + '</div>')
    if other_html:
        points["fixOther"] = "".join(other_html)

#: Where `run-steps.py`'s `aftermath` step leaves what it measured.
AFTERMATH_JSON = "aftermath.json"

#: How many files one commit's row names before it stops naming them. A commit that
#: touched forty files is a commit whose *subject* is the answer; the list is there so a
#: reader recognises a one-file config tweak without opening anything.
AFTERMATH_FILES = 6


def _aftermath_files_tip(c: dict) -> str:
    """What this commit touched, as one hover on its sha.

    It used to be a line of its own under every commit — `human-review.json +18 −1` — and
    on a branch with six commits that was six lines of filenames and arithmetic between the
    reader and the two things they can do about any of it. The band's job is to say *that
    the page is describing an older branch*; which file moved is the follow-up question, and
    a follow-up question belongs on the thing it is about, which is the sha.

    Plain text, not markup: a tooltip is read in one glance with a hand on the mouse."""
    files = c.get("files") or []
    if not files:
        # A merge commit prints no numstat. "Nothing changed" is the wrong reading of it.
        return "No file list — a merge, or nothing git could count."
    parts = []
    for f in files[:AFTERMATH_FILES]:
        counts = " ".join(x for x in (
            f"+{f['added']}" if f.get("added") else "",
            f"\u2212{f['deleted']}" if f.get("deleted") else "") if x) or "no lines"
        parts.append(f"{f['path']} {counts}"
                     + (" (generated)" if f.get("generated") else ""))
    if len(files) > AFTERMATH_FILES:
        parts.append(f"and {len(files) - AFTERMATH_FILES} more")
    return "\n".join(parts)


def _aftermath_commit(c: dict) -> str:
    """One commit's row: the sha (carrying the file list in its hover), what the commit
    did, and when. Nothing to press: reverting, cherry-picking or re-reviewing any of it is
    the developer's call, made from the command line, and the page does not teach it.
    """
    when = (c.get("when") or "")[:10]
    return ('<li>'
            f'<code data-tip="{html.escape(_aftermath_files_tip(c), quote=True)}">'
            f'{html.escape(c["short"])}</code> '
            f'{html.escape(c.get("subject", ""))}'
            + (f' <span class="rb-gen">{html.escape(when)}</span>' if when else "")
            + '</li>')


def _regenerate_offer(out_dir: Path, root: Path) -> str:
    """The band's one answer, once, under the list of commits.

    The band says a human moved the code after the review was written, and everything else
    on the page describes the branch as the agent left it. The thing a reader wants at that
    point is not to undo the commit — it is legitimate more often than not — but to make
    the rest of the page catch up with it, which is the masthead's rerun, offered here
    because here is where the reader is actually looking at the problem.

    Once per band and not once per commit: the command does not name a commit, so three
    copies of it under three shas would be three identical buttons inviting the reader to
    work out which one applies to which row. The answer is the band's, so it sits with the
    band.

    **One control, not a button with a glyph beside it.** It was a grey pill reading
    *Regenerate the report* and, next to it, a separate `↻` — two elements, one action, and
    a reader who pressed one had no way to know the other did the same thing. Now the words
    and the mark are the same control, which is the rule every command on this page follows.

    **And the line it copies is the line the server runs.** `__rerun__` is not a name out of
    the content file — it is the server's own verb — but its command is declared in the
    manifest like everything else, so this does not reconstruct it. It used to, and the two
    had drifted: this printed `cd <repo> && refresh-report.py --dir … --steps static` while
    `serve-review.py` ran the same program through a different interpreter with `--no-serve`
    on the end. Reading the register is what makes the drift unrepresentable rather than
    merely fixed.
    """
    entry = ACTIONS.get(RERUN_ACTION)
    if not entry:
        return ""
    return ('<p class="rb-actions"><span class="rb-act">'
            + command_html(entry["command"], RERUN_ACTION,
                           label="Regenerate the report",
                           tip="Rebuilds this page against the branch as it is now",
                           running="Rebuilding this page…")
            + '</span></p>')


def _tooling_commit_shas(root: Path, base_ref: str | None, shas: list[str]) -> set[str]:
    """Which of these commits are the base's own, already — read with `git`, at build
    time, because the aftermath step hands the band a list of commits and nothing about
    which of them the base already carries.

    Two different ways a commit can already be `base_ref`'s: `git cherry` catches a
    cherry-pick — a new sha, the same patch, reported `-` when an equivalent (by patch-id)
    already sits on the base. `git merge-base --is-ancestor` catches the other way a
    commit crosses branches unchanged — a merge, or a rebase that replays without
    conflict — where the sha itself, not just its patch, is already reachable from the
    base. Neither test alone catches both: a cherry-pick gets a new sha `git
    merge-base` has never seen, and a merged commit's patch-id is exactly the one already
    on the base, which is what `git cherry` is answering in the first place — the two are
    complementary, not redundant.

    **Asked once per commit, not once for the branch.** `git cherry <base> <head>` is a
    *symmetric* difference: it patch-ids `base..head` on one side and `head..base` on the
    other, and reports `-` only for a pair that matches across the two. The moment the
    branch merges the base — which is exactly what a branch that also cherry-picks tooling
    does, and what `CLAUDE.md` tells this project to do — `head..base` empties out, because
    the base's commits are now reachable from the head. There is nothing left on the right
    to match against, so every pick comes back `+`. On the demo branch that reported 2
    tooling commits where 8 had been picked: the six it missed were all older than the
    first `Merge main`.

    Per commit the window is the honest one again: `<sha>..<base>` is everything the base
    grew that this particular commit cannot see, which is where a commit copied off the
    base actually lives. One `git cherry` per commit, and on a twenty-commit branch the
    whole loop is well under a second — the band is built once per page.

    Best-effort, and silent about it: a repository this cannot ask (no `base_ref`, a
    shallow clone, `git` missing) reports every commit as the branch's own rather than
    guessing, because a tooling commit wrongly kept in the list a reader can filter with
    their own judgement; a branch commit wrongly folded away as tooling is invisible."""
    if not shas or not base_ref:
        return set()
    tooling: set[str] = set()
    for sha in shas:
        cherry = subprocess.run(["git", "cherry", base_ref, sha], cwd=root,
                                capture_output=True, text=True)
        if cherry.returncode == 0:
            for line in cherry.stdout.splitlines():
                marker, _, listed = line.strip().partition(" ")
                # Its own line, not the whole range: `base..sha` ends at `sha` but also
                # holds every branch commit before it, and those are answered by their
                # own pass with their own window.
                if listed == sha and marker == "-":
                    tooling.add(sha)
        if sha in tooling:
            continue
        anc = subprocess.run(["git", "merge-base", "--is-ancestor", sha, base_ref],
                              cwd=root, capture_output=True)
        if anc.returncode == 0:
            tooling.add(sha)
    return tooling


def _merge_seam_shas(root: Path, shas: list[str]) -> set[str]:
    """The merge commits among these, which the band drops rather than lists.

    A `Merge main: …` commit has no patch of its own — `git show` on it is empty — so it
    can be neither a cherry-pick (`git cherry` refuses to patch-id a merge and leaves it
    out of its output entirely) nor an ancestor of the base (the merge itself was made on
    the branch). It falls through both tests in `_tooling_commit_shas` and lands in the
    branch's own list, where it reads as a commit that changed nothing.

    Everything it brought is already on the list beside it, commit by commit, folded as
    tooling. The seam itself is the one row that says nothing a reader can act on, so it
    does not get a row. Merges only: an ordinary commit with an empty file list is a
    person having committed nothing, which is worth seeing."""
    if not shas:
        return set()
    out = subprocess.run(["git", "rev-list", "--merges", "--no-walk", *shas],
                         cwd=root, capture_output=True, text=True)
    if out.returncode != 0:
        return set()
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def _code_totals(commits: list[dict]) -> dict:
    """`{files, added, deleted, genFiles}` over exactly these commits' own file lists —
    not the aftermath step's pre-aggregated `totals`, which is summed over every commit
    the step saw. This band may now be folding some of those away as tooling, and a total
    that still includes a folded commit's lines is the thing the fold exists to stop
    saying. Same arithmetic `run-steps.py` already does per commit; asked here of
    whichever subset the band is about to head with."""
    files = added = deleted = gen_files = 0
    for c in commits:
        for f in c.get("files") or []:
            if f.get("generated"):
                gen_files += 1
                continue
            files += 1
            added += f.get("added", 0) or 0
            deleted += f.get("deleted", 0) or 0
    return {"files": files, "added": added, "deleted": deleted, "genFiles": gen_files}


def _tooling_fold_html(commits: list[dict], base_label: str) -> str:
    """The base's own commits, folded to one grey row — expandable, never counted in the
    band's headline. A cherry-picked guardrail is not news about this review; it is
    `main`'s own history riding along, and a band that lists eight of them beside two
    commits that actually touched the feature buries the two a reader came for."""
    n = len(commits)
    plural = "" if n == 1 else "s"
    items = "".join(_aftermath_commit(c) for c in commits)
    return ('<details class="toolcommits"><summary><span class="foldlbl">'
            f'{n} tooling commit{plural} merged from {html.escape(base_label)}'
            f'</span></summary><ul>{items}</ul></details>')


def _taken_fold_html(commits: list[dict], takeover: dict | None,
                     review_short: str) -> str:
    """The branch's own commits a takeover accepted without a pass, folded to one row.

    They used to be a second list, above this band, typed by the agent that wrote the
    takeover note: the same kind of statement as the band, counted from a different commit,
    and frozen at the moment the note was written. Here they are read off `git` with the
    rest of the band, split from tooling the same way, and the note's heading rides in the
    hover."""
    n = len(commits)
    when = html.escape(((takeover or {}).get("when") or "")[:10])
    tip = html.escape((takeover or {}).get("heading") or "", quote=True)
    items = "".join(_aftermath_commit(c) for c in commits)
    return (f'<details class="toolcommits takenover" title="{tip}"><summary>'
            f'<span class="foldlbl">{n} commit{"" if n == 1 else "s"} after '
            f'<code>{html.escape(review_short)}</code> taken over without a new pass'
            + (f' on {when}' if when else '')
            + f'</span></summary><ul>{items}</ul></details>')


def aftermath_html(out_dir: Path, root: Path, base_ref: str | None = None) -> str:
    """What landed on the branch after the agent stopped, at the top of the Review tab.

    This is the one band on the page that is about the page rather than about the code.
    Every number here was measured from a diff, and a diff cannot say when it was written:
    the film, the findings, the declined items, the assumptions and the costs all describe
    the branch as the agent left it, and three commits later they describe something
    nobody reviewed. The reader cannot see that, because the page is the only thing in a
    position to say it.

    Red when a file no generator owns has moved; grey when every one of them is generated,
    which on this project's own demo branch is the normal case — the guardrails regenerate
    diagrams, a spec and a `.drawio` on every commit, and a band that is red for that is a
    band nobody reads by the third branch. Nothing at all when the agent's commit is the
    tip, which is the state this whole flow is trying to produce.

    `base_ref` splits the commits a second way, orthogonal to generated/not: a tooling
    cherry-pick from `main` (see `_tooling_commit_shas`) is folded into one grey row and
    left out of every count in the headline, because it is not a change to this review —
    it is `main` arriving, the way `CLAUDE.md`'s own workflow says it should. The merge
    commits that brought it are dropped outright (see `_merge_seam_shas`): they carry no
    patch, so they belong to neither half of that split.
    """
    try:
        doc = json.loads((out_dir / AFTERMATH_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # No measurement is not "nothing happened". The step says why on its own row of
        # the status table (no review commit, most often), and inventing a reassuring
        # band here would be the page asserting the one thing it does not know.
        return ""
    # A takeover's own commit is bookkeeping — it touches the points file and nothing else,
    # and the band already says, in words, where it sits.
    commits = [c for c in doc.get("commits") or [] if not c.get("takeover")]
    if not commits:
        return ""
    # The seams first: a merge that brought the base in is not a commit this band has
    # anything to say about, and leaving it in makes both halves of the split wrong — it
    # is not tooling (it was made here) and it is not the branch's own work (it carries no
    # patch), so it inflates whichever list it falls into.
    seams = _merge_seam_shas(root, [c["sha"] for c in commits if c.get("sha")])
    commits = [c for c in commits if c.get("sha") not in seams]
    if not commits:
        return ""
    tooling_shas = _tooling_commit_shas(
        root, base_ref, [c["sha"] for c in commits if c.get("sha")])
    tooling = [c for c in commits if c.get("sha") in tooling_shas]
    branch_all = [c for c in commits if c.get("sha") not in tooling_shas]
    # What a takeover accepted without a pass, and what nobody has signed off at all. The
    # headline, the colour and the counts are about the second: the first was a decision,
    # and it is on the band as one fold, not as news.
    taken = [c for c in branch_all if c.get("taken_over")]
    branch_only = [c for c in branch_all if not c.get("taken_over")]
    takeover = doc.get("takeover") if isinstance(doc.get("takeover"), dict) else None
    since = "the review was taken over" if takeover else "the agent finished"
    reviewed = 'Reviewed at <code>' + html.escape(doc.get("review_short", "")) + '</code>'
    if takeover:
        reviewed += ('; taken over at <code>' + html.escape(takeover.get("sha", "")[:8])
                     + '</code> on ' + html.escape((takeover.get("when") or "")[:10])
                     + ' without a new pass')
    base_label = (base_ref or "the base").split("/", 1)[-1]
    code = _code_totals(branch_only)
    n = len(branch_only)
    plural = "" if n == 1 else "s"
    if code["files"]:
        lines = code["added"] + code["deleted"]
        # What is stale and what is not, named. The band used to say that everything on
        # every tab described the branch as it was — true of the page the agent built,
        # false one press of *Regenerate* later, when every measured tab is rebuilt from
        # the branch as it is now and only the model's half still dates from the review.
        # A reader who had just regenerated read the band as the page contradicting
        # itself, and asked why regenerating had not made the list go away. It cannot:
        # the list is code the review never judged, and only a new review pass — a commit
        # carrying `Review-Points:` — moves the point it is counted from. Said here, once,
        # so the button under the list is not mistaken for the thing that clears it.
        title = (f'<b>{n} commit{plural}, {lines} line'
                 f'{"" if lines == 1 else "s"} changed since {since}</b>')
        head = ('<p>The findings, the assumptions and the requirements matrix were written '
                'before them and have not seen them; every measured tab is rebuilt from '
                'the branch as it is now.</p>')
        sub = (reviewed + '. '
               + (f'{code["genFiles"]} generated file'
                  + ("" if code["genFiles"] == 1 else "s")
                  + ' moved as well and are not counted here. '
                  if code["genFiles"] else
                  'None of it is a generated file. ')
               + 'This list clears when a new review pass lands — a commit carrying a '
                 '<code>Review-Points:</code> trailer — not when the page is regenerated.')
        cls = "rband-alert"
        role = "alert"
    elif n:
        title = f'{n} commit{plural} since {since}, and every file in them is generated'
        head = ''
        sub = (reviewed + '. '
               'Regenerated output, not somebody editing the change under review — which '
               'is why this band is grey.')
        cls = "rband-warn"
        role = "status"
    elif taken:
        # Everything the branch did since the review was taken over, and nothing since:
        # the piles still describe the reviewed commit, and that is the one thing to say.
        title = (f'{len(taken)} commit{"" if len(taken) == 1 else "s"} taken over '
                 'without a new pass')
        head = ('<p>The findings, the assumptions and the requirements matrix describe '
                'the branch as it was reviewed.</p>')
        sub = reviewed + '.'
        cls = "rband-warn"
        role = "status"
    else:
        # Every commit since the review folded away as tooling: nothing here is news
        # about the review, only about what `main` shipped in the meantime.
        title = (f'Only tooling from {html.escape(base_label)} since {since} '
                 '— nothing about this review changed')
        head = ''
        sub = reviewed + '.'
        cls = "rband-warn"
        role = "status"
    # Folded to its one line. The count is the news; the commits behind it are there to
    # check, and a band that lists them open pushes the piles it qualifies off the screen.
    how = ('read from <code>git log</code> after <code>'
           + html.escape(doc.get("review_short", "")) + '</code>: every commit since, '
           'with its message')
    return (f'<details class="rband aftermath {cls}" role="{role}"><summary>{title}'
            f' <span class="rb-how">\u2014 {how}</span></summary>' + head
            + f'<p class="rb-sub">{sub}</p>'
            + ('<ul>' + "".join(_aftermath_commit(c) for c in branch_only) + '</ul>'
               if branch_only else '')
            + (_taken_fold_html(taken, takeover, doc.get("review_short", ""))
               if taken else '')
            + (_tooling_fold_html(tooling, base_label) if tooling else '')
            # After the list, not inside it: the commits are what happened, and this is the
            # one thing to do about all of them.
            + _regenerate_offer(out_dir, root) + '</details>')


#: The three block types that render the one list. Named so `render_block` can hand all
#: three to one function: they share the lede, the numbering, the band and — since
#: `{"auto": "review-points"}` — the question of what an empty one is allowed to say.
PILE_BLOCKS = ("findings", "assumptions", "autofixes")


#: The rounds the piles belong to, in the order they ran on the model: what the coder
#: assumed while implementing (I), what the code review raised (II), and the pass that
#: then applied the fixes it accepted (III) — a third run of its own, not part of the review.
PILE_ROUND = {"assumptions": ("I", "while coding"),
              "findings": ("II", "code review"), "autofixes": ("III", "fixing the review")}


def _round_kicker(spec, kind) -> str:
    """`I · while coding` over the first pile of each round, and nothing over the second
    pile of the same round. Read off the tab's own block list, so it needs no state."""
    for tab in spec.get("tabs") or []:
        kinds = [b.get("type") for b in tab.get("blocks") or [] if b.get("type") in PILE_ROUND]
        if kind not in kinds:
            continue
        first = next(k for k in kinds if PILE_ROUND[k] == PILE_ROUND[kind])
        if first != kind:
            return ""
        num, what = PILE_ROUND[kind]
        return (f'<p class="pileround"><b>Round {num}</b> \u00b7 {what}</p>')
    return ""


def render_pile_block(spec, block, heading=None):
    """One of the three piles, as `(html, weight, changes)`.

    Lifted out of `render_block` when the piles stopped being the content file's own list.
    The decision it now makes is not about layout at all — it is *whose* silence an empty
    pile is, the author's or the branch's — and that is worth testing directly rather than
    through a page build with a repository, a manifest and PlantUML behind it.

    `heading` is `render_block`'s local heading emitter; without one (a test, a caller
    rendering a pile on its own) the piles render bare.
    """
    kind = block.get("type", "section")
    points = spec.get("_reviewPoints")

    def head_of(fallback_id, fallback_title):
        if heading is None:
            return ""
        shown = block
        if points:
            # Read off the report, the heading and its intro are the builder's: whatever
            # the content file typed over them was already dropped (`own_review_tab`).
            fallback_title = PILE_TITLES[kind]
            shown = {**{k: v for k, v in block.items() if k not in ("title", "body")},
                     "title": fallback_title, "body": pile_intro(kind, points)}
        return _round_kicker(spec, kind) + heading(shown, fallback_id, fallback_title)

    # Whether these three piles are the branch's record or the content file's own list. It
    # changes what an empty one is allowed to say, and what each *item's* badge in the
    # autofixes pile is stamped with (`review-points.md` items were read and chosen —
    # "fixed" — a bare content file's were applied by the pass that raised them —
    # "auto-fixed") — and nothing else: the item shapes are identical, which is the whole
    # reason `review-points.md` could be bolted on without touching a renderer. The section
    # headings themselves no longer branch on it: "Open review issues" and "Auto-fixed"
    # read the same in both vocabularies, and only the lede above them still says whether a
    # pass or an agent's own second look raised the open pile.
    points = spec.get("_reviewPoints")
    if kind == "findings":
        items = spec.get("findings", [])
        head = _lede_above(
            head_of("first", "Open review issues" if points else "Requires human review"),
            opening_lede(spec))
        if points and not items:
            # Weight 1: the sentence saying which kind of empty this is has to keep the
            # tab alive, exactly as the assumptions pile's always has.
            return (head + points_empty_html("findings", points), 1, 0)
        return (head + render_findings(items), len(items), len(items))
    if kind == "assumptions":
        items = spec.get("assumptions", [])
        # `resolve_review_points` has already forced this block to mode C when the branch
        # carries no record, so the mode read here is the one the counts line read too.
        mode = block.get("mode", "")
        head = _lede_above(head_of("assumed", "Implementation assumptions"),
                           opening_lede(spec))
        if points and not items and not points.get("missing"):
            return (head + points_empty_html("assumptions", points), 1, 0)
        # Weight 1 even with nothing in it: an empty pile still carries the sentence
        # saying *which* kind of empty it is, and that sentence is the point.
        return (head + render_assumptions(items, mode),
                1 if (items or mode) else 0, len(items))
    items = spec.get("autofixes", [])
    head = _lede_above(head_of("fixed", "Auto-fixed"), opening_lede(spec))
    if points and not items:
        return (head + points_empty_html("autofixes", points), 1, 0)
    return (head + render_autofixes(items, badge="fixed" if points else "auto-fixed")
            + ((points or {}).get("fixOther") or ""),
            len(items), len(items))


def resolve_refs(items, root: Path):
    """Turn `path:from-to` strings into {label, abs} so the renderer can link them.

    A reference to a file that is not there is a build failure, not a link. A snippet
    already fails loudly — `extract-snippet.py` cannot cut lines out of nothing — but a
    bare ref used to render whatever it was given, so a path that went stale (a file
    renamed on the base branch, say) reached the reviewer as a deep link that silently
    did nothing when clicked. Failing here costs one build; failing there costs the
    reviewer's trust in every other link on the page.
    """
    out = []
    missing = []
    for ref in items:
        # `path`, `path:12`, `path:12-30`, `path:89,93-95`. A whole-file ref has no line
        # to cut off: `rpartition(":")` on one left an empty path, and the build aborted on
        # every `- file: b.py` review-points.py has always accepted.
        m = re.match(r"^(.*):(\d+)(?:-\d+)?(?:,\d+(?:-\d+)?)*$", ref)
        rel, start = (m.group(1), m.group(2)) if m else (ref, "1")
        target = (root / rel).resolve()
        if not target.is_file():
            missing.append(ref)
        out.append({"label": ref, "abs": f"{target}:{start}:1"})
    if missing:
        raise SystemExit(
            "[review] these references point at files that do not exist:\n  "
            + "\n  ".join(missing)
            + "\nFix the path in the content file (a base-branch rename is the usual cause)."
        )
    return out


# There is no `verdict_band_html` any more, and that is the point of this note: the band
# it built — full-bleed amber, the score at 3.4rem, a ten-pip dial, the bullets beside it —
# said the masthead's pill again a screenful lower and spent the first screenful of a review
# on a conclusion, so the list of findings the reader came for started below the fold. The
# `verdict` block in the content file is still read: its `score` is the pill's number and
# its band its colour, once `cap_grade` has lowered it to what the computed signals allow.
# Up to two of its `bullets` join the computed reasons in the grade panel; `summary` is
# dropped from this tab (`drop_model_summary`).


def _score_target(spec) -> tuple[str, str]:
    """`("review", "Review")` — the tab the verdict's reasons live in, for the score to
    link to, and the name to say in the hover.

    Found by what a tab renders, not by its id: `findings` is the block that holds the
    calls behind a score, wherever the content file puts it. The first tab is the fallback
    — it is the panel the page opens on, so a score linking there at worst goes where the
    reader already was. The emoji a label may lead with is dropped from the hover: `Open
    the 🤖 Review tab` reads as a glyph the sentence has to step over, and the pill in the
    strip is recognisable by its word.
    """
    tabs = spec.get("tabs") or []
    for tab in tabs:
        if any(b.get("type") == "findings" for b in tab.get("blocks") or []):
            return tab.get("id", ""), (tab.get("label") or tab.get("id", "")).lstrip("🤖 ")
    if tabs:
        return tabs[0].get("id", ""), (tabs[0].get("label") or "").lstrip("🤖 ")
    return "", ""


# --------------------------------------------------------------------------- #
# Push to GitHub PR — the piles, as inline comments on the pull request
# --------------------------------------------------------------------------- #

#: Written by the agent that wrote `review-points.md` (reference/pr-comments.md), or by
#: `push-pr-comments.py --from-review-points`; sent unchanged by `push-pr-comments.py`.
PR_COMMENTS_JSON = "pr-comments.json"
#: What the last push left behind: `{"comments": {"A:<slug>": {"html_url": …}}, …}`.
PR_POSTED_JSON = "pr-comments.posted.json"
PUSH_PR_ACTION = "__push_pr_comments__"
PUSH_PR_DRY_ACTION = "__push_pr_comments__:dry"
PR_PILE_LETTER = {"autofixes": "F", "findings": "I", "assumptions": "A"}
_PR_SLUG_MAX = 48


def pr_comment_slug(title: str) -> str:
    """The id `push-pr-comments.py` hides in each comment, from the item's title.

    A copy of that script's `slug`, not an import of it: the script is a dataclass module
    and `shared.actions._load` does not register what it loads in `sys.modules`, which
    `@dataclass` needs. `test_push_pr_comments.py` holds the two copies to one answer."""
    text = html.unescape(re.sub(r"<[^>]+>", "", title or "")).lower()
    s = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if len(s) > _PR_SLUG_MAX:
        s = s[:_PR_SLUG_MAX].rsplit("-", 1)[0]
    return s or "item"


def prepare_pr_push(spec: dict, out_dir: Path, root: Path, skill_dir: Path) -> dict | None:
    """Declare the two push actions and stamp each pile item with its PR comment's URL.

    Runs right after `resolve_review_points`, when the piles are lists. No payload, no
    button: the page never offers to post what nobody prepared. Each item gets `_ghUrl`
    in place — the same trick as `_snippets` / `_diffs` — so the three renderers need no
    new parameter."""
    spec["_prPush"] = None
    try:
        payload = json.loads((out_dir / PR_COMMENTS_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    comments = payload.get("comments") if isinstance(payload, dict) else None
    script = skill_dir / "push-pr-comments.py"
    if not comments or not script.is_file():
        return None
    if not pr_exists(spec, out_dir):
        # No pull request, nothing to post to: `push-pr-comments.py` would exit 2 on
        # "no PR for this branch", after the reader had pressed a button the page offered.
        print("[review] no pull request named in content.json (`pr.number` / `pr.url`) — "
              "the Review tab offers no 'Publish comment on GitHub PR'", file=sys.stderr)
        return None
    try:
        rel = str(out_dir.resolve().relative_to(root.resolve()))
    except ValueError:
        return None
    here, py = shlex.quote(str(root.resolve())), shlex.quote(sys.executable)
    push = (f"cd {here} && {py} {shlex.quote(str(script))}"
            f" --file {shlex.quote(rel + '/' + PR_COMMENTS_JSON)}")
    refresh = (f"{py} {shlex.quote(str(skill_dir / 'refresh-report.py'))}"
               f" --dir {shlex.quote(rel)} --steps reviewpoints,aftermath --no-serve")
    declare_action(PUSH_PR_DRY_ACTION, f"{push} --dry-run",
                   label="Print the exact GitHub calls the push would make")
    declare_action(PUSH_PR_ACTION, f"{push} && {refresh}", reload=True,
                   label="Post the Review tab's items as comments on the pull request")
    try:
        posted = json.loads((out_dir / PR_POSTED_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        posted = {}
    urls = {cid: (c or {}).get("html_url")
            for cid, c in (posted.get("comments") or {}).items()}
    for key, letter in PR_PILE_LETTER.items():
        for item in spec.get(key) or []:
            url = urls.get(f"{letter}:{pr_comment_slug(item.get('title', ''))}")
            if url:
                item["_ghUrl"] = url
    counts = {p: sum(1 for c in comments if c.get("pile") == p)
              for p in ("fixed", "ignored", "assumption")}
    spec["_prPush"] = {"count": len(comments), "counts": counts,
                       "posted": sum(1 for u in urls.values() if u),
                       "pushedAt": posted.get("pushed_at"),
                       "reviewUrl": posted.get("review_url"), "prUrl": posted.get("url")}
    return spec["_prPush"]


def pr_exists(spec: dict, out_dir: Path) -> bool:
    """Whether there is a pull request to post to: the content file names one (a number,
    or a `/pull/` URL), or an earlier push left its receipt. Read off what the run already
    knows rather than asked of GitHub at build time — the build runs offline too."""
    pr = spec.get("pr") or {}
    if pr.get("number") or "/pull/" in str(pr.get("url") or ""):
        return True
    return (out_dir / PR_POSTED_JSON).is_file()


def gh_comment_link(f) -> str:
    """*on GitHub ↗* beside an item's title, once it has a comment on the PR. The words
    say where the arrow goes; a bare ↗ read as "open this item" and left the reader
    guessing."""
    url = f.get("_ghUrl")
    if not url:
        return ""
    return (f' <a class="f-gh" href="{html.escape(url, quote=True)}" target="_blank" '
            'rel="noopener" data-tip="This item\'s comment on the pull request">on GitHub ↗</a>')


# Run on DOMContentLoaded, not inline: this sits in the Review tab, far above the page's
# own scripts, and `window.HR` (server.js) does not exist yet where it is parsed — an
# inline IIFE returned early and the button was never raised.
PR_PUSH_JS = """<script>document.addEventListener('DOMContentLoaded', function(){
  var b = document.querySelector('.pr-push');
  if (!b || !window.HR) return;
  HR.onready(function () { if (HR.can(b.dataset.push)) b.hidden = false; });
  var dlg = document.getElementById('pr-push-dlg');
  var face = b.textContent;
  function done(msg) { b.disabled = false; b.textContent = face; if (msg) alert(msg); }
  b.addEventListener('click', function () {
    b.disabled = true; b.textContent = 'Preparing the calls\\u2026';
    HR.run(b.dataset.dry).then(function (s) {
      if (s.exit !== 0) return done('The dry run failed:\\n\\n' + (s.output || ''));
      dlg.querySelector('pre').textContent = s.output || '';
      dlg.showModal();
      dlg.addEventListener('close', function once() {
        dlg.removeEventListener('close', once);
        if (dlg.returnValue !== 'post') return done();
        b.textContent = 'Posting\\u2026';
        HR.keepPlace();
        HR.run(b.dataset.push).then(function (s2) {
          if (s2.exit !== 0) return done('GitHub refused:\\n\\n' + (s2.output || ''));
          location.reload();
        }, function (e) { done(e.message); });
      });
    }, function (e) { done(e.message); });
  });
});</script>"""


def push_pr_button(spec) -> str:
    """*Publish comment on GitHub PR*, at the end of the Review tab's sticky counts line.

    Hidden until the probe says this server can run it — off disk, in the zip and on
    GitHub Pages there is nothing to post with. A press runs `--dry-run` first and shows
    its output (the summary, every downgrade, the exact `gh api` calls) in a dialog;
    only *Post* sends anything, under the reader's own `gh` login, and then the Review
    tab is re-derived so every item gets its ↗."""
    pp = spec.get("_prPush")
    if not pp:
        return ""
    c = pp["counts"]
    again = pp["posted"] > 0
    # One label whether or not it was pushed before — Victor's wording; the tooltip says
    # when it last went out and that a second press updates rather than duplicates.
    face = "Publish comment on GitHub PR"
    tip = (f"{c['fixed']} auto-fixed · {c['ignored']} open · {c['assumption']} "
           "assumptions, each as an inline comment on its line of the PR's diff — the "
           "calls the reviewing agent prepared in .human-review/pr-comments.json, sent "
           "unchanged. You see the exact calls (a dry run) before anything is posted. "
           + (f"Last pushed {pp['pushedAt'][:16].replace('T', ' ')}; pushing again updates "
              "those comments, it never duplicates them." if again and pp.get("pushedAt")
              else "Re-pushing later updates the same comments instead of duplicating."))
    return (f' <button type="button" class="pr-push" hidden '
            f'data-dry="{PUSH_PR_DRY_ACTION}" data-push="{PUSH_PR_ACTION}" '
            f'data-tip="{html.escape(tip, quote=True)}">{html.escape(face)}</button>')


def push_pr_dialog(spec) -> str:
    """The dry run's output and the *Post* that follows it — after the counts line, not in
    it: a `<dialog>` inside a `<p>` is not HTML the parser keeps where it was written."""
    if not spec.get("_prPush"):
        return ""
    return ('<dialog id="pr-push-dlg" class="pr-push-dlg"><form method="dialog">'
            '<p><b>These calls will be made to GitHub, under your account:</b></p><pre></pre>'
            '<menu><button value="cancel">Cancel</button> '
            '<button value="post" class="primary">Post</button></menu></form></dialog>'
            + PR_PUSH_JS)
