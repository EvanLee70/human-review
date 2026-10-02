"""The script-owned tabs and the scope bar are the skill's, whatever the content file says.

The fixture is the content file a GitHub Copilot (GPT) run wrote for petclinic on 2 Oct 2026,
cut down to the parts it got wrong: a Data tab with a section of its own prose and snippets,
an API tab of four sections instead of the verdict over the visual diff, a Demo tab declared
with no blocks (so it vanished), and a `gate green: CI …` chip on the scope bar.
"""
from __future__ import annotations

import json
import re

from test_build_review import _build, REF

COPILOT = {
    "title": "t",
    "scope": [{"auto": "autofixed", "href": "#review", "by": "Opus 5"},
              {"label": "gate", "value": "green: CI for 7f719558de59"}],
    "findings": [{"title": "f", "body": "<p>b</p>"}],
    "sections": [
        {"id": "video", "title": ""},
        {"id": "api-note", "title": "Coordinated contract migration",
         "body": "<p>Deploy and roll back the backend and frontend together.</p>",
         "snippets": [{"ref": REF + "-3", "caption": "The list response contract"}]},
        {"id": "swaggerdiff", "title": "", "includeHtml": "assets/openapi-diff.html"},
        {"id": "compatibility", "title": "", "includeHtml": "assets/openapi-compat.html"},
        {"id": "pb33f", "title": "Independent contract comparison",
         "includeHtml": "assets/openapi-changes.html"},
        {"id": "data-note", "title": "Page first, associations second",
         "body": "<p>The migration adds indexes without changing entity relationships.</p>",
         "snippets": [{"ref": REF + "-3", "caption": "Additive owner indexes"}]},
        {"id": "conceptual", "title": "", "body": "{{drawio:conceptual}}"},
    ],
    "tabs": [
        {"id": "review", "label": "Review", "blocks": [{"type": "findings"}]},
        {"id": "behaviour", "label": "Demo", "blocks": []},
        {"id": "api", "label": "API", "blocks": [
            {"type": "section", "id": "api-note"}, {"type": "section", "id": "swaggerdiff"},
            {"type": "section", "id": "compatibility"}, {"type": "section", "id": "pb33f"}]},
        {"id": "data", "label": "Data", "blocks": [
            {"type": "diagrams", "only": ["DomainModel", "DB"]},
            {"type": "section", "id": "data-note"}, {"type": "section", "id": "conceptual"}]},
    ],
}


def _assets(tmp_path):
    a = tmp_path / "assets"
    a.mkdir()
    (a / "openapi-verdict.html").write_text('<div class="oav">Backwards compatible</div>',
                                            encoding="utf-8")
    (a / "openapi-visual-diff.html").write_text("<html></html>", encoding="utf-8")
    (a / "feature.verdict.json").write_text(json.dumps(
        {"exit": 2, "log": ["[video] no feature script — nothing to film"]}), encoding="utf-8")


def _panel(page, tid):
    m = re.search(rf'<section class="panel" id="{tid}"[^>]*>(.*?)</section>\s*'
                  r'(?=<section class="panel"|</main>|<footer|$)', page, re.S)
    assert m, f"no {tid!r} panel on the page"
    return m.group(1)


def test_a_model_written_data_section_does_not_reach_the_page(tmp_path):
    _assets(tmp_path)
    page, err = _build(tmp_path, COPILOT)
    assert "Page first, associations second" not in page
    assert "Additive owner indexes" not in page
    assert "data-note" in err, "the drop is named on stderr"
    # …while what the scripts produced stays: the conceptual model closes the tab.
    assert "run the <code>diagrams</code> step" in _panel(page, "data")


def test_the_api_tab_is_the_verdict_over_the_visual_diff_and_nothing_else(tmp_path):
    _assets(tmp_path)
    page, err = _build(tmp_path, COPILOT)
    api = _panel(page, "api")
    assert "Backwards compatible" in api
    assert 'class="oaviframe"' in api
    assert 'src="assets/openapi-visual-diff.html#only-touched"' in api
    assert "Coordinated contract migration" not in page
    assert "Independent contract comparison" not in page
    assert "api-note" in err and "pb33f" in err


def test_a_demo_tab_declared_empty_still_says_why_there_is_no_film(tmp_path):
    _assets(tmp_path)
    page, _ = _build(tmp_path, COPILOT)
    demo = _panel(page, "behaviour")
    assert "Nothing was filmed." in demo
    assert "no feature script" in demo


def test_a_demo_tab_left_out_is_put_back_when_the_video_step_ran(tmp_path):
    _assets(tmp_path)
    content = {**COPILOT, "tabs": [t for t in COPILOT["tabs"] if t["id"] != "behaviour"]}
    page, _ = _build(tmp_path, content)
    assert 'id="tabbtn-behaviour"' in page
    assert "Nothing was filmed." in _panel(page, "behaviour")


def test_a_typed_chip_does_not_reach_the_scope_bar(tmp_path):
    _assets(tmp_path)
    page, err = _build(tmp_path, COPILOT)
    bar = page[page.index('<div class="scopebar">'):]
    bar = bar[:bar.index("</div>")]
    assert "green: CI" not in bar and ">gate<" not in bar
    assert "Opus 5" in bar, "the computed chip beside it still renders"
    assert "gate typed by hand" in err
