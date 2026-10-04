#!/usr/bin/env python3
"""One trace of the traced run, as Grafana shows it — the Sequence tab's "What does a trace
look like?".

The Sequence tab's diagrams are drawn from OpenTelemetry traces, and a reader who has never
seen a trace has nothing to hold the word against: the tab explains it in three lines, and
this is the picture beside the explanation. It is a screenshot of a trace this very run
recorded, of a test whose diagram is on the same tab, so the reader can put the waterfall
and the sequence side by side and see that one was drawn from the other.

It runs while the traced run's stack is still up — `run-steps.py` `_sequence` calls it
before `down` — because the trace store is part of that stack and goes with it. By hand,
against any Grafana that still holds the traces:

    trace-shot.py --grafana http://127.0.0.1:3300

Which trace: each candidate diagram (`--diagram`, default every `*.genseq.puml` the page
can show) is named `<test file>.<scenario slug>.genseq.puml`, and the scenario's own run
stamped its name on a span (`--attribute`, default `span.test.name`). The slug is turned
back into a case-insensitive pattern over that attribute, so a JUnit
`contentCarriesNestedPetsTypesAndVisits()` and its file
`OwnerListTest.java.contentcarriesnestedpetstypesandvisits.genseq.puml` meet without this
script knowing anything about JUnit. Then, in order:

  * a test whose whole run is ONE trace, before a test that made several — one trace for
    one diagram is the comparison the picture exists for;
  * a test this branch wrote or edited (`--prefer`), before one it did not — it is the test
    the review is about;
  * more services, then fewer spans — a trace that crosses a boundary shows what a trace
    is, and a short one still reads at full size.

Writes the PNG (`--out`) and, beside it, `<out>.json`: which diagram, which trace, how many
traces the test made, its spans and services — what the tab needs to say what it shows.
Nothing is written when no candidate has a trace; the tab then leaves the picture out.

Playwright is imported lazily: CI runs `--help` on every script here without it.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

#: Where the shot goes when nobody says: the review's own assets, which the Sequence tab
#: reads (`hrbuild/tabs/sequence.py`, `TRACE_SHOT`).
DEFAULT_OUT = ".human-review/assets/sequence.trace.png"
#: The attribute a traced test stamps its own name on. TraceQL's `span.` scope: the root
#: span the test opens, which is where the genseq tooling puts it.
DEFAULT_ATTRIBUTE = "span.test.name"
#: How far back to search when no window is given: a trace store started for the run holds
#: nothing older, and one left up by hand is searched over the morning's work at most.
DEFAULT_LOOKBACK_S = 6 * 3600
#: CSS width of the trace panel in the shot, chosen to be the page's own column
#: (`.wrap`, 1080px less its padding), so the picture lands on the tab at 1:1 and the
#: waterfall's labels are as large as the page's own text. Device scale 2 keeps it sharp
#: on a retina screen and when opened full size.
PANEL_WIDTH = 1040
DEVICE_SCALE = 2
#: The Explore chrome to the left of the panel at that width: the outline, its gutter.
CHROME_WIDTH = 200
#: The tallest window the shot is taken in, CSS px: Chromium composes at most 16 000
#: device pixels, which at scale 2 is 8 000 — a few hundred spans.
MAX_HEIGHT = 7800

_SEP = "[^a-z0-9]"


def scenario_of(diagram: str) -> tuple[str, str]:
    """`…/AddVisitApiTest.java.adds-a-visit.genseq.puml` -> `("AddVisitApiTest.java",
    "adds-a-visit")`: the generator names a diagram after its test file and the slug of the
    scenario, and a slug has no dots in it."""
    stem = Path(diagram).name
    for suffix in (".genseq.puml", ".genseq"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    test, _, slug = stem.rpartition(".")
    return (test, slug) if test else (stem, "")


def name_pattern(slug: str) -> str:
    """The slug back as a pattern over the name it was made from: words in order, anything
    that is not a letter or a digit between them, any case.

    `adds-a-visit` matches `adds a visit` and `Adds a visit!`; `contentcarries…visits`
    matches `contentCarries…Visits()`. Bounded by separators at both ends, so it holds
    whether the store anchors the pattern or not."""
    words = [w for w in slug.lower().split("-") if w]
    return f"(?i){_SEP}*" + f"{_SEP}+".join(map(re.escape, words)) + f"{_SEP}*"


def traceql(attribute: str, slug: str) -> str:
    return '{ %s =~ "%s" }' % (attribute, name_pattern(slug).replace("\\", "\\\\"))


def span_stats(trace: dict) -> tuple[int, list[str]]:
    """`(spans, services)` of a trace as Tempo returns it — `batches` (v1) or
    `resourceSpans` (OTLP JSON), scope spans under either of their two names."""
    batches = trace.get("batches") or trace.get("resourceSpans") or []
    spans, services = 0, []
    for b in batches:
        for a in (b.get("resource") or {}).get("attributes") or []:
            if a.get("key") == "service.name":
                name = (a.get("value") or {}).get("stringValue")
                if name and name not in services:
                    services.append(name)
        for scope in (b.get("scopeSpans") or b.get("instrumentationLibrarySpans") or []):
            spans += len(scope.get("spans") or [])
    return spans, services


def rank_key(c: dict) -> tuple:
    """The order the module docstring gives: one trace, the branch's own, more services,
    fewer spans — then the diagram's path, so two equal candidates never flip."""
    return (c["traces"] != 1, not c.get("preferred"), -len(c.get("services") or []),
            c.get("spans") or 0, c["diagram"])


