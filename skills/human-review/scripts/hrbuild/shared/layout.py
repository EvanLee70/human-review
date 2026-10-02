"""The tab layout and the scope bar are the skill's, not the content file's.

Every tab after Review shows something a script produced — the film, the REST contract,
the diagram deltas, the matrix, the city, the audit, the bars, the logging scan, the owners
check. Which blocks those tabs carry, and what the script-produced sections include and
embed, is therefore a fact about the skill, the same on every branch. It used to be
re-typed into `content.json` by whichever model ran the review, off an example in
`reference/content-schema.md`, and a model that improvised produced a different page:

- an API tab of four sections — its own prose and snippet on top, then the text diff, the
  compatibility table and the pb33f report — instead of the verdict band over the visual
  diff;
- a Data tab with a section of its own (*"Page first, associations second"*, prose and two
  hand-picked snippets) between the generated diagrams and the conceptual model;
- a Demo tab declared with no blocks, which the build drops as empty — so the one tab whose
  absence most needs explaining (no film) vanished instead of saying why;
- a scope chip `gate green: CI for 7f71…`, true of every page that exists at all, since a
  red gate means there is no page.

So the build normalises all of it before anything renders, and says on stderr what it threw
away. The content file still names the tabs (and so still carries the Review tab's ledes,
each tab's hover, and the few per-branch selections a script cannot make — which diagrams,
which tests to quote beside their sequences), but a script-owned tab keeps only blocks of
the kinds its scripts produce, its sections are rebuilt from `SECTIONS`, and a chip that is
not computed does not reach the bar.
"""
from __future__ import annotations

import copy

#: The canonical script-produced sections, by id. What a model writes into one of these is
#: replaced, except the keys in `MODEL_KEYS` — the few per-run inputs no script derives.
LAYOUT_SECTIONS: dict[str, dict] = {
    "video": {"id": "video", "title": "", "video": "assets/feature.webm"},
    "swaggerdiff": {
        "id": "swaggerdiff", "title": "",
        "includeHtml": "assets/openapi-verdict.html",
        "embed": {"src": "assets/openapi-visual-diff.html#only-touched", "class": "oaviframe",
                  "label": "openapi-visual-diff — the REST contract at the base against "
                           "the working tree",
                  "missing": "run scripts/openapi-visual-diff.py (needs `brew install oasdiff`)"},
    },
    "conceptual": {"id": "conceptual", "title": "", "body": "{{drawio:conceptual}}"},
    "requirements-map": {"id": "requirements-map", "title": "",
                         "includeHtml": "assets/requirements-map.html", "includeFirst": True},
    "ds-audit": {"id": "ds-audit", "title": "", "includeHtml": "assets/ds-audit.html"},
    "complexity-delta": {"id": "complexity-delta", "title": "",
                         "includeHtml": "assets/complexity-delta.html"},
}

#: Keys of a canonical section the content file may still set: where the film landed (the
#: project's `steps.video.out`), and the environment row and app links under it.
LAYOUT_MODEL_KEYS = {"video": ("video", "appLinks", "runtime")}

#: Which step produces each canonical include — named in the notice that replaces a
#: fragment the run did not write, so the reader knows what to re-run.
LAYOUT_PRODUCER = {"swaggerdiff": "api", "requirements-map": "rerun-model.py (the Tests matrix)",
            "ds-audit": "dsaudit", "complexity-delta": "complexity"}

#: Script-owned tabs, in the default strip order. `label` is used only for a tab the build
#: adds itself — a declared tab keeps its own, since a label is free and an id is not.
#: `blocks` are the non-section block types
#: the tab may carry (each a script's output, or a selection among them); `sections` the
#: canonical sections it carries, `required` the ones added when missing, and `first`
#: whether the sections lead the panel (the matrix opens Tests; the conceptual model closes
#: Data).
LAYOUT_TABS: dict[str, dict] = {
    "behaviour":    {"label": "Demo", "blocks": (), "sections": ("video",),
                     "required": ("video",)},
    "api":          {"label": "API", "blocks": (), "sections": ("swaggerdiff",),
                     "required": ("swaggerdiff",)},
    "data":         {"label": "Data", "blocks": ("diagrams", "puml"),
                     "sections": ("conceptual",), "required": ()},
    "requirements": {"label": "Tests", "blocks": ("tests", "traces"),
                     "sections": ("requirements-map",), "required": (), "first": True},
    "sequence":     {"label": "Sequence", "blocks": ("testpairs", "diagrams", "puml"),
                     "sections": (), "required": ()},
    "packages":     {"label": "Structure", "blocks": ("diagrams", "puml"),
                     "sections": (), "required": ()},
    "city":         {"label": "Code City", "blocks": ("codecity",), "sections": (),
                     "required": ()},
    "dsaudit":      {"label": "UX", "blocks": (), "sections": ("ds-audit",),
                     "required": ("ds-audit",)},
    "complexity":   {"label": "Complexity", "blocks": (), "sections": ("complexity-delta",),
                     "required": ("complexity-delta",)},
    "logging":      {"label": "Logging", "blocks": ("logging",), "sections": (),
                     "required": ()},
    "owners":       {"label": "CODEOWNERS", "blocks": ("codeowners",), "sections": (),
                     "required": ()},
}

