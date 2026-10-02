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
INPUT_VERSION = "test-mapping-input/1"
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
#: The mapping's coverage word → the renderer's `data-cov` and its hover.
COV_ATTR = {"covered": "covered", "partial": "partly", "exercised": "exercised",
            "missing": "missing", "n/a": "none", "unmapped": "unmapped"}
COV_LABEL = {"covered": "covered", "partial": "partly covered",
             "exercised": "exercised, never asserted", "missing": "no covering tests",
             "n/a": "not a claim", "unmapped": "not paired yet"}


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
        specs = sorted((d / "specs").rglob("spec.md")) if (d / "specs").is_dir() else []
        if hit and specs:
            return d.name, specs
    return "", []


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
                           f"`ticket: {fm_value}`"}
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
                           + ("content.json `pr.ticket`" if declared else "the PR title")}
        number = number or ref["number"]
    b_issue = branch_issue(branch) if branch else None
    if b_issue is not None:
        got = _issue(b_issue, slug, out_dir)
        if got:
            return {**got, "source": "github",
                    "via": f"GitHub issue #{got['number']}, named by the branch `{branch}`"}
        number = number or b_issue

    plain = {"number": None, "url": "", "author": "", "avatar": "", "createdAt": ""}
    if fm_text:
        return {**plain, "title": "the ticket text in review-points.md", "body": fm_text,
                "source": "front-matter",
                "via": "the `ticket:` text in review-points.md's front-matter — not a "
                       "GitHub issue"}
    name, specs = openspec_change(root, branch, number)
    if specs:
        body = "\n\n".join(p.read_text(encoding="utf-8") for p in specs)
        rel = ", ".join(f"`{p.relative_to(root)}`" for p in specs)
        return {**plain, "title": f"OpenSpec change {name}", "body": body,
                "source": "openspec", "via": f"the OpenSpec change `{name}` ({rel}) — not a "
                                             "GitHub issue"}
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
                       "request 0) — not a GitHub issue"}
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
                         "status": STAMP.get(st.get("status"), "unchanged"),
                         "hits": r.get("changedHits") or {}, "aimed": bool(r.get("aimed"))})
        return rows, True
    for t in (test_doc or {}).get("tests") or []:
        file, line = t.get("path"), t.get("line")
        if not file or not line or f"{file}:{line}" in seen:
            continue
        seen.add(f"{file}:{line}")
        rows.append({"id": f"{file}:{line}", "file": file, "line": int(line),
                     "title": t.get("name") or "", "suite": "",
                     "cat": T._cov_cat({"file": file}, root),
                     "status": STAMP.get(t.get("status"), "unchanged"),
                     "hits": {}, "aimed": True})
    return rows, False


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_GHERKIN_NEXT = re.compile(r"\s*(Scenario|Rule|Feature|Background|Examples|@)")
BODY_MAX = 60


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
    start = line
    while start > 1 and lines[start - 2].strip().startswith("@"):
        start -= 1
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
PRIOR = {"new": 1.0, "changed": 1.0, "unchanged": 0.5, "deleted": 0.5}
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
    ids, best first]}}`.

    A sentence under an *Out of scope* heading is `n/a`. Otherwise each test is scored
    against the sentence and each of its clauses (`_score`, `_best`), and the sentence is
    linked to every test at or over LINK_AT within NEAR_BEST of the best, at most
    MAX_LINKS. A sentence with no such test is *open* — the model's to decide, with the
    tests over CANDIDATE_AT as its candidates. Coverage is read off the links: asserted
    links make it `covered`, unless a clause has no asserted link of its own (`partial`);
    links that only run through it make it `exercised`."""
    docs = docs if docs is not None else test_documents(rows, root)
    idf, df, n = _idf(docs)
    decided, open_ = [], {}
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
    return {"decided": decided, "open": open_}


# --- the model's half -------------------------------------------------------------------

def model_input(ticket: dict, sentences: list[dict], rows: list[dict], scripted: dict,
                docs: dict) -> dict:
    """What the cheap model is asked about: only the open sentences, each with its few
    candidate tests, and those tests' bodies. Nothing it is not asked to decide."""
    by_id = {s["id"]: s for s in sentences}
    rows_by = {r["id"]: r for r in rows}
    want: list[str] = []
    for sid, cands in scripted["open"].items():
        for t in cands:
            if t not in want:
                want.append(t)
    return {
        "schema": INPUT_VERSION,
        "ticket": {"number": ticket.get("number"), "title": ticket.get("title", "")},
        "sentences": [{"id": sid, "text": by_id[sid]["text"],
                       "section": by_id[sid].get("section") or "",
                       "candidates": cands}
                      for sid, cands in scripted["open"].items()],
        "context": [s["text"] for s in sentences],
        "tests": [{"id": t, "title": rows_by[t]["title"], "kind": CATS[rows_by[t]["cat"]],
                   "status": rows_by[t]["status"],
                   "body": docs[t]["body_text"]} for t in want],
    }


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def problems(doc, allowed_sentences=None, allowed_tests=None) -> list[str]:
    """Every way a mapping departs from the schema, or from the facts it is about.

    The schema is checked by `review_points_schema.problems` — the skill's own small
    draft-2020-12 checker, so no `jsonschema` is needed on a trainee's laptop. Then what a
    schema cannot say: no sentence twice, a sentence id this ticket has, a test id from the
    list the model was given, and a coverage word its own links can stand behind."""
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
    return out


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


def merge(sentences: list[dict], scripted: dict, model: dict | None) -> list[dict]:
    """One entry per ticket sentence, in reading order: the script's where it decided, the
    model's where it did not and the model answered, `unmapped` where neither has."""
    script = {e["id"]: e for e in scripted["decided"]}
    answer = {e["id"]: e for e in (model or {}).get("sentences") or []}
    out = []
    for s in sentences:
        if s["id"] in script:
            out.append(script[s["id"]])
        elif s["id"] in answer:
            e = json.loads(json.dumps(answer[s["id"]]))
            e["by"] = "model"
            for t in e["tests"]:
                t["by"] = "model"
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


def _sentence_html(s: dict, entry: dict) -> str:
    cov = entry["coverage"]
    inner = _inline(s["md"])
    if cov == "n/a":
        return f'<span class="rm-plain" data-s="{s["id"]}">{inner}</span>'
    tip = COV_LABEL[cov]
    src = ' data-src="model"' if entry.get("by") == "model" else ""
    return (f'<span class="rm-f" data-s="{s["id"]}" data-cov="{COV_ATTR[cov]}"{src} '
            f'role="button" tabindex="0" data-tip="{html.escape(tip, quote=True)}">'
            f"{inner}</span>")


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
    # Where the left column's text came from, said on the frame that holds it.
    if ticket.get("via"):
        head += f'<span class="rm-src">Requirement text: {_inline(ticket["via"])}</span>'
    return ('<div class="rm-ticket"><div class="rm-tkhead">' + head
            + '</div><div class="rm-issue">' + "".join(body) + "</div></div>")


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
    if entry.get("gap"):
        d["gap"] = entry["gap"]
        d["gapKind"] = entry.get("gapKind") or "tests"
    elif entry["coverage"] == "unmapped":
        d["gap"] = ("The script found no test that shares this sentence's words, and no "
                    "model has been asked yet — run the 🤖 beside the Tests tab.")
        d["gapKind"] = "tests"
    return d


def render(ticket: dict, blocks: list[dict], rows: list[dict], entries: list[dict],
           root: Path, measured: bool = True) -> str:
    """The matrix fragment: same inputs, same bytes."""
    T = _tests_tab()
    by_sid = {e["id"]: e for e in entries}
    rows_by = {r["id"]: r for r in rows}
    tests = {}
    for r in rows:
        part = T._cov_part(root, r["file"], r["line"]) if r["status"] != "deleted" else None
        tests[r["id"]] = {"title": r["title"] or r["id"], "cat": r["cat"],
                          "status": r["status"], "parts": [part] if part else []}
    data = {"cats": CATS, "tests": tests,
            "sentences": {e["id"]: _sentence_data(e, rows_by) for e in entries
                          if e["coverage"] != "n/a"},
            # For the layout's title row (`tests.py:reqmap_layout`): the ticket this matrix
            # was drawn against, so the heading never names a different one.
            "ticket": {k: ticket.get(k) for k in ("number", "title", "url", "source", "via")}}
    blob = json.dumps(data, ensure_ascii=False, sort_keys=False).replace("</", "<\\/")
    who = ("Tests that cover files modified in this PR" if measured
           else "Tests this branch added or changed")
    side = ('<div class="rm-side">' + CAT_KEY
            + '<aside class="rm-code" aria-label="the tests the change set runs">'
            + '<div class="rm-tkhead"><span class="rm-av rm-av-ai" data-tip="paired with the '
            'ticket by a script, and by AI where the script could not">🤖</span>'
            + f'<span class="rm-who">{who}</span></div>'
            + '<div class="rm-list"></div></aside></div>')
    text = ('<div class="rm-text">' + LEGEND + _ticket_html(ticket, blocks, by_sid)
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
    blocks = parse_ticket(ticket.get("body") or "")
    sentences = ticket_sentences(blocks)
    rows, measured = covering_tests(spec, out_dir, root)
    docs = test_documents(rows, root)
    scripted = match(sentences, rows, root, docs)
    return {"ticket": ticket, "blocks": blocks, "sentences": sentences, "rows": rows,
            "measured": measured, "docs": docs, "scripted": scripted}


def split_counts(entries: list[dict]) -> dict:
    links = {"script": 0, "model": 0}
    sents = {"script": 0, "model": 0, "unmapped": 0}
    for e in entries:
        if e["coverage"] == "unmapped":
            sents["unmapped"] += 1
            continue
        sents[e.get("by") or "script"] += 1
        for t in e["tests"]:
            links[t.get("by") or e.get("by") or "script"] += 1
    return {"links": links, "sentences": sents}


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
    entries = merge(g["sentences"], g["scripted"], model)
    page = render(g["ticket"], g["blocks"], g["rows"], entries, root, g["measured"])
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
              "note": (f"{c['links']['script']} links by script, {c['links']['model']} by "
                       f"model; {c['sentences']['unmapped']} sentences not paired yet"),
              "sentences": [e for e in entries if e["coverage"] != "unmapped"]}
    (out_dir / MERGED).write_text(json.dumps(merged, indent=1, ensure_ascii=False) + "\n",
                                  encoding="utf-8")
    return (f"{len(g['sentences'])} sentences × {len(g['rows'])} tests — "
            f"{c['links']['script']} links by script, {c['links']['model']} by model, "
            f"{c['sentences']['unmapped']} sentences not paired yet")


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
        bad = problems(doc, sids, tids) if doc is not None else [f"{path}: not JSON"]
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
                                     g["docs"]), indent=1, ensure_ascii=False))
        return 0
    ref = Path(args.reference or review / ".model-prev" / "requirements-map.html")
    entries = merge(g["sentences"], g["scripted"], load_model_mapping(review))
    print(json.dumps(agreement(g, entries, ref.read_text(encoding="utf-8")), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
