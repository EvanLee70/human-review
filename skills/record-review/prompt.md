You implemented a change in this conversation. Now review it adversarially and record
what you decided — the record is the one thing about this change that only you can write.

The change set is `<base>..HEAD` plus whatever is still uncommitted. `<base>` is what you
were given; with nothing given, `git merge-base HEAD origin/main`.

1. Read this repo's own rules first — AGENTS.md / CLAUDE.md, and REVIEW.md if there is
   one. Do not ask for clarification: everything you need is the code, the ticket, and
   what you remember deciding while you wrote it.
2. If the implementation is not committed yet, commit it now — the implementation alone,
   nothing else in that commit. The last lines you write in the message are:
       Claude-Session: <this session's id>
   If it is already committed, in one commit or in several, leave those commits alone;
   the last of them is the one step 5 names.
3. Review that commit adversarially. Do NOT pass --fix to anything, and do not let a
   reviewer edit a file: you decide what to accept, one finding at a time, and that
   decision is the artifact this flow exists for.
   - **Claude Code** — run  /code-review high  over the change set.
   - **Any other harness** (VS Code Copilot Chat, Copilot CLI, …) — start four reviewer
     subagents in parallel, read-only, each with one lens: correctness (what input makes
     it return the wrong thing or crash), security, drift from the ticket (what it asked
     for that the code does not do, and what the code does that it never asked for), and
     tests (what the change can break with every test still green). Prompt each one to
     try to *break* the change, not to approve it, and to answer with file:line, the
     concrete failing scenario, and a severity. If you can pick their model, pick one
     other than yours: a reviewer that thinks like the author misses what the author
     missed.
   Whichever way it ran goes in the `reviewers:` field verbatim, and each finding's
   `source:` names the pass or the subagent that raised it.
4. Write review-points.md at the repo root. Format: see reference/review-points.md.
   **Terse.** The reader skims it and jumps into the code; every extra sentence is one
   they have to wade through. One item per finding, and only its fields:

       ### <what it is, 15 words at most>
       - file: path:line
       - severity: high|medium|low         (Fixed and Ignored; never on an assumption)
       - why: <15 words at most>           (Ignored: why declined. Assumptions: why this reading)
       - alternative: <the reading not taken, 15 words at most>    (Assumptions only)
       - confidence: 0.xx                                         (Assumptions only)

   No prose under an item, no preamble, no closing summary, no restating the diff or the
   ticket. More detail only if the human asks for it.
     Fixed       — what you repaired because the review was right.
     Ignored     — what you read and declined. An empty Ignored section after a
                   multi-agent review is not credible; if you accepted everything, say so
                   in one line.
     Assumptions — what you decided that the ticket did not: the only section nobody
                   else can write, because it is not in the diff. They come from this
                   conversation, not from the reviewers — scroll back to where you chose.
   Every entry names a file:line. An unanchored entry is dropped by the build.
   Check it parses before you commit:  review-points.py --check

   `confidence:` — a number in [0, 1] on every assumption, two decimals at most:

       1.0         the ticket left no other reading
       0.5         a coin flip between two readings
       below 0.3   you expect to be corrected

   The number is rendered to the reviewer beside the word "assumption", and it is what
   decides where they spend their attention — so it has to be what you actually think,
   not what is comfortable to hand over. **0.9 on everything is a lie the page will
   show**: a pile of assumptions that are all nearly certain reads as an agent that
   never noticed it was guessing, and the one reading you were genuinely unsure about
   becomes indistinguishable from the six you were not. If a call was close, write 0.5
   and let the reviewer go and look. Being told where to look is the entire point of
   the section; a flat 0.9 tells them nothing and costs you nothing, which is exactly
   what makes it worthless.
5. Commit the fixes and review-points.md together. The subject line starts with
   `[auto-fix]` — e.g. `[auto-fix] apply 3 review findings on visit/vet` — and so does
   every later commit that applies a reviewer's finding, so `git log --grep='\[auto-fix\]'`
   finds everything you changed on the review's say-so.
   The last lines you write in the message are:
       Review-Points: review-points.md
       Implements: <sha of the last implementation commit, from step 2>
       Claude-Session: <this session's id>
   Write them as the final lines of your message, one key per line, nothing between
   them. If the harness then appends a paragraph of its own — Claude Code adds
   `Co-Authored-By: Claude …` — leave it alone: the parser reads these keys out of the
   whole message body, not only out of git's trailer block, so a paragraph after them
   changes nothing. Do not move them, do not repeat them below it.
   `Claude-Session:` is `$CLAUDE_CODE_SESSION_ID`. Outside Claude Code there is no such
   id to read — leave that line out of both commits rather than invent one.
5b. Then prepare the pull-request comments: write .human-review/pr-comments.json, the exact
   body of GitHub's create-a-review call, one inline comment per item of review-points.md,
   each on the line of the diff it is about. Format and rules: reference/pr-comments.md.
   Check it:  push-pr-comments.py --check   — and fix every anchor it had to downgrade.
   Do not post it; the human presses the button that does.
6. Stop. Do not push, do not open a PR, do not build a review page — that is
   /human-review, and the human runs it when they want it.

---

The paths above, resolved. `human-review` is installed **beside** this skill — in the
plugin, in this repo (`skills/human-review/`) and in a `~/.copilot/skills/` symlink alike —
so they are all relative to the folder this prompt is in. Do not search the disk for them:

    ../human-review/reference/review-points.md    the format
    ../human-review/scripts/review-points.py      the parser
    ../human-review/reference/pr-comments.md      the PR comments
    ../human-review/scripts/push-pr-comments.py   their check

(In the repository that ships them: skills/human-review/reference/review-points.md and
skills/human-review/scripts/review-points.py, and so on.)

`review-points.py --check` prints what it understood and writes nothing; it exits 4 on a
file it cannot read and 5 on one whose every entry is unanchored. Both of those mean the
file is not yet worth committing. Run it from the repository root.
