"""The CODEOWNERS tab."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ..shared.util import CODEOWNERS

# The code-owners flag is the one thing on this page whose severity is *discovered* at
# build time rather than authored: whether a merge is blocked depends on the diff, not on
# what we wrote about it. So the renderer runs the check itself instead of including a
# fragment somebody remembered to regenerate — a stale "no owner touched this" is worse
# than no tab at all.
#
# `base` is the page's one base (`chips.page_base`). Defaulting to `origin/main` here put a
# second base on the page: on a branch carrying commits from before the review, the tab
# demanded an approval for a file nobody in the reviewed range had touched. A `base` on
# the block itself still wins, for a page that deliberately asks a different question.
def codeowners_fragment(block, root: Path, out_dir: Path, base: str | None = None):
    dest = out_dir / block.get("out", "assets/codeowners.html")
    cmd = [sys.executable, str(CODEOWNERS), "--base", block.get("base") or base or "origin/main",
           "--out", str(dest), "--json"]
    if block.get("noUntracked"):
        cmd.append("--no-untracked")
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=root)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or "[review] codeowners-check.py failed")
    return dest.read_text(encoding="utf-8"), json.loads(proc.stdout)
