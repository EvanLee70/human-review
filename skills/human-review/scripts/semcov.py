#!/usr/bin/env python3
"""Semantic test coverage: the Tests tab's ticket↔tests matrix, drawn by a script.

The matrix used to be a model's whole output — `requirements-map.html` and a `test-index/`
catalogue, ~180KB of HTML, CSS and JavaScript written fresh on every paid run, and a run on
another harness invented a layout of its own. Everything in it except one judgement can be
derived, so it now is:

1. **The ticket, left.** The issue body (`gh issue view`, cached in `ticket-body.json`),
   rendered from its markdown — paragraphs, headings, the numbered requirement list as the
   ticket has it — and split into sentences, each with an id derived from its own words
   (`s` + 6 hex of a hash), so an edit elsewhere in the ticket does not re-key it.
2. **The tests, right.** The tests whose per-test coverage (`testcov.py` →
   `assets/test-coverage.json`: JaCoCo, Karma/Istanbul, V8) runs a line this PR changed,
   with their kind (UI/API/unit), name, file and what the branch did to them
   (`test-changes.py`). Without a coverage run, the tests the branch added or changed.
3. **The pairing.** Mostly scripted (`match`): a sentence and a test are paired on shared
   evidence — words of the test's name and body, normalised and stemmed, a small table of
   synonyms that maps a ticket's verbs onto a test's (`booking` ↔ `create`/`post`, `clear`
   ↔ `null`), numbers and quoted literals, routes, and the identifiers on the changed lines
   the test's coverage actually ran. Every link carries the evidence it was made on. Only
   the sentences the script cannot pair go to a cheap model (`rerun-model.py`, haiku by
   default), with their few candidate tests; its answer is `test-mapping.json`, validated
   against `reference/test-mapping.schema.json`, and merged here.
4. **The page.** Deterministic: the same four inputs give the same bytes. The renderer
   (`reqmap/reqmap.css`, `reqmap/reqmap.js`) is the one the demo PR's model wrote, lifted
   verbatim, so the tab looks exactly as it did; `hrbuild/tabs/tests.py:reqmap_layout`
   re-lays it the same way it always has.

    semcov.py inputs   --dir .human-review   # what the model would be asked, as JSON
    semcov.py match    --dir .human-review   # the scripted pairing, as JSON
    semcov.py render   --dir .human-review   # write assets/requirements-map.html
    semcov.py validate --dir .human-review   # check test-mapping.json
    semcov.py agreement --dir D --reference OLD.html   # pairs vs a model-written matrix
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import html
import json
import math
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

SKILL = HERE.parent
SCHEMA_PATH = SKILL / "reference" / "test-mapping.schema.json"
SCHEMA_VERSION = "test-mapping/1"
INPUT_VERSION = "test-mapping-input/2"
#: The model's answer, under the review directory — the one model-owned file of the tab.
MAPPING = "test-mapping.json"
#: What the page shows: script and model merged, with who paired what. Written by the
#: render, read by nobody but a reader asking "why is this sentence green".
MERGED = "assets/test-mapping.merged.json"
#: The matrix fragment the build pastes into the Tests tab.
FRAGMENT = "assets/requirements-map.html"
#: The issue body, author and avatar, asked of GitHub once.
TICKET_BODY_CACHE = "ticket-body.json"
#: What marks a fragment as drawn here rather than by a model: a model-written one from an
#: older run is kept, and rendered as it is, until a mapping exists to replace it.
GENERATED = 'data-generated="semcov"'
ASSETS = HERE / "reqmap"

CATS = {"e2e": "UI", "api": "API", "unit": "unit"}
CAT_KEY = ('<p class="rm-cats"><span><span class="rm-cat" data-cat="e2e">UI</span>clicks the '
           'screen</span><span><span class="rm-cat" data-cat="api">API</span>REST/MCP</span>'
           '<span><span class="rm-cat" data-cat="unit">unit</span>one isolated component'
           '</span></p>')
LEGEND = ('<div class="rm-legend"><span class="rm-lgt">Legend:</span>'
          '<span class="rm-lg" data-cov="covered" data-tip="Every claim it makes is asserted '
          'by a test">fully covered</span>'
          '<span class="rm-lg" data-cov="partly" data-tip="Only part of this claim is covered '
          'by tests">partially</span>'
          '<span class="rm-lg" data-cov="exercised" data-tip="A test runs through this but '
          'never checks it">executed</span>'
          '<span class="rm-lg" data-cov="missing" data-tip="No test asserts this, or even '
          'reaches it">missing</span>'
          '<span class="rm-lg" data-cov="none" data-tip="Not a claim, so nothing to cover">'
          'N/A</span></div>')
#: The two states only some matrices use, added to the legend when one of them is on it:
#: a scripted pairing no model has confirmed, and a sentence a recorded decision narrowed.
LEGEND_EXTRA = {
    "unconfirmed": ('<span class="rm-lg" data-cov="unconfirmed" data-tip="Paired on shared '
                    'words only; no model has confirmed a test asserts it">unconfirmed</span>'),
    "narrowed": ('<span class="rm-lg" data-cov="narrowed" data-tip="Not delivered as written: '
                 'a scope decision the branch recorded narrows it">narrowed</span>')}
#: The mapping's coverage word → the renderer's `data-cov` and its hover.
COV_ATTR = {"covered": "covered", "partial": "partly", "exercised": "exercised",
            "missing": "missing", "n/a": "none", "unmapped": "unmapped",
            "unconfirmed": "unconfirmed", "narrowed": "narrowed"}
COV_LABEL = {"covered": "covered", "partial": "partly covered",
             "exercised": "exercised, never asserted", "missing": "no covering tests",
             "n/a": "not a claim", "unmapped": "not paired yet",
             "unconfirmed": "paired on shared words, not confirmed",
             "narrowed": "narrowed on purpose — not delivered as written"}


def _tests_tab():
    """`hrbuild/tabs/tests.py` — the coverage join, the test excerpts, the ticket ref."""
    from hrbuild.tabs import tests
    return tests


# --- 1. the ticket ----------------------------------------------------------------------

def _gh_issue_full(slug: str, number: int) -> dict | None:
    args = ["gh", "issue", "view", str(number), "--json",
            "number,title,body,author,createdAt,url"]
    if slug:
        args += ["-R", slug]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=20, check=True)
        return json.loads(out.stdout)
    except Exception as exc:                      # noqa: BLE001 - every failure is the same
        print(f"[semcov] `gh issue view {number}` did not answer ({exc}); the ticket "
              f"column needs its body. It is cached in {TICKET_BODY_CACHE} once it does.",
              file=sys.stderr)
        return None


def _avatar(login: str) -> str:
    """The author's GitHub avatar as a data URI, so the page needs no network to draw it."""
    if not login:
        return ""
    try:
        with urllib.request.urlopen(f"https://github.com/{login}.png?size=48",
                                    timeout=10) as r:
            kind = r.headers.get_content_type() or "image/png"
            return f"data:{kind};base64," + base64.b64encode(r.read()).decode()
    except Exception:                             # noqa: BLE001 - an initial stands in
        return ""


def _issue(number: int, slug: str, out_dir: Path, title: str = "",
           url: str = "") -> dict | None:
    """Issue `number`'s body, author and avatar: from `ticket-body.json` when it is that
    issue, else asked of GitHub once and written down. None when GitHub does not answer."""
    cache = out_dir / TICKET_BODY_CACHE
    try:
        got = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        got = {}
    if got.get("number") != number or "body" not in got:
        raw = _gh_issue_full(slug, number)
        if raw is None:
            return None
        login = ((raw.get("author") or {}).get("login")) or ""
        got = {"number": number, "title": raw.get("title") or title,
               "url": raw.get("url") or url or "", "author": login,
               "avatar": _avatar(login), "createdAt": raw.get("createdAt") or "",
               "body": raw.get("body") or ""}
        try:
            cache.write_text(json.dumps(got, indent=1) + "\n", encoding="utf-8")
        except OSError:
            pass
    # The content file's title and link outrank the cache, as they do over the heading.
    return {**got, "title": title or got.get("title", ""), "url": url or got.get("url", "")}


def _repo_slug(spec: dict) -> str:
    pr = spec.get("pr") or {}
    return re.sub(r"^https?://github\.com/", "", pr.get("repo") or "").strip("/")


def front_matter(path: Path) -> dict:
    """The `key: value` lines between the `---` fences at the top of `path`, or {}."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    m = re.match(r"---\r?\n(.*?)\r?\n---\s*(?:\n|$)", text, re.S)
    if not m:
        return {}
    out = {}
    for k, v in re.findall(r"^([A-Za-z][\w-]*):[ \t]*(.*?)\s*$", m.group(1), re.M):
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out


#: An issue number in a ticket value: `#25`, `25`, `GH-25`, `issue 25` (and, below, a
#: github.com issue URL or `owner/repo#25`).
_BARE_ISSUE = re.compile(r"^\s*(?:#|gh-?|issue\s*#?)?\s*(\d+)\s*$", re.I)
_GH_ISSUE_URL = re.compile(r"^https?://github\.com/([^/\s]+/[^/\s]+)/issues/(\d+)\b", re.I)


def _ticket_value(value: str, slug: str) -> tuple[tuple[int, str] | None, str]:
    """`((number, slug) or None, text or "")` out of a front-matter `ticket:` value.

    `#25`, `25` and a github.com issue URL name an issue; a URL that is not one names
    nothing this script can read; anything else is the requirement text itself (and a
    `#25` inside it is tried as an issue first)."""
    v = (value or "").strip()
    if not v:
        return None, ""
    m = _BARE_ISSUE.match(v)
    if m:
        return (int(m.group(1)), slug), ""
    m = _GH_ISSUE_URL.match(v)
    if m:
        return (int(m.group(2)), m.group(1)), ""
    m = re.match(r"^([\w.-]+/[\w.-]+)#(\d+)$", v)
    if m:
        return (int(m.group(2)), m.group(1)), ""
    if re.match(r"^https?://\S+$", v):
        return None, ""
    m = re.search(r"(?<![\w&])#(\d+)\b", v)
    return ((int(m.group(1)), slug) if m else None), v


def branch_name(spec: dict, root: Path) -> str:
    """The branch under review: the content file's `pr.branch`, else the checkout's."""
    b = ((spec.get("pr") or {}).get("branch") or "").strip()
    if b:
        return b
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, timeout=10)
    except Exception:                             # noqa: BLE001 - no git is no branch
        return ""
    b = out.stdout.strip() if out.returncode == 0 else ""
    return "" if b == "HEAD" else b


def branch_issue(branch: str) -> int | None:
    """The issue a branch name carries — `25-paging`, `feature/25-paging`, `issue-25`,
    `gh-25`, `#25` — or None. A number at the end of a name (`hr-try-4`) is a counter,
    not an issue: reading it as #4 would draw some other ticket's matrix."""
    if re.match(r"^(?:release|hotfix|v\d)", branch, re.I):
        return None                               # a version, not an issue
    tail = branch.rsplit("/", 1)[-1]
    m = (re.match(r"^#?(\d+)(?:[-_]|$)", tail)
         or re.search(r"(?:^|[-_/])(?:issue|issues|gh|ticket)[-_#]?(\d+)(?:[-_]|$)", branch,
                      re.I))
    return int(m.group(1)) if m else None


