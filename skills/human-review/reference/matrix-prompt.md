# The model step, as a prompt

The Tests tab's matrix is drawn by a script (`scripts/semcov.py`): the ticket's sentences on
the left, the tests whose per-test coverage runs this PR's changed lines on the right, and
the pairing between them — **scripted wherever shared evidence decides it** (words of the
test's name and assertions, synonyms, literals, the changed lines its coverage ran). What
is left is the sentences the script could not pair, each with a few candidate tests. This
file asks a **cheap** model about those and nothing else: `rerun-model.py` appends the
open sentences and their candidates as JSON under `## Input` and hands the whole thing to
`claude -p --model haiku` with no tools. A GitHub Copilot session runs the same step with
its own cheap model (*Auto*, or `gpt-5-mini`): `rerun-model.py --prompt-only` prints this
prompt with its input, and `rerun-model.py --answer reply.json` checks and installs the
reply. Everything below is addressed to that run and not to a reader.

---

You are pairing sentences of a ticket with the automated tests that prove them. You write
**no HTML, no files, no prose** — your whole reply is one JSON document, the shape below,
and it is checked by a program before anything uses it.

**What you are given** (under `## Input`):

- `sentences` — the sentences of the ticket nobody has paired yet, each with its `id`,
  its `text`, the heading it sits under (`section`), and `candidates`: the ids of the
  tests most likely to be about it. The list may be empty.
- `context` — every sentence of the ticket in order, so you can read each open one where
  it sits. Context only: do not answer for a sentence that is not in `sentences`.
- `tests` — every candidate, with its `id` (`path:line`), `title`, `kind` (UI / API /
  unit), `status` (what this branch did to it) and `body` (its source).

**What you reply** — exactly this, and nothing around it (no code fence, no comment):

```json
{
  "schema": "test-mapping/1",
  "sentences": [
    {"id": "s1a2b3c", "coverage": "covered",
     "tests": [{"id": "src/test/…/VisitTest.java:231", "strength": "asserted",
                "why": "POSTs a visit with no vet and asserts it is stored with none"}]},
    {"id": "s4d5e6f", "coverage": "missing", "tests": [],
     "gap": "Nothing clears the vet on an existing visit and reads it back."}
  ]
}
```

One entry per sentence in `sentences`, every one of them, none twice:

- `coverage` — `covered` (a test asserts every claim the sentence makes), `partial` (a
  test asserts part of it — say which part is not in `gap`), `exercised` (a test runs
  through it but asserts none of it), `missing` (no candidate proves or reaches it), or
  `n/a` (not a claim at all: background, history, motivation — "we had this bug before").
- `tests` — the candidates that pin it, each with `strength`: `asserted` when the test
  body checks the claim, `exercised` when it only runs the code that implements it — and
  a `why` of one line saying what in the body does that. Only ids from `tests`; never
  invent or retype one. `missing` and `n/a` have `"tests": []`.
- `gap` (optional, one sentence) — what is not proven; `gapKind` `requirement` when the
  hole is in the ticket itself rather than in the tests.

**The rules that make this worth paying for:**

- Read the test **body**, not its name. A name says what someone meant to test; the
  assertions say what is tested. A test that sets the field and never reads it back has
  `exercised` it.
- `covered` and `partial` need at least one `asserted` test; `exercised` needs at least one
  test and no `asserted` one. An answer that breaks this is refused.
- The honest answer is usually not the flattering one. A sentence with nothing behind it is
  `missing`, and saying so is the value of the matrix.
- Prefer few, strong pairings over many weak ones: three tests that assert the claim are
  the answer; ten that mention the same entity are noise.
