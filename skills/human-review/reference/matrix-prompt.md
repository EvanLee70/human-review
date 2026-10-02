# The model step, as a prompt

The Tests tab's matrix is drawn by a script (`scripts/semcov.py`): the ticket's sentences on
the left — the issue, then the requirements of the OpenSpec change the branch implements
when there is one — the tests whose per-test coverage runs this PR's changed lines on the
right, and the pairing between them. The script *proposes* links on shared evidence (words
of the test's name and assertions, synonyms, literals, the changed lines its coverage ran),
and a shared word is a candidate, not proof. This file asks a **cheap** model, in one call,
to read the tests behind every proposed link and confirm or reject it, to pair the
sentences the script could not, and to say which sentences a recorded scope decision
narrowed. `rerun-model.py` appends all of it as JSON under `## Input` and hands the whole
thing to `claude -p --model haiku` with no tools. A GitHub Copilot session runs the same
step with its own cheap model (*Auto*, or `gpt-5-mini`): `rerun-model.py --prompt-only`
prints this prompt with its input, and `rerun-model.py --answer reply.json` checks and
installs the reply. Everything below is addressed to that run and not to a reader.

---

You are checking which automated tests prove the sentences of a ticket. You write **no
HTML, no files, no prose** — your whole reply is one JSON document, the shape below, and it
is checked by a program before anything uses it.

**What you are given** (under `## Input`):

- `sentences` — every sentence of the ticket that makes a claim, each with its `id`, its
  `text`, the heading it sits under (`section`), and:
  - `scripted` — the tests a script linked to it because they share words with it, each
    with the words it matched on (`why`). **These are guesses.** You confirm or reject each
    one.
  - `candidates` — other tests worth reading for it. May be empty.
  - for a requirement of an OpenSpec change: its `requirement` name and its `scenarios`,
    which say what the requirement means in practice.
- `decisions` — what the branch recorded deciding *not* to do, each with an `id` (`d1`,
  `d2`, …): assumptions it made, review findings it declined, scope cuts in its proposal.
- `answer` — **your reply, already laid out**: one entry per sentence, with a `review`
  slot for every scripted link. Fill in every blank (`coverage`, each `verdict` and `why`),
  add the tests you stand behind to `tests`, add `gap` / `decision` where they apply, and
  reply with the filled-in `answer` — nothing removed, nothing renamed.
- `context` — every sentence of the ticket in order. Context only.
- `tests` — every test named above, with its `id` (`path:line`), `title`, `kind` (UI / API
  / unit), `status` (what this branch did to it) and `body` (its source).

**What you reply** — exactly this, and nothing around it (no code fence, no comment):

```json
{
  "schema": "test-mapping/1",
  "sentences": [
    {"id": "s1a2b3c", "coverage": "covered",
     "tests": [{"id": "src/test/…/VisitTest.java:231", "strength": "asserted",
                "why": "POSTs a visit with no vet and asserts it is stored with none"}],
     "review": [{"id": "src/test/…/VisitTest.java:231", "verdict": "confirm",
                 "why": "asserts the stored vet is null"},
                {"id": "src/test/…/OwnerTest.java:40", "verdict": "reject",
                 "why": "shares 'visit' but only checks the owner's name"}]},
    {"id": "s4d5e6f", "coverage": "missing", "tests": [],
     "gap": "Nothing clears the vet on an existing visit and reads it back."},
    {"id": "s7a8b9c", "coverage": "narrowed", "decision": "d3",
     "tests": [{"id": "src/app/…/list.component.spec.ts:88", "strength": "asserted",
                "why": "asserts only Name and City headers are sortable"}],
     "gap": "Sorting is limited to Name and City; the ticket asks for any column."}
  ]
}
```

One entry per sentence in `sentences`, every one of them, none twice:

- `review` — **one verdict per test in that sentence's `scripted`, every one, none
  twice**: `confirm` when the test's body really checks (or at least runs) what the
  sentence claims, `reject` when the shared words are a coincidence — and a `why` of one
  line naming what in the body decides it. A confirmed test goes in `tests`; a rejected
  one must not. A sentence with an empty `scripted` has no `review`.
- `coverage` — `covered` (a test asserts every claim the sentence makes), `partial` (a
  test asserts part of it — say which part is not, in `gap`), `exercised` (a test runs
  through it but asserts none of it), `missing` (no test proves or reaches it),
  `narrowed` (the branch deliberately delivers less than the sentence says, and one of
  `decisions` records that — name it in `decision`), or `n/a` (not a claim at all:
  background, history, motivation).
- `tests` — the tests that pin it, from `scripted` (confirmed) or `candidates`, each with
  `strength`: `asserted` when the body checks the claim, `exercised` when it only runs the
  code — and a `why` of one line. Only ids from `tests`; never invent or retype one.
  `missing` and `n/a` have `"tests": []`; `narrowed` may list the tests that pin what was
  delivered instead.
- `gap` (optional, one sentence) — what is not proven, or how it was narrowed; `gapKind`
  `requirement` when the hole is in the ticket itself rather than in the tests.

**The rules that make this worth paying for:**

- Read the test **body**, not its name. A name says what someone meant to test; the
  assertions say what is tested. A test that sets the field and never reads it back has
  `exercised` it.
- Judge the sentence's **whole claim**. "Sortable by any column" is not covered by a test
  that sorts by two columns — and if a decision says sorting was limited on purpose, it is
  `narrowed`, not `covered` and not `missing`.
- `covered` and `partial` need at least one `asserted` test; `exercised` needs at least one
  test and no `asserted` one. An answer that breaks this is refused.
- The honest answer is usually not the flattering one. A sentence with nothing behind it is
  `missing`, and saying so is the value of the matrix.
- Prefer few, strong pairings over many weak ones: three tests that assert the claim are
  the answer; ten that mention the same entity are noise.