def openspec_change(root: Path, branch: str, number: int | None) -> tuple[str, list[Path]]:
    """`(change name, its spec files)` of the OpenSpec change this branch implements, or
    `("", [])`: `openspec/changes/<name>/specs/**/spec.md`, where `<name>` is the branch's
    last segment, is contained in it, or carries the ticket's number. Archived changes are
    done and are never the requirement text of a branch under review."""
    changes = root / "openspec" / "changes"
    if not changes.is_dir():
        return "", []
    tail = branch.rsplit("/", 1)[-1].lower()
    for d in sorted(p for p in changes.iterdir() if p.is_dir() and p.name != "archive"):
        name = d.name.lower()
        hit = (bool(tail) and (name == tail or (len(name) >= 4 and name in tail)
                               or (len(tail) >= 4 and tail in name)))
        if not hit and number is not None:
            hit = bool(re.search(rf"(?:^|[-_]){number}(?:[-_]|$)", name))
        if not hit and number is not None:
            # `paginate-sort-owners` carries no number, but its proposal says which issue it
            # answers ("narrowing the original request in #25"). That is the link run 5 had.
            hit = any(re.search(rf"(?<![\w&])#{number}\b|/issues/{number}\b",
                                _read(d / f)) for f in ("proposal.md", "tasks.md",
                                                        "design.md", ".openspec.yaml"))
        specs = sorted((d / "specs").rglob("spec.md")) if (d / "specs").is_dir() else []
        if hit and specs:
            return d.name, specs
    return "", []


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def spec_requirements(paths: list[Path]) -> list[dict]:
    """The requirements of OpenSpec spec deltas, in file order: `{"name", "text",
    "scenarios"}` — the statement under `### Requirement:` (its SHALL sentences, joined into
    one paragraph) and each `#### Scenario:` as one line of plain text. ADDED and MODIFIED
    only: a REMOVED requirement is not something the branch has to prove."""
    out: list[dict] = []
    for path in paths:
        removed, cur, scen = False, None, None
        for ln in _read(path).splitlines():
            h2 = re.match(r"^##\s+(.*)$", ln)
            if h2 and not ln.startswith("###"):
                removed = bool(re.match(r"(?i)removed\b", h2.group(1).strip()))
                cur = scen = None
                continue
            req = re.match(r"^###\s+Requirement:\s*(.+?)\s*$", ln)
            if req:
                cur, scen = None, None
                if not removed:
                    cur = {"name": req.group(1), "text": [], "scenarios": []}
                    out.append(cur)
                continue
            if cur is None:
                continue
            sc = re.match(r"^####\s+Scenario:\s*(.+?)\s*$", ln)
            if sc:
                scen = [sc.group(1) + ":"]
                cur["scenarios"].append(scen)
                continue
            if not ln.strip():
                continue
            if scen is not None:
                scen.append(_plain(re.sub(r"^\s*[-*]\s+", "", ln)))
            else:
                cur["text"].append(ln.strip())
    return [{"name": r["name"], "text": " ".join(r["text"]),
             "scenarios": [" ".join(s) for s in r["scenarios"]]} for r in out]


#: The heading the spec's requirements go under, below the issue, in the left column.
SPEC_HEADING = "Requirements of the OpenSpec change {name}"


def spec_markdown(name: str, reqs: list[dict]) -> str:
    """The requirements as the numbered list the left column draws below the issue: the
    requirement's name in bold, then its statement — each sentence of it pairable alone."""
    items = "\n".join(f"{i}. **{r['name']}** — {r['text']}" if r["text"]
                      else f"{i}. **{r['name']}**" for i, r in enumerate(reqs, 1))
    return f"---\n\n### {SPEC_HEADING.format(name=name)}\n\n{items}\n"


#: Words that mark a line of a proposal or design as a scope decision.
_SCOPE_LINE = re.compile(r"out of scope|non[- ]?goals?|not in scope|\bnarrow(?:s|ed|ing)\b|"
                         r"limited to|\bonly\b.*\binstead of\b|deferred|won'?t", re.I)


def recorded_decisions(out_dir: Path, change_dir: Path | None = None) -> list[dict]:
    """What the branch decided *not* to do, as `{"id": "d1", "text"}` — the model reads these
    to tell a sentence the tests miss from one the change deliberately narrowed.

    From the review record (`review-points.json`): every assumption (what was decided, and
    the alternative it ruled out) and every finding read and declined. From the OpenSpec
    change, when there is one: its proposal's and design's lines that name a scope cut.

    Each also carries where it was recorded, so the page can name and link it rather than
    say "a recorded decision": `where` (`proposal.md:86`, or which Review-tab pile), `quote`
    (the decision in its own words, without the prefix the model reads) and, for a line of
    the OpenSpec change, `href` to that line. Only `id` and `text` go to the model."""
    out: list[tuple[str, dict]] = []
    rp = _read_json(out_dir / "review-points.json") or {}

    def clean(x) -> str:
        return _plain(html.unescape(re.sub(r"<[^>]+>", "", str(x or "")))).strip()
    for a in rp.get("assumptions") or []:
        bits = [clean(a.get("title"))]
        if a.get("alternative"):
            bits.append(f"instead of: {clean(a['alternative'])}")
        if a.get("why"):
            bits.append(f"because {clean(a['why'])}")
        out.append(("Assumed: " + "; ".join(b for b in bits if b),
                    {"where": "an assumption on the Review tab", "quote": clean(a.get("title"))}))
    for f in rp.get("findings") or []:
        out.append((f"Declined: {clean(f.get('title'))} — {clean(f.get('why'))}",
                    {"where": "a declined finding on the Review tab",
                     "quote": clean(f.get("title"))}))
    if change_dir is not None:
        for name in ("proposal.md", "design.md"):
            for n, ln in enumerate(_read(change_dir / name).splitlines(), start=1):
                if ln.lstrip().startswith("#"):
                    continue                      # a heading names a section, decides nothing
                t = _plain(re.sub(r"^\s*[-*]\s+", "", ln))
                if t and _SCOPE_LINE.search(t):
                    out.append((f"Scope ({change_dir.name}/{name}): {t}",
                                {"where": f"{name}:{n}", "quote": t,
                                 "path": f"openspec/changes/{change_dir.name}/{name}",
                                 "href": f"vscode://file/{(change_dir / name).resolve()}:{n}:1"}))
    seen, uniq = set(), []
    for t, ref in out:
        if t and t not in seen:
            seen.add(t)
            uniq.append((t[:400], ref))
    return [{"id": f"d{i}", "text": t, **{k: v for k, v in ref.items() if v}}
            for i, (t, ref) in enumerate(uniq, 1)]


#: Where the implementation conversation is exported, relative to the review directory.
IMPL_CONVERSATION = "impl-conversation.md"


def first_request(path: Path) -> str:
    """The human's first request (`## Request 0 …`) of an exported implementation
    conversation, up to the agent's answer or the next request; "" when there is none."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    out: list[str] | None = None
    for ln in lines:
        if out is None:
            if re.match(r"^#{1,6}\s+Request\s+0\b", ln, re.I):
                out = []
            continue
        if re.match(r"^#{1,6}\s+(?:Request\s+\d+\b|The agent\b)", ln, re.I):
            break
        out.append(ln)
    return "\n".join(out or []).strip()


def fetch_ticket(spec: dict, out_dir: Path, root: Path | None = None) -> dict | None:
    """The requirement text the matrix is drawn against, or None when there is none.

    `{number, title, url, author, avatar, createdAt, body, source, via}`: `source` is
    `github` for an issue (`number` set), else `front-matter`, `openspec` or
    `conversation` (`number` None); `via` says, in words, where it was found — the page
    shows it, because a matrix over a chat transcript must not pass for one over an issue.

    A GitHub issue wins whenever one resolves, named — in this order — by review-points.md's
    `ticket:` front-matter, the content file's `pr.ticket` (or the PR title's `#N`, which is
    `tests.py:ticket_ref`'s answer), or the branch name. Its body is asked of GitHub once
    and written down; the build must not need a network to draw a column it drew yesterday.
    Without one: the `ticket:` value when it is text, an OpenSpec change matching the
    branch or ticket, then the implementation conversation's first request."""
    root = Path(root) if root is not None else out_dir.resolve().parent
    slug = _repo_slug(spec)
    fm_value = front_matter(root / "review-points.md").get("ticket", "")
    fm_issue, fm_text = _ticket_value(fm_value, slug)
    branch = branch_name(spec, root)
    number = None

    if fm_issue:
        got = _issue(fm_issue[0], fm_issue[1], out_dir)
        if got:
            return {**got, "source": "github",
                    "via": f"GitHub issue #{got['number']}, named by review-points.md "
                           f"`ticket: {fm_value}`",
                    "origin": f"From GitHub issue #{got['number']}, which the review "
                              "record names"}
        number = fm_issue[0]
    ref = _tests_tab().ticket_ref(spec, out_dir)
    if ref:
        got = _issue(ref["number"], slug, out_dir, ref.get("title") or "", ref.get("url") or "")
        if got:
            pr = spec.get("pr") or {}
            declared = (pr.get("ticket") or pr.get("issue") or spec.get("ticket")
                        or spec.get("issue"))
            return {**got, "source": "github",
                    "via": f"GitHub issue #{got['number']}, named by "
                           + ("content.json `pr.ticket`" if declared else "the PR title"),
                    "origin": f"From GitHub issue #{got['number']}, "
                              + ("the ticket this review is for" if declared
                                 else "named in the pull request's title")}
        number = number or ref["number"]
    b_issue = branch_issue(branch) if branch else None
    if b_issue is not None:
        got = _issue(b_issue, slug, out_dir)
        if got:
            return {**got, "source": "github",
                    "via": f"GitHub issue #{got['number']}, named by the branch `{branch}`",
                    "origin": f"From GitHub issue #{got['number']}, named by the branch "
                              f"{branch}"}
        number = number or b_issue

    plain = {"number": None, "url": "", "author": "", "avatar": "", "createdAt": ""}
    if fm_text:
        return {**plain, "title": "the ticket text in review-points.md", "body": fm_text,
                "source": "front-matter",
                "via": "the `ticket:` text in review-points.md's front-matter — not a "
                       "GitHub issue",
                "origin": "From the ticket text the review record gives — there is no "
                          "GitHub issue"}
    name, specs = openspec_change(root, branch, number)
    if specs:
        body = "\n\n".join(p.read_text(encoding="utf-8") for p in specs)
        rel = ", ".join(f"`{p.relative_to(root)}`" for p in specs)
        return {**plain, "title": f"OpenSpec change {name}", "body": body,
                "source": "openspec", "via": f"the OpenSpec change `{name}` ({rel}) — not a "
                                             "GitHub issue",
                "origin": f"From the OpenSpec change {name} — there is no GitHub issue"}
    conv = out_dir / IMPL_CONVERSATION
    body = first_request(conv)
    if body:
        try:
            where = conv.resolve().relative_to(root.resolve())
        except ValueError:
            where = conv
        return {**plain, "title": "the implementation conversation's first request",
                "body": body, "source": "conversation",
                "via": f"the implementation conversation's first request (`{where}`, "
                       "request 0) — not a GitHub issue",
                "origin": "From the first request of the conversation that implemented "
                          "this — there is no GitHub issue"}
    return None


_OUT_OF_SCOPE = re.compile(r"\bout of scope\b|\bnon[- ]?goals?\b|\bnot in scope\b|"
                           r"\bwon'?t (?:do|fix)\b|\bnot (?:part of|included)\b", re.I)
_ABBREV = re.compile(r"(?:\b(?:e\.g|i\.e|etc|vs|cf|approx|Mr|Mrs|Dr|St|No)\.)$", re.I)
_MARK = "*_\"'”’)]`"


def _plain(md: str) -> str:
    """A sentence's words, without its markdown."""
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", md)
    s = re.sub(r"(\*\*|__|\*|_|`)", "", s)
    return re.sub(r"\s+", " ", s).strip()


def split_sentences(md: str) -> list[str]:
    """One paragraph's (or list item's) inline markdown, as sentences — markdown kept.

    A boundary is `.`, `!` or `?`, any closing markup (`**`, a quote, a bracket), then
    whitespace, then something that does not start in lower case. Abbreviations do not
    end a sentence, and a chunk that leaves `**` or a backtick open is joined to the next
    one, so a bold span is never cut in half."""
    chunks, start = [], 0
    for m in re.finditer(r"[.!?][" + re.escape(_MARK) + r"]*(\s+)", md):
        nxt = md[m.end():].lstrip(_MARK + "(")
        if not nxt or nxt[0].islower():
            continue
        if _ABBREV.search(md[start:m.start() + 1]):
            continue
        chunks.append(md[start:m.start(1)])
        start = m.end()
    chunks.append(md[start:])
    out: list[str] = []
    for c in (c.strip() for c in chunks):
        if not c:
            continue
        if out and (out[-1].count("**") % 2 or out[-1].count("`") % 2):
            out[-1] = out[-1] + " " + c
        else:
            out.append(c)
    return out


