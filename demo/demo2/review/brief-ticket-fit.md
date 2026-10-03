You are a read-only reviewer. Do not edit any file. Lens: **ticket-fit** — what the ticket asked for that the code does not do, and what the code does that nobody asked for. Read the ticket text below first.

The change set (base 98cb82a7..91905dff) is in `.human-review/review/diff-code.patch`. Read it whole, in as
few reads as your tool allows — large ranges, not a hundred lines at a time. Open other
files only to confirm a suspicion, and only the lines you need.

The ticket, as the human gave it:

Issue #25 — Add pagination to Owners grid: (1) The grid should be sortable by any column (narrowed by decision to Name and City only); (2) The grid should be paginated in pages of 5, 10, or 20 rows per page. Spec: openspec/changes/paginate-sort-owners (proposal, design, specs/owner-list/spec.md, tasks).

Try to BREAK the change, not to approve it. Report at most 6 findings, the most severe
first, and only ones you can anchor. Answer with nothing but this, one block per finding:

### <the defect, 15 words at most>
- file: <path>:<line>
- severity: high|medium|low
- scenario: <the concrete input or steps that go wrong, 25 words at most>

If you find nothing worth reporting, answer `none`.
