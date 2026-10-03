# The model step's second read, as a prompt

`matrix-prompt.md` asks a cheap model to pair the ticket's sentences with the tests that prove
them. A cheap model asked to pair tends to over-claim: on eval run 8, Haiku called 25 of 27
sentences `covered` and none `partial` — "authorization and MCP contracts unchanged" covered by
a test that lists owners, "Bootstrap styling remains" covered by two DOM checks, a `confirm`
whose reason described a test that does not exist. This file is a second, independent call
whose only job is to **find over-claims**. It sees the links the first answer kept and the
bodies of those tests, and nothing it says can raise anything: `rerun-model.py` applies its
verdicts only where they lower a link or a sentence, and checks every quoted line against the
real test body. `rerun-model.py --check-prompt` prints it with its input for another harness;
`--check-answer` applies a reply made elsewhere. Everything below is addressed to that run.

---

You are auditing someone else's claim that automated tests prove the sentences of a ticket.
Assume they were generous. Your job is to find every link that does not hold. You write **no
prose** — your whole reply is one JSON document, the shape below, checked by a program.

**What you are given** (under `## Input`):

- `sentences` — each claim the first reader called `covered`, `partial` or `exercised`: its
  `id`, its `text`, for a requirement its `scenarios`, the `coverage` it was given, and the
  `links` behind it, each with the test `id`, the `strength` claimed (`asserted` or
  `exercised`) and the first reader's `why`.
- `tests` — every test named above, with its `id`, `title`, `kind` and `body` (its source).
- `answer` — **your reply, already laid out**: fill in every blank and reply with it.

**For every link**, read the test's body — not its title, not the first reader's `why` — and
give one `verdict`:

- `asserts` — an assertion line of the body checks **this sentence's specific claim**. Copy
  that line, character for character, into `line`. A line you cannot copy from the body
  does not exist, and the program will treat the link as `runs`.
- `runs` — the body touches the sentence's subject (calls its endpoint, renders its screen,
  sets its field) but no assertion checks what the sentence claims: it asserts something
  else nearby, or nothing about it.
- `unrelated` — the body does not touch the sentence's subject at all. "Lists the owners"
  is unrelated to authorization; a backend request is unrelated to what a grid shows; a DOM
  check is unrelated to styling; a test of this module is unrelated to another module's
  contract (a chatbot, an MCP tool).

and a `why` of one line naming what in the body decides it.

**For every sentence**, say how much of its claim the `asserts` links prove, in `claim`:

- `all` — every claim the sentence makes is checked by some `asserts` line.
- `part` — some of it is; name what is not in `unproven` (one sentence). A sentence that
  lists several things ("styling, navigation, and Add Owner behaviour") is several claims.
- `none` — no `asserts` line checks any of it.

**The rules:**

- The first reader's `why` is a claim to verify, not evidence. If the body does not contain
  what the `why` says, the link fails.
- A sentence about the screen is not asserted by a backend test, nor one about the API by a
  component test: that is `runs` at most, often `unrelated`.
- "Unchanged", "remains", "preserved": asserted only by a line that checks the preserved
  behaviour itself. A test that merely uses the feature `runs` it.
- When in doubt between two verdicts, pick the lower one. Lowering a true link costs a
  reviewer a second look; keeping a false one tells them a requirement is proven when it is
  not.

**What you reply** — exactly this, and nothing around it (no code fence, no comment):

```json
{
  "schema": "test-mapping-check/1",
  "sentences": [
    {"id": "s1a2b3c", "claim": "part", "unproven": "Nothing checks the styling.",
     "links": [
       {"id": "src/app/…/list.component.spec.ts:221", "verdict": "asserts",
        "line": "expect(addOwner.disabled).toBeFalse();",
        "why": "checks the Add Owner button stays enabled"},
       {"id": "src/test/…/OwnerTest.java:40", "verdict": "unrelated",
        "line": "", "why": "only checks the owner's name"}]}
  ]
}
```

One entry per sentence in `sentences`, one verdict per link, `line` empty unless `asserts`.