def sentence_id(text: str, seen: dict) -> str:
    """`s` + six hex of the sentence's own words: stable across edits elsewhere in the
    ticket, and readable enough for a model to copy back. A repeated sentence gets `-2`."""
    norm = re.sub(r"[^\w]+", " ", _plain(text).lower()).strip()
    sid = "s" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:6]
    seen[sid] = seen.get(sid, 0) + 1
    return sid if seen[sid] == 1 else f"{sid}-{seen[sid]}"


def parse_ticket(body: str) -> list[dict]:
    """The issue body as blocks: `{"kind": "p"|"h"|"hr"|"ol"|"ul"|"code"|"quote", ...}`.

    `p`/`quote` carry `sentences`; `ol`/`ul` carry `items`, each with its `sentences` (and
    an `ol` its `start`); `h` carries `text`. Every sentence is `{"id", "md", "text",
    "section"}`, `section` being the heading it sits under."""
    lines = (body or "").replace("\r\n", "\n").split("\n")
    blocks: list[dict] = []
    para: list[str] = []
    seen: dict = {}
    section = ""

    def sentences(md: str) -> list[dict]:
        return [{"id": sentence_id(s, seen), "md": s, "text": _plain(s), "section": section}
                for s in split_sentences(md) if _plain(s)]

    def flush():
        if para:
            md = " ".join(x.strip() for x in para)
            quote = all(x.lstrip().startswith(">") for x in para)
            if quote:
                md = " ".join(re.sub(r"^\s*>\s?", "", x) for x in para).strip()
            blocks.append({"kind": "quote" if quote else "p", "sentences": sentences(md)})
            para.clear()

    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("```"):
            flush()
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            blocks.append({"kind": "code", "text": "\n".join(lines[i + 1:j])})
            i = j + 1
            continue
        if not ln.strip():
            flush()
            i += 1
            continue
        h = re.match(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$", ln)
        if h:
            flush()
            section = _plain(h.group(2))
            blocks.append({"kind": "h", "text": h.group(2)})
            i += 1
            continue
        if re.match(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$", ln):
            flush()
            blocks.append({"kind": "hr"})
            i += 1
            continue
        item = re.match(r"^\s{0,3}(?:(\d+)[.)]|[-*+])\s+(.*)$", ln)
        if item:
            flush()
            kind = "ol" if item.group(1) else "ul"
            lst = {"kind": kind, "items": []}
            if kind == "ol":
                lst["start"] = int(item.group(1))
            while i < len(lines):
                it = re.match(r"^\s{0,3}(?:(\d+)[.)]|[-*+])\s+(.*)$", lines[i])
                if not it or bool(it.group(1)) != (kind == "ol"):
                    break
                text = [re.sub(r"^\[[ xX]\]\s+", "", it.group(2))]
                i += 1
                # Continuation lines: indented, and not a new item.
                while i < len(lines) and lines[i].strip() and lines[i].startswith((" ", "\t")) \
                        and not re.match(r"^\s{0,3}(?:\d+[.)]|[-*+])\s+", lines[i]):
                    text.append(lines[i].strip())
                    i += 1
                lst["items"].append({"sentences": sentences(" ".join(text))})
            blocks.append(lst)
            continue
        para.append(ln)
        i += 1
    flush()
    return blocks


def ticket_sentences(blocks: list[dict]) -> list[dict]:
    """Every sentence of the ticket, in reading order."""
    out = []
    for b in blocks:
        for s in b.get("sentences") or []:
            out.append(s)
        for it in b.get("items") or []:
            out.extend(it["sentences"])
    return out


def _inline(md: str) -> str:
    """Inline markdown → HTML: code, links, bold, italics. Escaped first."""
    codes: list[str] = []

    def keep(m):
        codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00{len(codes) - 1}\x00"
    s = re.sub(r"`([^`]+)`", keep, md)
    s = html.escape(s, quote=False)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
               lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>', s)
    s = re.sub(r"(\*\*|__)(.+?)\1", r"<strong>\2</strong>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", s)
    s = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"<em>\1</em>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], s)


# --- 2. the tests -----------------------------------------------------------------------

def _states(test_doc: dict | None) -> dict:
    states = {}
    for t in (test_doc or {}).get("tests") or []:
        states[(t.get("path"), t.get("name"))] = t
        states[(t.get("path"), t.get("line"))] = t
    return states


STAMP = {"added": "new", "modified": "changed", "deleted": "deleted"}


def _stamp(st: dict) -> str:
    """The card's word for what the branch did to a test — `helper` for one whose own
    lines are untouched but which calls a same-file helper the branch rewrote
    (`test-changes.py:helpers_called`); it is still one of the edited."""
    if st.get("status") == "modified" and st.get("viaHelper"):
        return "helper"
    return STAMP.get(st.get("status"), "unchanged")


def covering_tests(spec: dict, out_dir: Path, root: Path) -> tuple[list[dict], bool]:
    """`(rows, measured)` — the right-hand column, before any pairing.

    Measured: every test whose own coverage ran a line this PR changed
    (`tests.py:coverage_join` over `assets/test-coverage.json`), in that join's order.
    Unmeasured: the tests the branch's test files declare (`test-changes.py`'s manifest),
    which is the honest list when nothing was run. Each row is `{"id": "file:line", "file",
    "line", "title", "suite", "cat", "status", "hits", "aimed"}`."""
    T = _tests_tab()
    test_doc = T._load_test_changes(spec, out_dir) or _read_json(out_dir / "assets/test-changes.json")
    states = _states(test_doc)
    doc = T.load_coverage(out_dir, spec)
    rows, seen = [], set()
    if doc is not None:
        for r in T.coverage_join(doc)["rows"]:
            file, line = r.get("file"), r.get("line")
            if not file or not line or f"{file}:{line}" in seen:
                continue
            seen.add(f"{file}:{line}")
            st = states.get((file, r.get("title"))) or states.get((file, line)) or {}
            rows.append({"id": f"{file}:{line}", "file": file, "line": int(line),
                         "title": r.get("title") or "", "suite": r.get("suite") or "",
                         "cat": T._cov_cat(r, root),
                         "status": _stamp(st), "viaHelper": st.get("viaHelper") or [],
                         "hits": r.get("changedHits") or {}, "aimed": bool(r.get("aimed"))})
        return rows, True
    for t in (test_doc or {}).get("tests") or []:
        file, line = t.get("path"), t.get("line")
        if not file or not line or f"{file}:{line}" in seen:
            continue
        seen.add(f"{file}:{line}")
        row = {"id": f"{file}:{line}", "file": file, "line": int(line),
               "title": t.get("name") or "", "suite": "",
               "cat": T._cov_cat({"file": file}, root),
               "status": _stamp(t), "viaHelper": t.get("viaHelper") or [],
               "hits": {}, "aimed": True}
        # A deleted test is linked where it stood at the base, never at its HEAD `line`.
        if t.get("baseUrl") and t.get("status") == "deleted":
            row["baseUrl"] = t["baseUrl"]
        rows.append(row)
    return rows, False


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_GHERKIN_NEXT = re.compile(r"\s*(Scenario|Rule|Feature|Background|Examples|@)")
BODY_MAX = 60


#: How far above a declaration its annotations may reach.
ANNOTATIONS_MAX = 30


def annotations_start(lines: list[str], line: int) -> int:
    """The first line of the annotations (tags, decorators) right above `line`, a
    multi-line one included: run 8's `invalidInput_isBadRequest` carries its cases —
    `"size=7", "size=0", …` — in a `@ValueSource(strings = {` four lines long, and a
    window that stopped at the first line not starting with `@` read a body with no page
    size in it, so nothing tied the test to "valid page sizes SHALL be only 5, 10, 20"."""
    start, k = line, line - 1
    while k >= 1 and line - k <= ANNOTATIONS_MAX:
        text = lines[k - 1].strip()
        if text.startswith("@"):
            block = "\n".join(lines[k - 1:start - 1])
            if block.count("(") == block.count(")"):
                start = k
        elif not text or text.endswith((";", "{")) or text == "}":
            break
        k -= 1
    return start


def test_source(root: Path, file: str, line: int) -> tuple[int, list[str]]:
    """`(first line, lines)` of the test's own body: its annotations, down to the brace
    that closes it (a Gherkin scenario down to the next keyword), at most BODY_MAX lines.
    The same window `tests.py:_cov_part` quotes on the card."""
    path = root / file
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return line, []
    if not 1 <= line <= len(lines):
        return line, []
    start = annotations_start(lines, line)
    if path.suffix == ".feature":
        end = line
        while end < len(lines) and not _GHERKIN_NEXT.match(lines[end]):
            end += 1
    else:
        end = _tests_tab()._snippet_module()._closing_line(lines, line, line)
    while end > line and not lines[end - 1].strip():
        end -= 1
    end = min(end, start + BODY_MAX - 1)
    return start, lines[start - 1:end]


# --- 3. the scripted pairing ------------------------------------------------------------
#
# A sentence of a ticket and a test of a suite are written by different people in
# different registers — "Booking a visit lets you not choose a vet" against
# `create_withoutVet_leavesItUnassigned` — and the whole of this pass is making the two
# comparable: split identifiers, drop the words every sentence and every test has, stem
# what is left, and fold a short table of synonyms onto one concept each. What survives
# is weighted by how rare it is across the tests on the card (a word every test shares
# pins nothing), and a link is made only on a rare word, never on a common one alone.

STOP = set("""
a an the and or nor but if then so of to in on at by for with from into onto as is are was
were be been being it its this that these those there here than such which who whom whose
what when where while how why all any each every some most more much many one ones own
same other another both either neither only also too very just even still yet ever again
should must can could will would shall may might do does did done has have had having
i we you he she they them their our your my me us his her him
today means mean like time half before after about also lets let make makes made way
anyone someone everyone anything something everything everywhere throughout always never
know knows knew take taking took come coming came back kept go goes going went thing things
rather instead whether because though although however therefore thus already once
good bad better worse new old first last next same different real really actually
data information case cases part parts point use used using work works need needs want
app application system user users people person day days week weeks year years
test tests spec specs it describe expect assert asserts given when then scenario feature
void public private protected static final class return var let const await async
function throws exception string int long boolean true false undefined this
mock mvc perform result results status andexpect jsonpath json content type
fixture detectchanges tobe toequal tohavetext tocontain dto entity id ids
""".split())

#: Words that carry nothing but a concept: a ticket's "not choose one" is a test's
#: `withoutVet`, and neither the word "not" nor "without" pairs anything on its own.
CONCEPT_ONLY = {"no": "NONE", "not": "NONE", "without": "NONE", "none": "NONE",
                "never": "NONE", "nobody": "NONE", "nothing": "NONE"}

#: Words that mean the same thing to a test as they do to a ticket, folded onto a concept.
#: Generic CRUD and UI vocabulary — nothing in here knows what a pet clinic is. A word can
#: belong to two concepts; a sentence and a test share a concept when they share any.
CONCEPTS = {
    "CREATE": "book add creat new post schedul regist insert submit",
    "UPDATE": "edit updat chang modif put patch renam",
    "REMOVE": "remov clear delet unset unassign eras drop",
    "SHOW": "show shown display view list render appear read get fetch load name",
    "NONE": "empty null blank unassign unattend optional unset",
    "CHOOSE": "choos pick select option offer dropdown",
    "ERROR": "error exception fail invalid reject unknown notfound",
    "SORT": "sort order ascend descend",
    "FILTER": "search filter query",
    "PAGE": "pagin pagination pages",
    "PERSIST": "persist save store record keep",
    "LINK": "attend assign associat",
    # What a screen or a request starts as: "the initial grid state" against a spec that
    # `opens on the first page` and asserts `DEFAULT_OWNER_PAGE_QUERY` (run 8).
    "INITIAL": "initial default open start",
}
_CONCEPT_OF: dict[str, set] = {}
for _c, _ws in CONCEPTS.items():
    for _w in _ws.split():
        _CONCEPT_OF.setdefault(_w, set()).add(_c)
_CONCEPT_ROOTS = sorted((r, frozenset(cs)) for r, cs in _CONCEPT_OF.items() if len(r) >= 5)

#: A line that checks something: an assertion call or an assertion helper named for one
#: (`expect_pet_visit_list_shows_no_vet(...)`, `assertThat`, `verify`), a Gherkin `Then`
#: or the `And` under it, a matcher. A prefix, not a word: the helper's name is where a
#: suite says what it pins.
_ASSERT_LINE = re.compile(r"(?<![A-Za-z])(expect|assert|verify|should|then\b|and\b|"
                          r"tobe|toequal|tohave|tocontain|isequalto|isnull|isnotnull|"
                          r"andexpect|jsonpath|contains)", re.I)


def assertion_lines(body: list[str]) -> list[str]:
    """The lines of a test body that check something, a fluent chain (`assertThat(x)` then
    `.extracting(…)` then `.isNull()` on the lines under it) counted whole."""
    out, chained = [], False
    for ln in body:
        st = ln.strip()
        if _ASSERT_LINE.search(st) or (chained and st.startswith(".")):
            out.append(ln)
            chained = True
        else:
            chained = False
    return out


def stem(w: str) -> str:
    """A light stemmer: plurals, -ing, -ed, a final -e. Enough that `booking`, `booked` and
    `book` meet, and `create`, `created` and `creating` do."""
    w = w.lower()
    if w.endswith("'s"):
        w = w[:-2]
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 4 and w.endswith("sses"):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    for suf in ("ing", "ed"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            w = w[:-len(suf)]
            if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "lsz":
                w = w[:-1]
            break
    if len(w) > 6 and w.endswith("able"):
        w = w[:-4]                      # sortable → sort, editable → edit
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return w


def _concepts(st: str, word: str) -> set:
    """The concepts a stem belongs to: by exact root, or — for roots of five letters or
    more — by prefix, so `paginat` (paginated) is ≈page like `pagin` is."""
    out = set(_CONCEPT_OF.get(st, ())) | set(_CONCEPT_OF.get(word, ()))
    for root, cs in _CONCEPT_ROOTS:
        if st.startswith(root):
            out |= cs
    return out


def words(text: str) -> list[str]:
    """The words of a sentence or of code: identifiers split on case and on `_`/`-`,
    lower-cased, stop words out. Numbers stay — a page size of 5 is a word."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    out = []
    for w in re.findall(r"[A-Za-z]+|\d+", text):
        lw = w.lower()
        if lw in CONCEPT_ONLY:
            out.append(lw)
            continue
        if lw in STOP or (len(lw) < 2 and not lw.isdigit()):
            continue
        out.append(lw)
    return out


def groups(text: str) -> list[frozenset]:
    """One group per distinct word: its stem and every concept it belongs to. A sentence
    word matches a test when any member of its group is in the test, and counts once —
    `booked` is `book` and ≈create, not two pieces of evidence."""
    out, seen = [], set()
    for w in words(text):
        if w in CONCEPT_ONLY:
            g = frozenset({CONCEPT_ONLY[w]})
        else:
            st = stem(w)
            if st in STOP:
                continue
            g = frozenset({st} | _concepts(st, w))
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out


def terms(text: str) -> set[str]:
    """Every stem and concept of a text, flat — how a test is read."""
    return set().union(*groups(text)) if text else set()


def _literals(text: str) -> set[str]:
    """Quoted strings and routes, verbatim (lower-cased): `"Unknown"`, `/api/owners`."""
    lits = {m.lower() for m in re.findall(r"[\"“']([^\"”']{2,40})[\"”']", text or "")}
    lits |= {m.lower() for m in re.findall(r"(?<!\w)(/[\w{}\-./]+)", text or "")}
    return lits


def _changed_line_text(root: Path, hits: dict, cache: dict) -> str:
    """The source of the changed lines a test's coverage ran — what it executed of this PR."""
    parts = []
    for f in sorted(hits):
        if f not in cache:
            try:
                cache[f] = (root / f).read_text(encoding="utf-8").splitlines()
            except OSError:
                cache[f] = []
        src = cache[f]
        parts.extend(src[n - 1] for n in hits[f] if 1 <= n <= len(src))
    return "\n".join(parts)


#: A link is made at or above this score; a sentence whose best test scores under it goes to
#: the model with its candidates. Named, because these are the numbers the agreement
#: measurement in `test_semcov.py` is about.
LINK_AT = 0.40
#: A test scoring under this is not even a candidate.
CANDIDATE_AT = 0.10
#: At most this many links per sentence — a generic sentence must not claim the whole card.
MAX_LINKS = 8
#: …and only tests within this share of the sentence's best score.
NEAR_BEST = 0.6
#: How many candidates a sentence takes to the model.
MAX_CANDIDATES = 8
#: The fewest words a clause needs to be scored on its own.
CLAUSE_MIN = 3
#: Where in the test a shared word was found, and what that is worth. The name is the
#: test's own claim; an assertion line pins; the rest of the body only mentions; a changed
#: line the test's coverage ran is evidence it went there, not that it checked anything.
WHERE = (("title", 1.0, "name"), ("asserts", 0.55, "assert"), ("body", 0.3, "body"),
         ("cov", 0.3, "covered change"))
#: A word shared with this share of the card or more pins nothing on its own.
RARE_SHARE = 0.35
#: A test the branch wrote or edited was written for this ticket; one it did not touch, and
#: one that only passes through the change (`coverage_join`'s `aimed`), mostly was not.
#: A test edited only through a helper it calls was adapted to the change, not written for
#: the ticket (run 10's AddVisitApiTest: its helper learned to walk pages) — the prior of
#: the untouched, so the label does not move the pairing.
PRIOR = {"new": 1.0, "changed": 1.0, "helper": 0.5, "unchanged": 0.5, "deleted": 0.5}
PASSING_THROUGH = 0.75


def test_documents(rows: list[dict], root: Path) -> dict:
    """Per test: `{"title": terms, "body": terms, "cov": terms, "asserts": terms,
    "lits": set, "prior": float, "body_text": str, "from": int}` — what the pairing reads."""
    docs, cache = {}, {}
    for r in rows:
        start, body = test_source(root, r["file"], r["line"])
        text = "\n".join(body)
        stem_name = Path(r["file"]).name.split(".")[0]
        assert_text = "\n".join(assertion_lines(body))
        docs[r["id"]] = {
            "title": terms(r["title"]) | terms(stem_name),
            "body": terms(text),
            "asserts": terms(assert_text),
            "cov": terms(_changed_line_text(root, r.get("hits") or {}, cache)),
            "lits": _literals(text) | _literals(r["title"]),
            "prior": PRIOR.get(r.get("status"), 0.6) * (1.0 if r.get("aimed", True)
                                                         else PASSING_THROUGH),
            "body_text": text, "from": start,
        }
    return docs


def _idf(docs: dict):
    n = max(len(docs), 1)
    df: dict[str, int] = {}
    # Counted over what a test *claims* — its name and its assertion lines — not over its
    # whole body: a changed line every test runs through (`vet` on a PR about vets) is in
    # every body, and would make the one word the ticket is about read as noise.
    for d in docs.values():
        for t in d["title"] | d["asserts"]:
            df[t] = df.get(t, 0) + 1
    return {t: math.log(1 + n / c) for t, c in df.items()}, df, n


def _label(t: str) -> str:
    """A concept reads `≈none`; a word reads as itself."""
    return f"≈{t.lower()}" if t.isupper() else t


def _score(unit: list, s_lits: set, d: dict, idf: dict, df: dict, n: int):
    """`(score, strength, evidence)` of one sentence (or clause) against one test.

    `unit` is the sentence's word groups (`groups`). Each group the test shares counts
    once, at the best place it was found (`WHERE`), weighted by the rarest of its members
    there; the score is the share of the sentence's weight the test carries, times the
    test's prior. Zero unless two groups are shared and one of them is rare on the card and
    in the test's name or an assertion — or a literal is shared verbatim: two tests and a
    ticket all saying `visit` pair nothing."""
    known = [g for g in unit if any(t in idf for t in g)]
    if not known:
        return 0.0, "", []
    total = sum(max(idf[t] for t in g if t in idf) for g in known)
    got, ev, rare, pinned, shared, used = 0.0, [], False, False, 0, set()
    title_rare = False
    for g in known:
        for key, w, where in WHERE:
            # A word of the test answers one word of the sentence: `not … blank` against
            # a test's one `null` is one shared idea, not two.
            hit = sorted(t for t in g if t in d[key] and t in idf and t not in used)
            if not hit:
                continue
            t = max(hit, key=lambda x: (idf[x], x))
            used.add(t)
            got += idf[t] * w
            shared += 1
            ev.append(f"{_label(t)} ({where})")
            if key in ("title", "asserts"):
                pinned = True
                if df.get(t, n) <= max(1.0, RARE_SHARE * n):
                    rare = True
                    title_rare = title_rare or key == "title"
            break
    score = got / total
    for lit in sorted(s_lits & d["lits"]):
        score += 0.15
        ev.append(f'"{lit}" (literal)')
        rare = pinned = True
        shared += 2
    # One shared word is enough only for a short sentence — three words to give or fewer —
    # and only when that word is rare and in the test's own name: "sortable by any column"
    # against `City sort uses the full tie chain`.
    if not rare or (shared < 2 and not (len(known) <= 3 and title_rare)):
        return 0.0, "", []
    return min(score * d["prior"], 1.0), ("asserted" if pinned else "exercised"), ev


def _clauses(text: str) -> list[str]:
    """The parts of a sentence a test could pin one at a time: split on commas, colons,
    semicolons and a coordinating `and`/`but`, keeping parts of two words or more."""
    return [c.strip() for c in re.split(
        r",\s*(?:and|but|or)?\s*|;\s*|:\s*|\s+and\s+|\s+but\s+", text)
        if len(words(c)) >= 2]


def _best(units: list[set], lits: set, d: dict, idf, df, n):
    """The test's score against the whole sentence or its best clause, whichever is higher:
    a long sentence is not a weaker claim, it is several claims."""
    best = (0.0, "", [])
    for u in units:
        got = _score(u, lits, d, idf, df, n)
        if got[0] > best[0]:
            best = got
    return best


def match(sentences: list[dict], rows: list[dict], root: Path,
          docs: dict | None = None) -> dict:
    """The scripted pairing: `{"decided": [mapping entries], "open": {sid: [candidate test
    ids, best first]}, "candidates": {sid: [the best-scoring test ids]}}`.

    A decided entry's links are *candidates* too, not proof: they were made on shared words,
    and "sortable by any column" shares `grid` and `sort` with a test that checks the page
    size. `merge` shows them as unconfirmed until the model has confirmed or rejected each
    one (`model_input` puts every one of them in front of it).

    A sentence under an *Out of scope* heading is `n/a`. Otherwise each test is scored
    against the sentence and each of its clauses (`_score`, `_best`), and the sentence is
    linked to every test at or over LINK_AT within NEAR_BEST of the best, at most
    MAX_LINKS. A sentence with no such test is *open* — the model's to decide, with the
    tests over CANDIDATE_AT as its candidates. Coverage is read off the links: asserted
    links make it `covered`, unless a clause has no asserted link of its own (`partial`);
    links that only run through it make it `exercised`."""
    docs = docs if docs is not None else test_documents(rows, root)
    idf, df, n = _idf(docs)
    decided, open_, cands = [], {}, {}
    for s in sentences:
        if _OUT_OF_SCOPE.search(s.get("section") or ""):
            decided.append({"id": s["id"], "coverage": "n/a", "tests": [], "by": "script",
                            "gap": "Out of scope, per the ticket — nothing to cover.",
                            "gapKind": "requirement"})
            continue
        clauses = _clauses(s["text"])
        # A clause is scored on its own only when it can carry a claim of its own: three
        # words or more. Two — "the vet is shown" — match every test that names a vet.
        units = [groups(s["text"])] + [groups(c) for c in clauses
                                       if len(clauses) > 1 and len(groups(c)) >= CLAUSE_MIN]
        lits = _literals(s["text"])
        scored = []
        for r in rows:
            sc, strength, ev = _best(units, lits, docs[r["id"]], idf, df, n)
            if sc > 0:
                scored.append((round(sc, 6), r["id"], strength, ev))
        scored.sort(key=lambda x: (-x[0], x[1]))
        # Candidates *beyond* the links: the pool used to be the best eight overall, so a
        # sentence with six links took two others to the model, and run 8's e2e scenario
        # "Searching with an empty last name shows the first page of every owner" (scoring
        # 0.48 on "an omitted or empty lastName SHALL match all owners") never reached it.
        cands[s["id"]] = [x[1] for x in scored
                          if x[0] >= CANDIDATE_AT][:MAX_CANDIDATES + MAX_LINKS]
        best = scored[0][0] if scored else 0.0
        near = [x for x in scored if x[0] >= LINK_AT and x[0] >= NEAR_BEST * best]
        links = near[:MAX_LINKS]
        if len(near) > MAX_LINKS:
            # More tests tie for this sentence than it may claim: the script cannot tell
            # them apart, so the choice among them is the model's.
            open_[s["id"]] = [x[1] for x in near][:MAX_CANDIDATES + 4]
            continue
        if not links:
            open_[s["id"]] = [x[1] for x in scored if x[0] >= CANDIDATE_AT][:MAX_CANDIDATES]
            continue
        tests = [{"id": tid, "strength": strength, "by": "script",
                  "why": "shares " + ", ".join(dict.fromkeys(ev))[:160],
                  "evidence": list(dict.fromkeys(ev))} for sc, tid, strength, ev in links]
        asserted = [t for t in tests if t["strength"] == "asserted"]
        entry = {"id": s["id"], "tests": tests, "by": "script"}
        if not asserted:
            entry["coverage"] = "exercised"
            entry["gap"] = "The tests paired with this run through it; none asserts it."
            entry["gapKind"] = "tests"
        else:
            loose = []
            if len(clauses) > 1:
                for c in clauses:
                    if not any(_score(groups(c), set(), docs[t["id"]], idf, df, n)[1]
                               == "asserted" for t in asserted):
                        loose.append(c)
            if loose:
                entry["coverage"] = "partial"
                entry["gap"] = "No paired test asserts " + "; ".join(f"“{c}”" for c in loose)
                entry["gapKind"] = "tests"
            else:
                entry["coverage"] = "covered"
        decided.append(entry)
    return {"decided": decided, "open": open_, "candidates": cands}


# --- the model's half -------------------------------------------------------------------

def scripted_links(scripted: dict) -> dict:
    """`{sentence id: [test id, …]}` — the links the script made, which the model must
    confirm or reject one by one."""
    return {e["id"]: [t["id"] for t in e["tests"]]
            for e in scripted["decided"] if e["coverage"] != "n/a"}


def model_input(ticket: dict, sentences: list[dict], rows: list[dict], scripted: dict,
                docs: dict, decisions: list[dict] | None = None) -> dict:
    """What the cheap model is asked, in one call: every sentence that makes a claim — each
    with the links the script made on shared words (`scripted`, to confirm or reject) and
    the other tests that scored best (`candidates`, to read) — plus the scope decisions the
    branch recorded, and the bodies of every test named. Out-of-scope sentences are not
    asked about: the ticket itself already said there is nothing to cover."""
    by_id = {s["id"]: s for s in sentences}
    rows_by = {r["id"]: r for r in rows}
    links = {e["id"]: e for e in scripted["decided"]}
    cands = scripted.get("candidates") or {}
    asked, want = [], []
    for s in sentences:
        sid = s["id"]
        e = links.get(sid)
        if e is not None and e["coverage"] == "n/a":
            continue
        made = [{"id": t["id"], "why": t.get("why") or ""} for t in (e or {}).get("tests") or []]
        made_ids = {t["id"] for t in made}
        pool = scripted["open"].get(sid) if e is None else cands.get(sid)
        others = [t for t in (pool or []) if t not in made_ids][:MAX_CANDIDATES]
        item = {"id": sid, "text": by_id[sid]["text"],
                "section": by_id[sid].get("section") or "",
                "scripted": made, "candidates": others}
        if by_id[sid].get("requirement"):
            item["requirement"] = by_id[sid]["requirement"]
            item["scenarios"] = by_id[sid].get("scenarios") or []
        asked.append(item)
        for t in [m["id"] for m in made] + others:
            if t not in want and t in rows_by:
                want.append(t)
    # The reply's skeleton, every verdict slot already in place: a cheap model fills in a
    # form far more reliably than it builds one from rules, and the first real run on the
    # demo PR came back with most `review` lists simply missing.
    skeleton = [{"id": x["id"], "coverage": "", "tests": [],
                 "review": [{"id": t["id"], "verdict": "", "why": ""} for t in x["scripted"]]}
                for x in asked]
    return {
        "schema": INPUT_VERSION,
        "ticket": {"number": ticket.get("number"), "title": ticket.get("title", "")},
        "sentences": asked,
        "answer": {"schema": SCHEMA_VERSION, "sentences": skeleton},
        # Only what the model judges by; where each was recorded is the page's business.
        "decisions": [{"id": d["id"], "text": d["text"]} for d in decisions or []],
        "context": [s["text"] for s in sentences],
        "tests": [{"id": t, "title": rows_by[t]["title"], "kind": CATS[rows_by[t]["cat"]],
                   # An edit through a helper is an edit; the model's vocabulary is three words.
                   "status": {"helper": "changed"}.get(rows_by[t]["status"],
                                                      rows_by[t]["status"]),
                   "body": docs[t]["body_text"]} for t in want],
    }


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def problems(doc, allowed_sentences=None, allowed_tests=None, scripted=None,
             decisions=None) -> list[str]:
    """Every way a mapping departs from the schema, or from the facts it is about.

    The schema is checked by `review_points_schema.problems` — the skill's own small
    draft-2020-12 checker, so no `jsonschema` is needed on a trainee's laptop. Then what a
    schema cannot say: no sentence twice, a sentence id this ticket has, a test id from the
    list the model was given, a coverage word its own links can stand behind — and, when
    `scripted` (`{sid: [test ids]}`) is given, a verdict on every link the script made, each
    confirmed link kept and each rejected one dropped. `narrowed` names the recorded decision
    it rests on, one of `decisions` (ids) when those are given."""
    import review_points_schema
    out = review_points_schema.problems(doc, load_schema())
    if out or not isinstance(doc, dict):
        return out
    seen = set()
    for i, s in enumerate(doc["sentences"]):
        at = f"$.sentences[{i}]"
        if s["id"] in seen:
            out.append(f"{at}: sentence {s['id']} appears twice")
        seen.add(s["id"])
        if allowed_sentences is not None and s["id"] not in allowed_sentences:
            out.append(f"{at}: {s['id']} is not a sentence it was asked about")
        for j, t in enumerate(s["tests"]):
            if allowed_tests is not None and t["id"] not in allowed_tests:
                out.append(f"{at}.tests[{j}]: {t['id']} is not one of the tests listed")
        strengths = {t["strength"] for t in s["tests"]}
        cov = s["coverage"]
        if cov in ("missing", "n/a") and s["tests"]:
            out.append(f"{at}: `{cov}` with tests paired — a sentence nothing covers has no "
                       "tests")
        if cov in ("covered", "partial") and "asserted" not in strengths:
            out.append(f"{at}: `{cov}` needs at least one asserted test")
        if cov == "exercised" and (not s["tests"] or "asserted" in strengths):
            out.append(f"{at}: `exercised` means tests run through it and none asserts it")
        if cov == "narrowed":
            if not s.get("decision"):
                out.append(f"{at}: `narrowed` names the recorded decision it rests on "
                           "(`decision`)")
            elif decisions is not None and s["decision"] not in decisions:
                out.append(f"{at}: decision {s['decision']} is not one of the decisions listed")
        kept = {t["id"] for t in s["tests"]}
        verdicts: dict = {}
        for j, v in enumerate(s.get("review") or []):
            if v["id"] in verdicts:
                out.append(f"{at}.review[{j}]: {v['id']} is judged twice")
            verdicts[v["id"]] = v["verdict"]
            if scripted is not None and v["id"] not in (scripted.get(s["id"]) or []):
                out.append(f"{at}.review[{j}]: {v['id']} is not a link the script made for "
                           "this sentence")
            if v["verdict"] == "confirm" and v["id"] not in kept:
                out.append(f"{at}.review[{j}]: {v['id']} is confirmed but not in `tests`")
            if v["verdict"] == "reject" and v["id"] in kept:
                out.append(f"{at}.review[{j}]: {v['id']} is rejected but still in `tests`")
        if scripted is not None:
            for tid in scripted.get(s["id"]) or []:
                if tid not in verdicts:
                    out.append(f"{at}: the script's link to {tid} is neither confirmed nor "
                               "rejected")
    return out


def salvage(doc, allowed_sentences=None, allowed_tests=None, scripted=None,
            decisions=None) -> tuple[dict | None, list[str], list[str]]:
    """`(the answer to install or None, every problem found, the sentence ids dropped)`.

    A problem that belongs to one sentence — a verdict missing, an id it was not shown —
    costs that sentence, not the whole paid answer: the sentence is dropped, so the page
    shows its scripted links as unconfirmed (never as covered), and the rest is installed.
    A problem with the document itself refuses it whole.

    One slip is repaired rather than dropped, because the repair can only make the answer
    *less* flattering: a link the model confirmed but forgot to list in `tests` is added as
    `exercised` — confirming says the test at least runs the claim, never that it asserts
    it, and `covered` still needs an asserted test the model named itself."""
    if isinstance(doc, dict) and isinstance(doc.get("sentences"), list):
        doc = json.loads(json.dumps(doc))
        for e in doc["sentences"]:
            if not isinstance(e, dict) or not isinstance(e.get("tests"), list):
                continue
            listed = {t.get("id") for t in e["tests"] if isinstance(t, dict)}
            for v in e.get("review") or []:
                if isinstance(v, dict) and v.get("verdict") == "confirm" \
                        and v.get("id") not in listed:
                    e["tests"].append({"id": v["id"], "strength": "exercised",
                                       **({"why": v["why"]} if v.get("why") else {})})
                    listed.add(v["id"])
    found = problems(doc, allowed_sentences, allowed_tests, scripted, decisions)
    if not found:
        return doc, [], []
    bad_idx = set()
    for p in found:
        m = re.match(r"\$\.sentences\[(\d+)\]", p)
        if not m:
            return None, found, []
        bad_idx.add(int(m.group(1)))
    kept = [e for i, e in enumerate(doc["sentences"]) if i not in bad_idx]
    dropped = [doc["sentences"][i].get("id", f"#{i}") for i in sorted(bad_idx)]
    if not kept:
        return None, found, dropped
    out = {**doc, "sentences": kept}
    if problems(out, allowed_sentences, allowed_tests, scripted, decisions):
        return None, found, dropped
    return out, found, dropped


# --- downgrade only: a script's sanity rule and a second read ---------------------------
#
# Eval run 8's Haiku answer called 25 of 27 sentences `covered` and none `partial`:
# "authorization … and chatbot/MCP contracts SHALL remain unchanged" stood on a test that
# lists owners, "Bootstrap styling remains" on two DOM checks, and one `confirm` gave a reason
# its test does not have. The model stays cheap; what it says is now *checked*, by two
# passes that can only take away. `sanity` is free: a link whose test shares no specific
# word, route or identifier with its sentence cannot be evidence for it. `check_input` /
# `apply_check` are a second cheap call that re-reads every kept link against the test body,
# must quote the line that asserts the claim (and the quote is looked up in the real body),
# and whose verdicts are applied only where they lower a link or a sentence.

CHECK_VERSION = "test-mapping-check/1"
CHECK_INPUT_VERSION = "test-mapping-check-input/1"
#: Coverage words in the order a downgrade walks them. `narrowed` and `n/a` are not on it:
#: they are facts about the requirement, and neither pass touches them.
COV_RANK = {"missing": 0, "exercised": 1, "partial": 2, "covered": 3}
#: What the second read is asked about: every sentence a link stands behind.
CHECKED = ("covered", "partial", "exercised")
#: A word on nearly every test of the card is the PR's own vocabulary — `owner` on 57 of
#: run 8's 62 — and sharing it says nothing about the sentence's subject. Nearly every, not
#: most: at half the card, `page` (49 of 62) went too, and with it a true link of a
#: pagination sentence to a test that sends `page=x` and expects a 400.
DOMAIN_COMMON = 0.9
#: The shortest quoted assertion line accepted as one.
QUOTE_MIN = 6


def _downgrade(e: dict, **rec) -> None:
    e.setdefault("downgrades", []).append({k: v for k, v in rec.items() if v not in (None, "")})


def _drop_link(e: dict, tid: str, by: str, why: str) -> None:
    """Take a link off a sentence, and if it was a scripted link the model confirmed, turn
    the verdict into a rejection with this reason — a confirmed link must stay in `tests`."""
    t = next((x for x in e["tests"] if x["id"] == tid), None)
    if t is None:
        return
    e["tests"] = [x for x in e["tests"] if x["id"] != tid]
    for v in e.get("review") or []:
        if v["id"] == tid and v["verdict"] == "confirm":
            v["verdict"], v["why"] = "reject", f"{by}: {why}"
    _downgrade(e, id=tid, by=by, why=why, **{"from": t["strength"], "to": "dropped"})


def _settle(e: dict, by: str, why: str, before: str) -> None:
    """The coverage word the remaining links can stand behind — never higher than it was."""
    cov = e["coverage"]
    if cov not in COV_RANK:
        return
    strengths = {t["strength"] for t in e["tests"]}
    if cov in ("covered", "partial") and "asserted" not in strengths:
        cov = "exercised" if e["tests"] else "missing"
    if cov == "exercised" and not e["tests"]:
        cov = "missing"
    if COV_RANK[cov] > COV_RANK.get(before, 3):
        cov = before
    if cov != e["coverage"]:
        e["coverage"] = cov
    if e["coverage"] != before:
        _downgrade(e, by=by, why=why, **{"from": before, "to": e["coverage"]})
        if not e.get("gap") or e.get("gapKind") == "requirement":
            e["gap"] = why
            e["gapKind"] = "tests"


def _specific(text: str, docs: dict) -> tuple[set, set]:
    """`(stems, literals)` of a sentence that can say what it is about: its stems (no
    concepts) minus the PR's own vocabulary (`DOMAIN_COMMON`), and its routes and quoted
    strings verbatim."""
    n = max(len(docs), 1)
    df: dict[str, int] = {}
    for d in docs.values():
        for t in d["title"] | d["body"] | d["asserts"]:
            df[t] = df.get(t, 0) + 1
    stems = {t for g in groups(text) for t in g if not t.isupper()}
    return {t for t in stems if df.get(t, 0) <= max(1, DOMAIN_COMMON * n)}, _literals(text)


def sanity(doc: dict, texts: dict, docs: dict) -> tuple[dict, int]:
    """`(the answer, links dropped)`: every link of a `covered`/`partial`/`exercised`
    sentence whose test shares no specific stem, route or quoted string with the sentence
    is dropped, with the reason, and the sentence settles to what is left. Never adds."""
    doc = json.loads(json.dumps(doc))
    dropped = 0
    for e in doc.get("sentences") or []:
        before = e.get("coverage")
        if before not in CHECKED or e["id"] not in texts:
            continue
        stems, lits = _specific(texts[e["id"]], docs)
        for t in list(e["tests"]):
            d = docs.get(t["id"])
            if d is None:
                continue
            words_ = d["title"] | d["body"] | d["asserts"]
            if stems & words_ or lits & d["lits"]:
                continue
            _drop_link(e, t["id"], "script", "the test shares no word, route or identifier "
                       "of the sentence's subject with it — only the PR's common vocabulary")
            dropped += 1
        _settle(e, "script", "No remaining paired test shares the sentence's subject.", before)
    return doc, dropped


def check_input(doc: dict, asked: dict) -> dict:
    """What the second read is asked: every `covered`/`partial`/`exercised` sentence of the
    first answer with the links it kept, the bodies of those tests, and the reply laid out."""
    by_id = {s["id"]: s for s in asked["sentences"]}
    bodies = {t["id"]: t for t in asked["tests"]}
    items, want = [], []
    for e in doc.get("sentences") or []:
        if e.get("coverage") not in CHECKED or not e["tests"] or e["id"] not in by_id:
            continue
        s = by_id[e["id"]]
        item = {"id": e["id"], "text": s["text"], "coverage": e["coverage"],
                "links": [{"id": t["id"], "strength": t["strength"], "why": t.get("why") or ""}
                          for t in e["tests"]]}
        if s.get("scenarios"):
            item["scenarios"] = s["scenarios"]
        items.append(item)
        want += [t["id"] for t in e["tests"] if t["id"] in bodies and t["id"] not in want]
    return {"schema": CHECK_INPUT_VERSION, "sentences": items,
            "answer": {"schema": CHECK_VERSION, "sentences": [
                {"id": x["id"], "claim": "", "unproven": "",
                 "links": [{"id": t["id"], "verdict": "", "line": "", "why": ""}
                           for t in x["links"]]} for x in items]},
            "tests": [{k: bodies[t][k] for k in ("id", "title", "kind", "body")} for t in want]}


def check_problems(chk) -> list[str]:
    """Why a second read cannot be used at all. Junk *inside* a usable one — an id it was
    not asked about, a verdict it made up — is skipped, not refused: the read can only
    lower things, so ignoring a line of it is always the safe direction."""
    if not isinstance(chk, dict):
        return ["the reply holds no JSON object"]
    if chk.get("schema") != CHECK_VERSION:
        return [f"`schema` is not {CHECK_VERSION}"]
    if not isinstance(chk.get("sentences"), list):
        return ["`sentences` is not a list"]
    return []


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").rstrip(";")


def quoted_in(line: str, body: str) -> bool:
    """Is `line` really in `body` — whitespace aside, all of it? The second read must copy
    the assertion that proves a claim; one it paraphrased or invented is not a quote. A
    fluent chain the body breaks over two lines (`perform(…)` / `.andExpect(…)`) may come
    back joined on one, which is the same code."""
    q = _norm(line)
    return len(q) >= QUOTE_MIN and q in _norm(body)


def apply_check(doc: dict, chk: dict, asked: dict) -> tuple[dict, dict]:
    """`(the answer, {"links": lowered, "sentences": lowered})` — the second read's verdicts
    applied where, and only where, they lower something: `unrelated` drops a link, `runs`
    (or an `asserts` whose `line` is not in the test's body) turns `asserted` into
    `exercised`, `claim: part` turns `covered` into `partial`, `claim: none` leaves no
    link asserted. Nothing is ever added or raised."""
    doc = json.loads(json.dumps(doc))
    bodies = {t["id"]: t.get("body") or "" for t in asked["tests"]}
    verdicts = {e.get("id"): e for e in chk.get("sentences") or [] if isinstance(e, dict)}
    lowered = {"links": 0, "sentences": 0}
    for e in doc.get("sentences") or []:
        before = e.get("coverage")
        v = verdicts.get(e["id"])
        if before not in CHECKED or not isinstance(v, dict):
            continue
        links = {x.get("id"): x for x in v.get("links") or [] if isinstance(x, dict)}
        for t in list(e["tests"]):
            x = links.get(t["id"])
            if not x:
                continue
            why = str(x.get("why") or "").strip()
            verdict = x.get("verdict")
            if verdict == "asserts" and not quoted_in(str(x.get("line") or ""),
                                                      bodies.get(t["id"], "")):
                verdict = "runs"
                why = ("its quoted assertion is not in the test's body"
                       + (f" ({why})" if why else ""))
            if verdict == "unrelated":
                _drop_link(e, t["id"], "second read", why or "does not touch the subject")
                lowered["links"] += 1
            elif verdict == "runs" and t["strength"] == "asserted":
                t["strength"] = "exercised"
                t["why"] = f"second read: runs it, asserts none of it — {why}".rstrip(" —")
                _downgrade(e, id=t["id"], by="second read", why=why or "asserts none of it",
                           **{"from": "asserted", "to": "exercised"})
                lowered["links"] += 1
        claim = v.get("claim")
        unproven = str(v.get("unproven") or "").strip()
        if claim == "none":
            for t in e["tests"]:
                if t["strength"] == "asserted":
                    t["strength"] = "exercised"
                    _downgrade(e, id=t["id"], by="second read",
                               why=unproven or "asserts none of the claim",
                               **{"from": "asserted", "to": "exercised"})
                    lowered["links"] += 1
        if claim == "part" and e["coverage"] == "covered":
            e["coverage"] = "partial"
            e["gap"], e["gapKind"] = unproven or "A second read found part of it unasserted.", \
                "tests"
        _settle(e, "second read", unproven or "A second read found no listed test asserting "
                "this sentence's claim.", before)
        lowered["sentences"] += e["coverage"] != before
    return doc, lowered


def load_model_mapping(review: Path) -> dict | None:
    """`test-mapping.json`, if it is there and valid; a broken one is said and ignored."""
    p = review / MAPPING
    if not p.is_file():
        return None
    doc = _read_json(p)
    bad = problems(doc) if doc is not None else ["not JSON"]
    if bad:
        print(f"[semcov] {p} does not match {SCHEMA_PATH.name} and is ignored: "
              + "; ".join(bad[:3]), file=sys.stderr)
        return None
    return doc


#: Said on a sentence the script paired on shared words and no model has read yet.
UNCONFIRMED_GAP = ("Paired on shared words only — no model has read these tests to confirm "
                   "one asserts this sentence's claim, so it is not shown as covered. Run the "
                   "🤖 beside the Tests tab.")


def merge(sentences: list[dict], scripted: dict, model: dict | None,
          decisions: list[dict] | None = None) -> list[dict]:
    """One entry per ticket sentence, in reading order.

    A scripted link is a *candidate*: shared words say a test is worth reading, not that it
    asserts the claim. So a sentence is coloured by the model's answer wherever there is one
    — the links it confirmed (keeping the script's evidence beside them), any test it added,
    the coverage it read off the bodies, the links it rejected and why. Where the model has
    not answered, the script's links stand as `unconfirmed`, never as covered. Sentences the
    ticket itself puts out of scope stay `n/a`; one nobody paired is `unmapped`."""
    script = {e["id"]: e for e in scripted["decided"]}
    answer = {e["id"]: e for e in (model or {}).get("sentences") or []}
    said = {d["id"]: d["text"] for d in decisions or []}
    refs = {d["id"]: {k: d[k] for k in ("where", "quote", "href", "path") if d.get(k)}
            for d in decisions or []}
    out = []
    for s in sentences:
        sc = script.get(s["id"])
        if sc is not None and sc["coverage"] == "n/a":
            out.append(sc)
            continue
        if s["id"] in answer:
            e = json.loads(json.dumps(answer[s["id"]]))
            e["by"] = "model"
            made = {t["id"]: t for t in (sc or {}).get("tests") or []}
            verdicts = {v["id"]: v for v in e.pop("review", None) or []}
            for t in e["tests"]:
                t["by"] = "model"
                if t["id"] in made and made[t["id"]].get("evidence"):
                    t["evidence"] = made[t["id"]]["evidence"]
            rejected = [{"id": v["id"], "why": v.get("why") or ""}
                        for v in verdicts.values() if v["verdict"] == "reject"]
            # A link a downgrade-only pass took off that was never a scripted one (the
            # model added it from the candidates): shown with the others it lost, and why.
            rejected += [{"id": d["id"], "why": f"{d['by']}: {d.get('why') or ''}".rstrip(": ")}
                         for d in e.get("downgrades") or []
                         if d.get("to") == "dropped" and d["id"] not in verdicts]
            if rejected:
                e["rejected"] = rejected
            if e.get("decision") in said:
                e["decisionText"] = said[e["decision"]]
                if refs.get(e["decision"]):
                    e["decisionRef"] = refs[e["decision"]]
            out.append(e)
        elif sc is not None:
            e = json.loads(json.dumps(sc))
            e["coverage"], e["gap"], e["gapKind"] = "unconfirmed", UNCONFIRMED_GAP, "tests"
            out.append(e)
        else:
            out.append({"id": s["id"], "coverage": "unmapped", "tests": [], "by": "script"})
    return out


# --- 4. the page ------------------------------------------------------------------------

def _when(iso: str) -> str:
    try:
        d = datetime.datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return ""
    return f"opened on {d:%b} {d.day}, {d.year}"


#: How much of a decision's own words a hover quotes before cutting: a tooltip is one glance.
DECISION_TIP_MAX = 90


def decision_name(entry: dict) -> str:
    """`proposal.md:86 “Sorting is limited to Name and City…”` — the decision a narrowed
    sentence rests on, named the way a reader can go and find it; "" when the mapping named
    none the build could resolve."""
    ref = entry.get("decisionRef") or {}
    quote = ref.get("quote") or ""
    if len(quote) > DECISION_TIP_MAX:
        quote = quote[:DECISION_TIP_MAX].rsplit(" ", 1)[0].rstrip(",;:—-. ") + "…"
    if ref.get("where") and quote:
        return f"{ref['where']} “{quote}”"
    return ref.get("where") or (f"“{quote}”" if quote else "")


def _sentence_html(s: dict, entry: dict) -> str:
    cov = entry["coverage"]
    inner = _inline(s["md"])
    if cov == "n/a":
        return f'<span class="rm-plain" data-s="{s["id"]}">{inner}</span>'
    tip = COV_LABEL[cov]
    if cov == "narrowed" and decision_name(entry):
        tip += " — by " + decision_name(entry)
    src = ' data-src="model"' if entry.get("by") == "model" else ""
    # The `id` goes last: `class="rm-f" data-s=` is the shape two readers parse
    # (`tests.py:model_pairing`, `reference_pairs`); the id is what the tally jumps to.
    return (f'<span class="rm-f" data-s="{s["id"]}" data-cov="{COV_ATTR[cov]}"{src} '
            f'role="button" tabindex="0" data-tip="{html.escape(tip, quote=True)}" '
            f'id="rm-s-{s["id"]}">{inner}</span>')


#: The one line that says a model read the tests — instead of a 🤖 after every sentence.
AI_NOTE = "🤖 coloured where AI read the tests — hover a sentence to see who paired it"


def _ticket_html(ticket: dict, blocks: list[dict], entries: dict) -> str:
    def para(sents):
        return " ".join(_sentence_html(s, entries[s["id"]]) for s in sents)
    body = []
    for b in blocks:
        k = b["kind"]
        if k == "h":
            body.append(f'<p class="rm-h">{_inline(b["text"])}</p>')
        elif k == "hr":
            body.append('<hr class="rm-hr">')
        elif k == "code":
            body.append(f'<pre class="code"><code>{html.escape(b["text"])}</code></pre>')
        elif k in ("p", "quote"):
            if b["sentences"]:
                body.append(f"<p>{para(b['sentences'])}</p>")
        else:
            start = f' start="{b["start"]}"' if k == "ol" else ""
            items = "".join(f"<li>{para(it['sentences'])}</li>" for it in b["items"])
            body.append(f"<{k}{start}>{items}</{k}>")
    login = ticket.get("author") or ""
    av = (f'<img class="rm-av" src="{ticket["avatar"]}" alt="" width="24" height="24">'
          if ticket.get("avatar") else
          f'<span class="rm-av rm-av-ai" aria-hidden="true">'
          f'{html.escape((login[:1] or "?").upper())}</span>')
    head = (av + f'<span class="rm-who">{html.escape(login)}</span>'
            + f'<span class="rm-when">{html.escape(_when(ticket.get("createdAt", "")))}</span>'
            if login or ticket.get("number") is not None else "")
    # Where the left column's text came from, in plain words, on a muted line of its own
    # under the header strip. It used to sit *in* the strip as "Requirement text: GitHub
    # issue #25, named by content.json pr.ticket" — a file and a key a reviewer never needs,
    # squeezing the author and the date into a four-line column beside it.
    # Who coloured the sentences, said once: a sentence's own hover says whether AI read
    # its tests or the script paired it — a robot after every clause (thirty on eval run 8)
    # made the column unreadable.
    note = (f'<span class="rm-ainote">{" · " if ticket.get("origin") else ""}{AI_NOTE}</span>'
            if any(e.get("by") == "model" for e in entries.values()) else "")
    src = (f'<p class="rm-src">{html.escape(ticket.get("origin") or "")}{note}</p>'
           if ticket.get("origin") or note else "")
    return ('<div class="rm-ticket"><div class="rm-tkhead">' + head + '</div>' + src
            + tally_html(blocks, entries)
            + '<div class="rm-issue">' + "".join(body) + "</div></div>")


#: The states a sentence can be in that are not "a test asserts all of it", worst first,
#: with the word the tally says — the legend's own word, so the two read as one key.
TALLY = (("missing", "missing"), ("partial", "partially"), ("exercised", "executed"),
         ("unconfirmed", "unconfirmed"), ("unmapped", "not paired yet"),
         ("narrowed", "narrowed"))


def tally_html(blocks: list[dict], entries: dict) -> str:
    """One line at the top of the ticket frame counting the sentences that are NOT solid
    green, each count a link that jumps to the first of them (and, in `reqmap.js`, on to
    the next one per press).

    Eval run 10: the top of the column read solid green, and the one `missing` and four
    `partly` sentences took a scroll to find — nothing above the fold said they existed.
    Said when everything is green too, so a reader can tell "none" from "not counted".
    Inside the frame, under its header strip, so the ticket and the card still start on
    one line."""
    order = [s["id"] for s in ticket_sentences(blocks)]
    claims = [entries[sid] for sid in order if sid in entries
              and entries[sid]["coverage"] != "n/a"]
    if not claims:
        return ""
    parts = []
    for cov, word in TALLY:
        hit = [e for e in claims if e["coverage"] == cov]
        if hit:
            tip = ("Jump to it" if len(hit) == 1
                   else "Jump to the first — press again for the next")
            parts.append(f'<a class="rm-lg rm-jump" data-cov="{COV_ATTR[cov]}" '
                         f'href="#rm-s-{hit[0]["id"]}" data-tip="{tip}">'
                         f'{len(hit)} {word}</a>')
    n = len(claims)
    if not parts:
        return (f'<p class="rm-tally" data-all="yes">All {n} claim{"s" if n != 1 else ""} '
                "fully covered by a test.</p>")
    off = n - sum(1 for e in claims if e["coverage"] == "covered")
    return (f'<p class="rm-tally"><span class="rm-tallyt">{off} of {n} claims not fully '
            f'covered:</span> ' + " ".join(parts) + "</p>")


def _sentence_data(entry: dict, rows_by: dict) -> dict:
    groups: dict[str, list] = {}
    for t in entry["tests"]:
        cat = rows_by[t["id"]]["cat"] if t["id"] in rows_by else "unit"
        groups.setdefault(cat, []).append({"id": t["id"], "strength": t["strength"],
                                           "why": t.get("why") or "", "by": t.get("by")
                                           or entry.get("by") or "script"})
    out_groups = []
    for cat in ("e2e", "api", "unit"):
        if cat in groups:
            ts = groups[cat]
            counts = {}
            for t in ts:
                counts[t["strength"]] = counts.get(t["strength"], 0) + 1
            out_groups.append({"cat": cat, "tests": ts,
                               "sum": ", ".join(f"×{n} {k}" for k, n in sorted(counts.items()))})
    d = {"cov": COV_ATTR[entry["coverage"]], "label": COV_LABEL[entry["coverage"]],
         "by": entry.get("by") or "script", "groups": out_groups}
    if entry.get("rejected"):
        # What the model read and turned down, so a reader can see the keyword match that
        # used to paint this sentence green, and why it does not prove it.
        d["rejected"] = [{"id": r["id"], "why": r.get("why") or ""} for r in entry["rejected"]]
    if entry.get("decisionText"):
        d["decision"] = entry["decisionText"]
    ref = entry.get("decisionRef") or {}
    if ref:
        # Named and linked on the sentence's hover and in its box — "narrowed by a
        # recorded decision" sent the reader hunting for which one.
        d["decisionName"] = decision_name(entry)
        for k in ("where", "quote", "href"):
            if ref.get(k):
                d["decision" + k.capitalize()] = ref[k]
    if entry.get("gap"):
        d["gap"] = entry["gap"]
        # A narrowed sentence is a fact about the requirement, whatever the model tagged it.
        d["gapKind"] = ("requirement" if entry["coverage"] == "narrowed"
                        else entry.get("gapKind") or "tests")
    elif entry["coverage"] == "narrowed":
        d["gap"] = ("Not delivered as the sentence says: " + entry.get("decisionText", "a "
                    "scope decision the branch recorded narrows it") + ".")
        d["gapKind"] = "requirement"
    elif entry["coverage"] == "unmapped":
        d["gap"] = ("The script found no test that shares this sentence's words, and no "
                    "model has been asked yet — run the 🤖 beside the Tests tab.")
        d["gapKind"] = "tests"
    return d


#: The covering-tests card, grouped by why a test is on it — the ones about the change
#: first. A test is listed because its own coverage ran a line this PR changed, and on a
#: change to a shared class (an exception advice) that pulls in tests about something else
#: entirely: they go last, each saying on its stamp which lines brought it here.
RANK_LABELS = {
    "0": "Paired with a sentence of the ticket",
    "1": "Written or edited by this branch, paired with no sentence",
    "2": "Untouched and unpaired — run a changed line few other tests run",
    "3": "Untouched and unpaired — only pass through changed code most tests run",
}


#: The two "untouched and unpaired" groups start folded. On run 6 they were 33 of the
#: card's 92 rows — UserTest, SpecialtyTest, VisitDateRangeTest — tests about something
#: else that happen to run a changed line, and listing them open doubled the tab's height.
#: They stay one click away, counted on the button that shows them (`reqmap.js`); the
#: paired and the branch-written groups are never folded.
FOLD_FROM_RANK = 2
FOLD_LABEL = "more tests that only pass through changed code"
#: The hover on a deleted test's location: it opens the base commit, not this checkout.
DELETED_HREF_TIP = "Deleted on this branch — open it as it was at the base commit, on GitHub"


def test_rank(r: dict, paired: set) -> int:
    """0 paired with a sentence; 1 a test the branch wrote, edited or deleted; 2 an
    untouched test aimed at the change (`coverage_join`'s `aimed`); 3 one that only passes
    through changed lines most of its suite runs."""
    if r["id"] in paired:
        return 0
    if r.get("status") in ("new", "changed", "helper", "deleted"):
        return 1
    return 2 if r.get("aimed", True) else 3


def test_why(r: dict) -> str:
    """Why the test is on the card, in a line: the changed lines its own coverage ran, per
    file — `ExceptionControllerAdvice.java 56–58, 70`. Empty without a coverage run."""
    hits = r.get("hits") or {}
    if not hits:
        return ""
    T = _tests_tab()
    files = sorted(hits.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    def lines(ls) -> str:
        runs = T._cov_ranges(ls).split(", ")
        return ", ".join(runs[:3]) + (f" and {len(runs) - 3} more" if len(runs) > 3 else "")
    parts = [f"{Path(f).name} {lines(ls)}" for f, ls in files[:4]]
    if len(files) > 4:
        parts.append(f"{len(files) - 4} more file{'s' if len(files) > 5 else ''}")
    return "its coverage ran changed lines of " + "; ".join(parts)


def render(ticket: dict, blocks: list[dict], rows: list[dict], entries: list[dict],
           root: Path, measured: bool = True, who: str | None = None) -> str:
    """The matrix fragment: same inputs, same bytes."""
    T = _tests_tab()
    by_sid = {e["id"]: e for e in entries}
    rows_by = {r["id"]: r for r in rows}
    paired = {t["id"] for e in entries for t in e["tests"]}
    tests = {}
    for r in rows:
        part = T._cov_part(root, r["file"], r["line"]) if r["status"] != "deleted" else None
        tests[r["id"]] = {"title": r["title"] or r["id"], "cat": r["cat"],
                          "status": r["status"], "parts": [part] if part else [],
                          "rank": test_rank(r, paired), "why": test_why(r)}
        if r["status"] == "deleted" and r.get("baseUrl"):
            tests[r["id"]]["href"] = r["baseUrl"]
            tests[r["id"]]["hrefTip"] = DELETED_HREF_TIP
        if r.get("viaHelper"):
            # Which helper, on the stamp's hover: the row's own source shows no edit.
            tests[r["id"]]["via"] = T.via_helper_tip(r["viaHelper"])
    used = {e["coverage"] for e in entries}
    legend = LEGEND.replace("</div>", "".join(v for k, v in LEGEND_EXTRA.items()
                                              if k in used) + "</div>")
    data = {"cats": CATS, "tests": tests, "ranks": RANK_LABELS,
            "fold": {"from": FOLD_FROM_RANK, "label": FOLD_LABEL},
            "sentences": {e["id"]: _sentence_data(e, rows_by) for e in entries
                          if e["coverage"] != "n/a"},
            # For the layout's title row (`tests.py:reqmap_layout`): the ticket this matrix
            # was drawn against, so the heading never names a different one.
            "ticket": {k: ticket.get(k) for k in ("number", "title", "url", "source", "via",
                                                  "origin")}}
    blob = json.dumps(data, ensure_ascii=False, sort_keys=False).replace("</", "<\\/")
    # "…in this PR" only over a pull request (`tests.py:covcard_who`, passed in by
    # `write_fragment`); a matrix drawn with no spec to ask says "change".
    who = (who or T.COVCARD_WHO) if measured else "Tests this branch added or changed"
    side = ('<div class="rm-side">' + CAT_KEY
            + '<aside class="rm-code" aria-label="the tests the change set runs">'
            + '<div class="rm-tkhead"><span class="rm-av rm-av-ai" data-tip="paired with the '
            'ticket by a script, and by AI where the script could not">🤖</span>'
            + f'<span class="rm-who">{who}</span></div>'
            + '<div class="rm-list"></div></aside></div>')
    text = ('<div class="rm-text">' + legend + _ticket_html(ticket, blocks, by_sid)
            + '<div class="rm-gap" hidden></div></div>')
    css = (ASSETS / "reqmap.css").read_text(encoding="utf-8")
    js = (ASSETS / "reqmap.js").read_text(encoding="utf-8")
    return (f'<div class="reqmap" {GENERATED}>\n<style>\n{css}</style>\n'
            f'<script type="application/json" class="rm-data">{blob}</script>\n'
            f'<div class="rm-body">{text}{side}</div>\n<script>\n{js}</script>\n</div>\n')


# --- the whole pass ---------------------------------------------------------------------

def gather(spec: dict, out_dir: Path, root: Path) -> dict | None:
    """Everything the matrix is drawn from, or None when there is no ticket to draw."""
    ticket = fetch_ticket(spec, out_dir, root)
    if ticket is None:
        return None
    # The OpenSpec change the branch implements, when there is one: under an issue, its
    # requirements are drawn as a numbered list below the issue's own text and paired like
    # it — run 5's issue had two bullets and its spec ten requirements, and the matrix showed
    # only the two. When the change *is* the ticket (no issue), `fetch_ticket` already drew it.
    name, specs = openspec_change(root, branch_name(spec, root), ticket.get("number"))
    reqs = spec_requirements(specs) if specs and ticket.get("source") != "openspec" else []
    if reqs:
        ticket = {**ticket, "body": (ticket.get("body") or "").rstrip() + "\n\n"
                  + spec_markdown(name, reqs),
                  "origin": (ticket.get("origin") or "") + f", then the {len(reqs)} "
                  f"requirement{'s' if len(reqs) != 1 else ''} of the OpenSpec change {name}",
                  "spec": {"name": name, "requirements": len(reqs)}}
    blocks = parse_ticket(ticket.get("body") or "")
    if reqs:
        _attach_scenarios(blocks, SPEC_HEADING.format(name=name), reqs)
    sentences = ticket_sentences(blocks)
    rows, measured = covering_tests(spec, out_dir, root)
    docs = test_documents(rows, root)
    scripted = match(sentences, rows, root, docs)
    change_dir = root / "openspec" / "changes" / name if name else None
    return {"ticket": ticket, "blocks": blocks, "sentences": sentences, "rows": rows,
            "measured": measured, "docs": docs, "scripted": scripted,
            "decisions": recorded_decisions(out_dir, change_dir)}


def _attach_scenarios(blocks: list[dict], heading: str, reqs: list[dict]) -> None:
    """Hang each requirement's name and scenarios on the sentences of its list item, so the
    model judges a SHALL sentence against the scenarios that say what it means."""
    after = False
    for b in blocks:
        if b["kind"] == "h" and _plain(b["text"]) == _plain(heading):
            after = True
            continue
        if after and b["kind"] == "ol":
            for item, r in zip(b["items"], reqs):
                for snt in item["sentences"]:
                    snt["requirement"] = r["name"]
                    snt["scenarios"] = r["scenarios"]
            return


def split_counts(entries: list[dict]) -> dict:
    """Links by who stands behind them — `script` (shared words, unconfirmed) or `model` —
    plus how many scripted links the model rejected, and sentences by who coloured them."""
    links = {"script": 0, "model": 0}
    sents = {"script": 0, "model": 0, "unmapped": 0}
    rejected = 0
    for e in entries:
        if e["coverage"] == "unmapped":
            sents["unmapped"] += 1
            continue
        sents[e.get("by") or "script"] += 1
        rejected += len(e.get("rejected") or [])
        for t in e["tests"]:
            links[t.get("by") or e.get("by") or "script"] += 1
    return {"links": links, "sentences": sents, "rejected": rejected}


def write_fragment(spec: dict, out_dir: Path, root: Path) -> str | None:
    """Render `assets/requirements-map.html` from the inputs, or leave a model-written one
    from an older run alone. Returns what happened, for the build's log, or None when
    there was nothing to draw from."""
    frag = out_dir / FRAGMENT
    model = load_model_mapping(out_dir)
    old = frag.read_text(encoding="utf-8") if frag.is_file() else ""
    if model is None and old and GENERATED not in old:
        print(f"[semcov] no {MAPPING}: keeping the model-written {FRAGMENT} from an older "
              "run as it is. Run rerun-model.py (the Tests tab's 🤖) to replace it with the "
              "scripted matrix.", file=sys.stderr)
        return "kept the model-written matrix"
    g = gather(spec, out_dir, root)
    if g is None:
        return None
    entries = merge(g["sentences"], g["scripted"], model, g["decisions"])
    page = render(g["ticket"], g["blocks"], g["rows"], entries, root, g["measured"],
                  who=_tests_tab().covcard_who(spec, out_dir))
    if old and GENERATED not in old:
        # The model-written matrix this replaces is a paid judgement; keep one copy.
        # `.model-prev/` is "the copy just replaced", as rerun-model.py uses it.
        prev = out_dir / ".model-prev"
        prev.mkdir(exist_ok=True)
        (prev / "requirements-map.html").write_text(old, encoding="utf-8")
    frag.parent.mkdir(parents=True, exist_ok=True)
    frag.write_text(page, encoding="utf-8")
    c = split_counts(entries)
    merged = {"schema": SCHEMA_VERSION,
              "note": (f"{c['links']['script']} links by script (unconfirmed), "
                       f"{c['links']['model']} by model, {c['rejected']} script links "
                       f"rejected by the model; {c['sentences']['unmapped']} sentences not "
                       "paired yet"),
              "sentences": [e for e in entries if e["coverage"] != "unmapped"]}
    (out_dir / MERGED).write_text(json.dumps(merged, indent=1, ensure_ascii=False) + "\n",
                                  encoding="utf-8")
    return (f"{len(g['sentences'])} sentences × {len(g['rows'])} tests — "
            f"{c['links']['script']} links by script (unconfirmed), {c['links']['model']} by "
            f"model, {c['rejected']} rejected, {c['sentences']['unmapped']} sentences not "
            "paired yet")


# --- agreement with a model-written matrix ----------------------------------------------

def reference_pairs(html_text: str) -> tuple[dict, set]:
    """`({sentence text: coverage}, {(sentence text, test id)})` out of a model-written
    `requirements-map.html` — the pairing a paid run made, to measure this one against."""
    m = re.search(r'<script type="application/json" class="rm-data">(.*?)</script>',
                  html_text, re.S)
    data = json.loads(m.group(1)) if m else {}
    texts = {}
    for sid, inner in re.findall(r'<span class="rm-f" data-s="([^"]+)"[^>]*>(.*?)</span>',
                                 html_text, re.S):
        texts.setdefault(sid, _plain(html.unescape(re.sub(r"<[^>]+>", "", inner))))
    covs, pairs = {}, set()
    for sid, s in (data.get("sentences") or {}).items():
        t = texts.get(sid)
        if not t:
            continue
        covs[t] = s.get("cov")
        for g in s.get("groups") or []:
            for ev in g.get("tests") or []:
                pairs.add((t, ev["id"]))
    for t in re.findall(r'<span class="rm-plain">(.*?)</span>', html_text, re.S):
        covs.setdefault(_plain(html.unescape(re.sub(r"<[^>]+>", "", t))), "none")
    return covs, pairs


def agreement(g: dict, entries: list[dict], ref_html: str) -> dict:
    covs, gold = reference_pairs(ref_html)
    text_of = {s["id"]: s["text"] for s in g["sentences"]}
    ours = {(text_of[e["id"]], t["id"]) for e in entries for t in e["tests"]}
    on_card = {r["id"] for r in g["rows"]}
    gold_card = {p for p in gold if p[1] in on_card}
    tp = ours & gold
    # The reference model paired over the tests it chose to list; a link here to a test it
    # never looked at is not evidence of a wrong pairing, so precision is also given over
    # the tests the reference itself paired with something.
    ref_tests = {t for _, t in gold}
    ours_ref = {p for p in ours if p[1] in ref_tests}
    state = {text_of[e["id"]]: COV_ATTR[e["coverage"]] for e in entries}
    same = sum(1 for t, c in covs.items() if state.get(t) == c)
    return {"pairs": len(ours), "reference_pairs": len(gold),
            "reference_pairs_on_card": len(gold_card), "agree": len(tp),
            "precision": round(len(tp) / len(ours), 3) if ours else None,
            "precision_on_reference_tests": round(len(ours_ref & gold) / len(ours_ref), 3)
            if ours_ref else None,
            "recall": round(len(tp) / len(gold), 3) if gold else None,
            "recall_on_card": round(len(ours & gold_card) / len(gold_card), 3)
            if gold_card else None,
            "sentence_state_agree": f"{same}/{len(covs)}"}


# --- CLI --------------------------------------------------------------------------------

def _spec(review: Path) -> dict:
    return _read_json(review / "content.json") or {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("inputs", "match", "render", "validate", "agreement"))
    ap.add_argument("--dir", default=".human-review", help="the review directory")
    ap.add_argument("--root", default=".", help="the repository root")
    ap.add_argument("--reference", help="agreement: a model-written requirements-map.html")
    ap.add_argument("--file", help="validate: the mapping to check (default: test-mapping.json)")
    args = ap.parse_args(argv)
    review, root = Path(args.dir), Path(args.root).resolve()
    spec = _spec(review)
    if args.command == "render":
        said = write_fragment(spec, review, root)
        print(f"[semcov] {said}" if said else "[semcov] no ticket to draw the matrix from")
        return 0 if said else 2
    if args.command == "validate":
        path = Path(args.file) if args.file else review / MAPPING
        doc = _read_json(path)
        g = gather(spec, review, root)
        sids = {s["id"] for s in g["sentences"]} if g else None
        tids = {r["id"] for r in g["rows"]} if g else None
        links = scripted_links(g["scripted"]) if g else None
        dids = {d["id"] for d in g["decisions"]} if g else None
        bad = problems(doc, sids, tids, links, dids) if doc is not None \
            else [f"{path}: not JSON"]
        for b in bad:
            print(b, file=sys.stderr)
        print(f"[semcov] {path}: " + ("valid" if not bad else f"{len(bad)} problem(s)"))
        return 0 if not bad else 1
    g = gather(spec, review, root)
    if g is None:
        print("[semcov] no ticket resolved — nothing to pair", file=sys.stderr)
        return 2
    if args.command == "match":
        print(json.dumps({"schema": SCHEMA_VERSION, "sentences": g["scripted"]["decided"],
                          "open": g["scripted"]["open"]}, indent=1, ensure_ascii=False))
        return 0
    if args.command == "inputs":
        print(json.dumps(model_input(g["ticket"], g["sentences"], g["rows"], g["scripted"],
                                     g["docs"], g["decisions"]), indent=1, ensure_ascii=False))
        return 0
    ref = Path(args.reference or review / ".model-prev" / "requirements-map.html")
    entries = merge(g["sentences"], g["scripted"], load_model_mapping(review))
    print(json.dumps(agreement(g, entries, ref.read_text(encoding="utf-8")), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
