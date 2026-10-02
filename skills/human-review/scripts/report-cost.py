#!/usr/bin/env python3
"""What producing this review guide cost — recorded by the run that produced it.

The `$` tab's fourth row. The first three (writing the code, the review, the auto-fixes)
were measured by `/record-review` in the harness that ran it and committed as
`review-cost.json`; this one can only be measured by the `/human-review` run itself, at its
end, so it is: Step 5 runs this before the refresh, and it writes
`.human-review/report-cost.json` beside the step ledger:

  * the model work — the session that ran `/human-review` from `.started` to now (a Claude
    transcript, or the Copilot CLI / VS Code session on this branch that ran it), plus
    every paid model step it shelled out to inside that window (`.model-runs.json`, the
    requirements↔tests mapping; `.film-runs.json`, the film script);
  * the wall-clock — `.started` to now, each producer the step ledger timed, and how much
    of the run the model was actually working.

**Once per run.** A second call for the same `.started` keeps the first measurement
(`--force` to replace it): the money is that run's, and a later caller would bill it
again. A refresh (`refresh-report.py`) calls no model and adds only its own seconds, under
`refreshes`.

Usage:
  report-cost.py                         # record this run (Step 5, before the refresh)
  report-cost.py --harness copilot-cli   # say which harness ran it, when it is not Claude
  report-cost.py --json                  # print the record
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_cost as hc  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=".human-review", help="the review directory")
    ap.add_argument("--harness", help="claude-code, copilot-cli or vscode-copilot "
                                      "(default: Claude when .session names one)")
    ap.add_argument("--force", action="store_true",
                    help="measure again even though this run already recorded itself")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    review = Path(args.dir)
    if not review.is_dir():
        print(f"[report-cost] no {review}/ — nothing to record", file=sys.stderr)
        return 2
    root = Path(hc.git(Path.cwd(), "rev-parse", "--show-toplevel") or ".")
    doc = hc.record_run(root, review.resolve(), args.harness, force=args.force)
    if args.json:
        print(json.dumps(doc, indent=1))
        return 0
    g, w = doc["guide"], doc.get("wallclock") or {}
    money = " + ".join(x for x in (
        f"${g['usd']:.2f}" if g.get("usd") is not None else "",
        f"{g['aic']:.1f} AIC" if g.get("aic") is not None else "") if x)
    mins = (w.get("seconds") or 0) / 60
    model = (w.get("modelSeconds") or 0) / 60
    print(f"this guide      {money or 'unmeasured — ' + str(g.get('reason'))} · "
          f"{mins:.0f} min, of which model {model:.0f} min · {review / hc.REPORT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