class Grafana:
    """Just enough of Grafana's HTTP API: find the Tempo data source, ask it through
    Grafana's proxy. Basic auth is sent and harmless where anonymous access is on."""

    def __init__(self, base: str, user: str, password: str):
        self.base = base.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def get(self, path: str, **params) -> object:
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, headers={"Authorization": self.auth,
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def tempo_uid(self) -> str | None:
        try:
            sources = self.get("/api/datasources")
        except (urllib.error.URLError, ValueError, OSError):
            return None
        for ds in sources if isinstance(sources, list) else []:
            if ds.get("type") == "tempo":
                return ds.get("uid")
        return None

    def search(self, uid: str, q: str, start: int, end: int) -> list[dict]:
        doc = self.get(f"/api/datasources/proxy/uid/{uid}/api/search", q=q,
                       start=start, end=end, limit=20)
        return [t for t in (doc or {}).get("traces") or [] if t.get("traceID")]

    def trace(self, uid: str, trace_id: str) -> dict:
        doc = self.get(f"/api/datasources/proxy/uid/{uid}/api/traces/{trace_id}")
        return doc if isinstance(doc, dict) else {}


def candidates_on_disk(root: Path) -> list[str]:
    """Every diagram the page can show: the run's own copies (`.human-review/assets/genseq`)
    and the committed ones, by repository path, once each."""
    found = set()
    overlay = root / ".human-review/assets/genseq"
    if overlay.is_dir():
        found |= {str(p.relative_to(overlay)) for p in overlay.rglob("*.genseq.puml")}
    got = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard",
                          "*.genseq.puml"], cwd=root, capture_output=True, text=True)
    if got.returncode == 0:
        found |= {p for p in got.stdout.split("\n") if p and not p.startswith(".human-review/")}
    return sorted(found)


def choose(g: Grafana, uid: str, diagrams: list[str], prefer: set[str], attribute: str,
           start: int, end: int, log=print) -> dict | None:
    """The best candidate with a trace in the window, as a dict, or None."""
    found = []
    for rel in diagrams:
        test, slug = scenario_of(rel)
        if not slug:
            continue
        try:
            traces = g.search(uid, traceql(attribute, slug), start, end)
        except (urllib.error.URLError, ValueError, OSError) as exc:
            log(f"[trace-shot] {Path(rel).name}: search failed — {exc}")
            continue
        if not traces:
            continue
        found.append({"diagram": rel, "test": test, "slug": slug, "traces": len(traces),
                      "preferred": rel in prefer, "ids": [t["traceID"] for t in traces],
                      "root": traces[0].get("rootTraceName")})
    if not found:
        return None
    # Only what can still win is fetched: single-trace tests when there are any, and for a
    # test that made several, its biggest trace stands for it.
    single = [c for c in found if c["traces"] == 1]
    for c in single or found:
        best = None
        for tid in c["ids"]:
            try:
                spans, services = span_stats(g.trace(uid, tid))
            except (urllib.error.URLError, ValueError, OSError):
                continue
            if best is None or (len(services), spans) > (len(best[2]), best[1]):
                best = (tid, spans, services)
        if best:
            c["trace"], c["spans"], c["services"] = best
    ready = [c for c in (single or found) if c.get("trace")]
    return min(ready, key=rank_key) if ready else None


def explore_url(base: str, uid: str, trace_id: str) -> str:
    """Grafana Explore on one trace, in kiosk mode (no navigation bar)."""
    panes = {"t": {"datasource": uid, "queries": [{
        "refId": "A", "datasource": {"type": "tempo", "uid": uid},
        "queryType": "traceql", "query": trace_id}],
        "range": {"from": "now-24h", "to": "now"}}}
    return (base.rstrip("/") + "/explore?schemaVersion=1&orgId=1&kiosk&panes="
            + urllib.parse.quote(json.dumps(panes, separators=(",", ":"))))