#: The tab that is on the page whenever its step ran, declared or not: no film is the case
#: that most needs saying, and an absent pill says nothing.
LAYOUT_ALWAYS = "behaviour"


def _video_step_ran(out_dir) -> bool:
    """The film, the recorder's verdict beside it, or the ledger's mark that the step ran."""
    return any((out_dir / p).exists() for p in
               ("assets/feature.webm", "assets/feature.verdict.json", ".step-video"))


def _layout_section(sid: str, model: dict | None, out_dir) -> dict:
    sec = copy.deepcopy(LAYOUT_SECTIONS[sid])
    for k in LAYOUT_MODEL_KEYS.get(sid, ()):
        if model and model.get(k):
            sec[k] = model[k]
    inc = sec.get("includeHtml")
    if inc and not (out_dir / inc).is_file():
        # A canonical fragment the run did not write is a step that did not run, said in
        # the panel — not a build that refuses, and not a tab that silently disappears.
        del sec["includeHtml"]
        sec["body"] = (f'<p class="sub">Not produced — <code>{inc}</code> is missing; '
                       f'run the <code>{LAYOUT_PRODUCER.get(sid, sid)}</code> step.</p>')
    return sec


def _layout_overridden(model: dict, sid: str) -> list[str]:
    """The keys of a model-written canonical section the build is about to replace."""
    canon, keep = LAYOUT_SECTIONS[sid], set(LAYOUT_MODEL_KEYS.get(sid, ())) | {"id"}
    return sorted(k for k in set(model) | set(canon)
                  if k not in keep and model.get(k) != canon.get(k)
                  and (model.get(k) or canon.get(k)))


def own_layout(spec: dict, out_dir) -> list[str]:
    """Normalise `spec["tabs"]`, the script-owned `sections` and `scope` in place.

    Returns one warning per thing the content file asked for and did not get. Tabs this
    module does not know (`review`, or any tab a test invents) pass through untouched."""
    warnings: list[str] = []
    tabs = spec.get("tabs")
    if not isinstance(tabs, list):
        return warnings
    by_id = {s.get("id"): s for s in spec.get("sections") or []}
    used: set[str] = set()
    orphans: set[str] = set()

    for tab in tabs:
        rule = LAYOUT_TABS.get(tab.get("id"))
        if rule is None:
            continue
        tid = tab["id"]
        if tab.get("intro"):
            warnings.append(f"tab {tid!r}: 'intro' dropped — this tab shows what its "
                            "script produced and nothing a model wrote")
            tab.pop("intro")
        kept, sections, dropped = [], [], []
        for b in tab.get("blocks") or []:
            kind = b.get("type", "section")
            if kind == "section" and b.get("id") in rule["sections"]:
                if b["id"] not in sections:
                    sections.append(b["id"])
            elif kind in rule["blocks"]:
                kept.append(b)
            else:
                dropped.append(f"{kind} {b.get('id')!r}" if b.get("id") else kind)
                if kind == "section":
                    orphans.add(b.get("id"))
        for sid in rule["required"]:
            if sid not in sections:
                sections.append(sid)
        if dropped:
            warnings.append(f"tab {tid!r}: dropped {', '.join(dropped)} — the tab is "
                            "script-owned; prose and snippets of your own do not go on it")
        sec_blocks = [{"type": "section", "id": sid} for sid in rule["sections"]
                      if sid in sections]
        tab["blocks"] = sec_blocks + kept if rule.get("first") else kept + sec_blocks
        used.update(sections)

    present = {t.get("id") for t in tabs}
    if LAYOUT_ALWAYS not in present and _video_step_ran(out_dir):
        at = next((i + 1 for i, t in enumerate(tabs) if t.get("id") == "review"), 0)
        tabs.insert(at, {"id": LAYOUT_ALWAYS, "label": LAYOUT_TABS[LAYOUT_ALWAYS]["label"],
                         "blocks": [{"type": "section", "id": "video"}]})
        used.add("video")

    # A section only a dropped block pointed at goes with it: rendered, it would still
    # resolve its snippets for a page that never shows them.
    referenced = {b.get("id") for t in tabs for b in t.get("blocks") or []
                  if b.get("type", "section") == "section"}
    out = [s for s in spec.get("sections") or []
           if s.get("id") not in used and not (s.get("id") in orphans
                                               and s.get("id") not in referenced)]
    for sid in LAYOUT_SECTIONS:
        if sid in used:
            model = by_id.get(sid)
            if model and _layout_overridden(model, sid):
                warnings.append(f"section {sid!r} is built by the skill — its "
                                + ", ".join(_layout_overridden(model, sid))
                                + " in the content file differed and were replaced")
            out.append(_layout_section(sid, model, out_dir))
    if used or orphans:
        spec["sections"] = out

    scope = spec.get("scope")
    if isinstance(scope, list):
        typed = [c for c in scope if not c.get("auto")]
        if typed:
            spec["scope"] = [c for c in scope if c.get("auto")]
            names = [str(c.get("label") or c.get("value") or "?") for c in typed]
            hint = (' — {"auto": "diffstat"} measures files and lines at build time'
                    if {"files", "lines"} & set(names) else "")
            warnings.append(f"scope: {' and '.join(names)} typed by hand — dropped. The bar "
                            "carries computed chips only ({\"auto\": \"diffstat\"|\"tests\"|"
                            "\"autofixed\"})" + hint)
    return warnings