def shoot(url: str, out: Path, user: str, password: str) -> None:
    """The Trace panel of Explore, whole: header, minimap and every span row.

    The rows live in a scroll container of Explore's own, so a panel taller than the
    window would be clipped at its bottom edge whatever the screenshot call asks; the
    window is grown to the panel's height first, and the panel shot after it settles."""
    from playwright.sync_api import sync_playwright
    panel = 'section[data-testid="data-testid Panel header Trace"]'
    row = '[data-testid="data-testid SpanBar--wrapper"]'
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": PANEL_WIDTH + CHROME_WIDTH,
                                              "height": 1000},
                                    device_scale_factor=DEVICE_SCALE,
                                    http_credentials={"username": user, "password": password})
            page.goto(url)
            page.locator(row).first.wait_for(timeout=60000)
            # Out of the way of the hover styles, and Explore's own scroll to the top.
            page.mouse.move(0, 0)
            for _ in range(3):
                box = page.locator(panel).bounding_box()
                need = min(int(box["y"] + box["height"] + 40) if box else 1000, MAX_HEIGHT)
                if need <= page.viewport_size["height"]:
                    break
                page.set_viewport_size({"width": PANEL_WIDTH + CHROME_WIDTH, "height": need})
                page.wait_for_timeout(600)
            page.wait_for_timeout(800)
            box = page.locator(panel).bounding_box()
            out.parent.mkdir(parents=True, exist_ok=True)
            # Clipped rather than the element shot whole: a trace of a few hundred spans
            # is taller than Chromium can compose at this scale, and its top is the part
            # that says what a trace is.
            page.screenshot(path=str(out), animations="disabled", clip={
                "x": box["x"], "y": box["y"], "width": box["width"],
                "height": min(box["height"], MAX_HEIGHT - box["y"])})
        finally:
            browser.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--grafana", default=os.environ.get("GRAFANA_URL"),
                    help="Grafana's base URL (default: $GRAFANA_URL)")
    ap.add_argument("--user", default=os.environ.get("GRAFANA_USER", "admin"))
    ap.add_argument("--password", default=os.environ.get("GRAFANA_PASSWORD", "admin"))
    ap.add_argument("--attribute", default=DEFAULT_ATTRIBUTE,
                    help="the TraceQL attribute a test's run carries its name in "
                         f"(default: {DEFAULT_ATTRIBUTE})")
    ap.add_argument("--diagram", action="append", default=[],
                    help="a candidate *.genseq.puml, repeatable (default: every one the "
                         "page can show)")
    ap.add_argument("--prefer", action="append", default=[],
                    help="a candidate drawn for a test this branch wrote or edited")
    ap.add_argument("--since", type=int, help="search window start, epoch seconds")
    ap.add_argument("--until", type=int, help="search window end, epoch seconds")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"the PNG (default: {DEFAULT_OUT})")
    ap.add_argument("--wait", type=int, default=20,
                    help="seconds to keep asking while the store has not indexed the run "
                         "yet (default: 20)")
    ap.add_argument("--dry-run", action="store_true",
                    help="say which trace would be shot, and shoot nothing")
    args = ap.parse_args(argv)

    if not args.grafana:
        print("[trace-shot] no Grafana — pass --grafana or set GRAFANA_URL", file=sys.stderr)
        return 2
    g = Grafana(args.grafana, args.user, args.password)
    uid = g.tempo_uid()
    if not uid:
        print(f"[trace-shot] {args.grafana} answers with no Tempo data source",
              file=sys.stderr)
        return 3
    now = int(time.time())
    start = (args.since or now - DEFAULT_LOOKBACK_S) - 60
    end = (args.until or now) + 60
    diagrams = args.diagram or candidates_on_disk(Path("."))
    # Tempo indexes what it ingests a few seconds late, so a search fired as the suite ends
    # comes back empty for traces that are on their way — the generator retries for the
    # same reason. Asked again until `--wait` runs out, never once and given up on.
    deadline = time.monotonic() + max(args.wait, 0)
    while True:
        pick = choose(g, uid, diagrams, set(args.prefer), args.attribute, start, end,
                      log=lambda m: print(m, file=sys.stderr))
        if pick or time.monotonic() >= deadline:
            break
        time.sleep(2)
    if not pick:
        print(f"[trace-shot] none of {len(diagrams)} diagram(s) has a trace in "
              f"{args.grafana} carrying {args.attribute} — no shot", file=sys.stderr)
        return 4
    said = (f"{Path(pick['diagram']).name}: trace {pick['trace']} — {pick['spans']} spans, "
            f"{', '.join(pick['services'])}"
            + (f" (1 of the test's {pick['traces']} traces)" if pick["traces"] > 1 else ""))
    if args.dry_run:
        print(f"[trace-shot] would shoot {said}")
        return 0
    out = Path(args.out)
    shoot(explore_url(g.base, uid, pick["trace"]), out, args.user, args.password)
    meta = {"diagram": pick["diagram"], "test": pick["test"], "trace": pick["trace"],
            "traces": pick["traces"], "spans": pick["spans"], "services": pick["services"],
            "root": pick.get("root"),
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    out.with_suffix(".json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    print(f"[trace-shot] {out}: {said}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
