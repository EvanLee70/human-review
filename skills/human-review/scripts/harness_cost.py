"""The four components of a change's bill, whichever harness spent each one.

A change reviewed with this toolkit is paid for four times, and the `$` tab names each:

  1. **implementation** — writing the code, up to `/record-review prepare`;
  2. **review** — finding: `prepare` → the reviewers done (their subagents included);
  3. **auto-fixes** — taking the review's advice: the reviewers done → the last `finish`,
     every `RR ci --push` round included;
  4. **this guide** — the page's own model work: the session that ran `/human-review`,
     plus the paid model steps it shelled out to (`.model-runs.json`, `.film-runs.json`).

1–3 are **recorded by `/record-review`** in the harness that ran them, into the committed
`review-cost.json` beside `review-points.md` (schema `reference/review-cost.schema.json`).
4 is recorded by the `/human-review` run itself into `.human-review/report-cost.json`, with
the wall-clock time of the run; a refresh adds its own time there and never its money. A
branch recorded before either file existed is *derived* instead, from the same stores, and
says so — the derivation is a scan, the record is a measurement.

Three harnesses, three stores, one entry shape:

  * **Claude Code** — `~/.claude/projects/**/<session>.jsonl`, priced at API list price by
    `review-cost.py`, which owns the prices and the dedupe; this module only picks windows.
  * **Copilot CLI** — `~/.copilot/session-store.db`, `assistant_usage_events` per model
    call: tokens, and `total_nano_aiu` (1e9 = one AI credit). Matched by `cwd` (the repo or
    one of its worktrees), branch and time, and by what the session was *for* — its summary
    and first prompt name `/record-review` or `/human-review`. The CLI exports no session id
    to the shell it runs (`COPILOT_AGENT_SESSION_ID` is read when set, which is only the
    cloud agent), so the match is the only link.
  * **VS Code Copilot Chat** — `workspaceStorage/<hash>/chatSessions/<id>.jsonl`, an op-log
    replayed into the chat; each request (one user turn) carries `copilotCredits`. A chat
    *forked* in VS Code copies its parent's requests, credits included, under new ids: they
    are deduped on `(responseTimestamp, elapsedMs)`, which the copy keeps.

Copilot is shown in AI credits, its native unit. Its dollar figure is GitHub's own billing
rate — `AIC_USD`, one credit = $0.01 since 22 Jul 2026, the rate the billing API's
`grossAmount` implies and `copilot-usage` converts with — which is a *billed* price, where
the Claude figures are *list-price equivalents*. The page says which is which.
"""
from __future__ import annotations

import datetime as dt
import functools
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

HERE = Path(__file__).resolve().parent

CLAUDE, COPILOT_CLI, VSCODE = "claude-code", "copilot-cli", "vscode-copilot"
HARNESS_LABELS = {CLAUDE: "Claude Code", COPILOT_CLI: "Copilot CLI",
                  VSCODE: "VS Code Copilot Chat"}

#: Dollars per Copilot AI credit. GitHub moved Copilot from premium requests ($0.04 each)
#: to AI credits at $0.01 each on 2026-07-22; the billing API's `grossAmount` is credits ×
#: 0.01, which is how `copilot-usage` converts. A billed rate, not a list price.
AIC_USD = 0.01
AIC_RATE_NOTE = ("Copilot at GitHub's billing rate, $0.01 per AI credit (since 22 Jul 2026); "
                 "Claude at API list price — nobody on a subscription is billed that")
NANO_PER_AIC = 1_000_000_000

RECORD_FILE = "review-cost.json"          # committed, beside review-points.md
REPORT_FILE = "report-cost.json"          # in .human-review/, beside .steps.json
RECORD_SCHEMA = "review-cost/1"
REPORT_SCHEMA = "report-cost/1"

COMPONENTS = (("implementation", "implementation"),
              ("review", "review"),
              ("autofix", "auto-fixes"),
              ("guide", "this guide"))

#: Prompts that are not implementation even when typed into the chat that wrote the code.
NOT_IMPLEMENTATION = ("/record-review", "/human-review")


def copilot_db() -> Path:
    return Path(os.environ.get("HUMAN_REVIEW_COPILOT_DB")
                or os.path.expanduser("~/.copilot/session-store.db"))


def vscode_roots() -> list[Path]:
    given = os.environ.get("HUMAN_REVIEW_VSCODE_USER")
    if given is not None:
        return [Path(p) for p in given.split(os.pathsep) if p]
    return [Path(os.path.expanduser(p)) for p in (
        "~/Library/Application Support/Code/User",
        "~/Library/Application Support/Code - Insiders/User",
        "~/.config/Code/User", "~/.config/Code - Insiders/User")]


# ----------------------------------------------------------------------------- time

def parse(raw) -> "dt.datetime | None":
    """Any stamp this module meets — ISO with Z or offset, or epoch milliseconds — as an
    aware UTC datetime. Naive ISO is taken as UTC, which is what the Copilot DB writes."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, dt.datetime):
        t = raw
    elif isinstance(raw, (int, float)):
        t = dt.datetime.fromtimestamp(raw / 1000, dt.timezone.utc)
    else:
        try:
            t = dt.datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def iso(t) -> "str | None":
    t = parse(t)
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds") if t else None


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _within(t, lo, hi) -> bool:
    return t is not None and (lo is None or t >= lo) and (hi is None or t <= hi)


def normalize_harness(raw) -> str:
    """`Copilot CLI`, `copilot -p`, `copilot-cli` → `copilot-cli`; VS Code's chat →
    `vscode-copilot`; anything Claude → `claude-code`; else ''. The front-matter of a
    branch recorded by hand says it in prose, and a reader of it must not care which."""
    s = str(raw or "").lower()
    if not s:
        return ""
    if "vs code" in s or "vscode" in s or "vs-code" in s:
        return VSCODE
    if "copilot" in s:
        return COPILOT_CLI
    if "claude" in s:
        return CLAUDE
    return s.strip()


# ----------------------------------------------------------------------------- git

def git(root: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def fork_time(root: Path, base: str) -> "dt.datetime | None":
    """The earlier of the merge-base's commit time and the oldest author date on the
    branch — `authoring-sessions.py`'s rule, so a rebase does not cut off the first commit
    and a file written for some older branch is not billed to this one."""
    fork = git(root, "merge-base", base, "HEAD") or base
    stamps = git(root, "log", "-1", "--format=%ct", fork).split()
    stamps += git(root, "log", "--format=%at", f"{fork}..HEAD").split()
    secs = [int(s) for s in stamps if s.isdigit()]
    return dt.datetime.fromtimestamp(min(secs), dt.timezone.utc) if secs else None


def committed_at(root: Path, sha: str | None) -> "dt.datetime | None":
    return parse(git(root, "log", "-1", "--format=%cI", sha)) if sha else None


def changed_files(root: Path, base: str) -> set[str]:
    fork = git(root, "merge-base", base, "HEAD") or base
    return {f for f in git(root, "diff", "--name-only", fork).splitlines() if f}


def worktree_roots(root: Path) -> set[str]:
    """The repository and every worktree of it: a session started in either is this repo's."""
    out = {str(Path(root).resolve())}
    for line in git(root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            out.add(str(Path(line[9:]).resolve()))
    return out


def _in_repo(cwd: str | None, roots: set[str]) -> bool:
    if not cwd:
        return False
    try:
        real = str(Path(cwd).resolve())
    except OSError:
        real = cwd
    return any(real == r or real.startswith(r + os.sep) for r in roots)


def session_kind(text: str | None) -> str:
    """What a session was for, from its own words: whichever of `record-review` and
    `human-review` it names first. `Run the human-review skill … after /record-review`
    is a page run; `You are continuing a /record-review` is a review."""
    m = re.search(r"\b(record|human)-review\b", text or "")
    return {"record": "record-review", "human": "human-review"}[m.group(1)] if m else "other"


# ----------------------------------------------------------------------------- entries

def entry(harness: str, session: str, what: str, window=(None, None), tokens: int = 0,
          models: dict | None = None, usd: float | None = None, aic: float | None = None,
          calls: int = 0, model_seconds: float = 0.0, note: str | None = None,
          subagent_models: list[str] | None = None) -> dict:
    """One harness session's share of one component. `usd` is a Claude list price, `aic`
    Copilot credits; never both, so a reader always knows which kind of number it is.

    `subagent_models` names the models of the agents the session forked inside the window,
    apart from its own: in the review component those agents ARE the reviewers, and the
    session around them is the orchestrator briefing them, usually on a bigger model."""
    out = {"harness": harness, "session": session, "what": what,
           "window": [iso(window[0]), iso(window[1])],
           "tokens": int(tokens or 0),
           "models": {k: int(v) for k, v in (models or {}).items() if v},
           "usd": None if usd is None else round(float(usd), 4),
           "aic": None if aic is None else round(float(aic), 2),
           "calls": int(calls or 0), "modelSeconds": round(float(model_seconds or 0), 1)}
    if note:
        out["note"] = note
    if subagent_models:
        out["subagentModels"] = list(subagent_models)
    return out


def component(key: str, entries: list[dict], reason: str | None = None,
              window=(None, None), source: str = "recorded") -> dict:
    """A component's row: its entries, their sums, and — when nothing was found — why.

    An unmeasured component carries a reason and no zero: `$0.00` and "nobody could see
    this" read the same and mean opposite things."""
    entries = [e for e in entries if e]
    label = dict(COMPONENTS).get(key, key)
    usd = sum(e["usd"] or 0.0 for e in entries)
    aic = sum(e["aic"] or 0.0 for e in entries)
    return {"key": key, "label": label, "measured": bool(entries),
            "reason": None if entries else (reason or "nothing on this machine recorded it"),
            "source": source, "window": [iso(window[0]), iso(window[1])],
            "harnesses": sorted({e["harness"] for e in entries}),
            "entries": entries,
            "tokens": sum(e["tokens"] for e in entries),
            "usd": round(usd, 4) if any(e["usd"] is not None for e in entries) else None,
            "aic": round(aic, 2) if any(e["aic"] is not None for e in entries) else None,
            "modelSeconds": round(sum(e.get("modelSeconds") or 0 for e in entries), 1)}


def usd_equivalent(c: dict) -> float:
    return (c.get("usd") or 0.0) + (c.get("aic") or 0.0) * AIC_USD


# ----------------------------------------------------------------------------- Claude

@functools.lru_cache(maxsize=1)
def rc():
    """`review-cost.py`, which owns the prices, the dedupe and the subagent discovery."""
    spec = importlib.util.spec_from_file_location("review_cost_for_harness",
                                                  HERE / "review-cost.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _intervals_union(spans) -> float:
    total, cur = 0.0, None
    for a, b in sorted(s for s in spans if s[0] and s[1] and s[1] >= s[0]):
        if cur and a <= cur[1]:
            cur[1] = max(cur[1], b)
        else:
            if cur:
                total += (cur[1] - cur[0]).total_seconds()
            cur = [a, b]
    if cur:
        total += (cur[1] - cur[0]).total_seconds()
    return total


def claude_model_spans(path: Path, lo, hi) -> list[tuple]:
    """When the model was working: each stretch from a user/tool record to the last
    assistant record answering it. Waiting on a tool or a human is not in it."""
    spans, start, last = [], None, None
    for rec in rc()._rows(path):
        t = parse(rec.get("timestamp"))
        if t is None or not _within(t, lo, hi):
            continue
        kind = rec.get("type")
        if kind == "user":
            if start and last:
                spans.append((start, last))
            start, last = t, None
        elif kind == "assistant" and start is not None:
            last = t
    if start and last:
        spans.append((start, last))
    return spans


def claude_entry(session: str | None, lo, hi, what: str) -> dict | None:
    """The session's turns in `[lo, hi]` plus the agents it started inside the window."""
    if not session:
        return None
    path = rc().transcript(session)
    if path is None:
        return None
    lo, hi = parse(lo), parse(hi)
    data = rc()._window(path, lo, hi)
    if not data.get("messages"):
        return None
    spans = claude_model_spans(path, lo, hi)
    sub_models: list[str] = []
    n_agents = 0
    for agent in rc().subagent_transcripts(path):
        first, _last = rc().agent_span([agent])
        if first is not None and _within(first, lo, hi):
            n_agents += 1
            spans += claude_model_spans(Path(agent), lo, hi)
            sub_models += [m for m in transcript_models(Path(agent)) if m not in sub_models]
    out = entry(CLAUDE, session, what, (lo, hi), data["tokens"], data.get("models"),
                usd=data["cost"], calls=data["messages"],
                model_seconds=_intervals_union(spans), subagent_models=sub_models)
    if n_agents:
        # How many, not only which: "Sonnet 5.5" says what the reviewers ran on, and the
        # review chip's "Sonnet 5.5 (4 subagents)" needs the count to say how many read.
        out["subagents"] = n_agents
    return out


#: What the harness types into a conversation on its own, never a person: a background
#: task finishing, a skill's body loaded under its slash command, a hook's reminder.
_HARNESS_PROMPTS = ("<task-notification>", "<system-reminder>", "<local-command-",
                    "Base directory for this skill:")


def _real_prompt(rec: dict) -> bool:
    """A user record that opens a new turn: somebody (or `claude -p`) asked for something.

    A tool result is the model's own turn going on; a meta record or a `<task-notification>`
    is the harness waking the same turn up — eval run 8's `ci --push` waited on CI in the
    background and its turn ended seven minutes later, on the notification, not before it."""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return False
        text = " ".join(b.get("text") or "" for b in content if isinstance(b, dict))
    else:
        text = str(content or "")
    return bool(text.strip()) and not text.lstrip().startswith(_HARNESS_PROMPTS)


def claude_turn_bounds(session: str | None, t) -> tuple:
    """`(start, end)` of the turn of `session` that was running at `t`: from the prompt
    that opened it to the last record before the next prompt. `(None, None)` when the
    transcript is gone or `t` is before its first prompt.

    The boundary a stamp cannot give: `lastCiAt` is written when `ci` *starts*, `.started`
    when Step 2 runs, and the model's work on either goes on until its turn ends."""
    t = parse(t)
    path = rc().transcript(session) if session and t else None
    if path is None:
        return None, None
    start = end = None
    for rec in rc()._rows(path):
        when = parse(rec.get("timestamp"))
        if when is None:
            continue
        if _real_prompt(rec):
            if when <= t:
                start, end = when, when
                continue
            if start is not None:
                break
        if start is not None and when >= start:
            end = when
    return start, end


def claimed_sessions(root: Path, base: str) -> list[str]:
    """The `Claude-Session:` trailers of the branch's own commits: the record of which
    conversation wrote it, which outlives `.human-review/` and a rebase."""
    fork = git(root, "merge-base", base, "HEAD") or base
    out = git(root, "log", "--format=%(trailers:key=Claude-Session,valueonly)", f"{fork}..HEAD")
    return sorted({s.strip() for s in out.splitlines() if s.strip()})


def _missing_claude(sessions, what: str) -> str:
    """Why a Claude window came back empty, in the words that say what to do about it."""
    sessions = [s for s in sessions or [] if s]
    if not sessions:
        return (f"no Claude session is named for the {what} — `.human-review/review/state.json` "
                "has no `session`/`sessions` and no commit carries a `Claude-Session:` "
                "trailer; rerun `record-review.py finish` inside the session that did it")
    gone = [s for s in sessions if rc().transcript(s) is None]
    if gone:
        return (f"the transcript of session {', '.join(s[:8] for s in gone)} is not under "
                f"{rc().PROJECTS} — cleaned up, or recorded on another machine")
    return (f"session {', '.join(s[:8] for s in sessions)} has no model turn inside the "
            f"{what}'s window")


_MODEL_FIELD = re.compile(r'"model"\s*:\s*"([^"]+)"')


@functools.lru_cache(maxsize=256)
def _transcript_model_ids(path: str, size: int, mtime: int) -> tuple[str, ...]:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    seen: dict[str, None] = {}
    for raw in _MODEL_FIELD.findall(text):
        if raw.startswith("claude-"):
            seen.setdefault(raw, None)
    return tuple(seen)


def transcript_model_ids(path: Path) -> list[str]:
    """Every Claude model id a transcript's assistant turns name, in order of first use."""
    try:
        st = Path(path).stat()
    except OSError:
        return []
    return list(_transcript_model_ids(str(path), st.st_size, st.st_mtime_ns))


def transcript_models(path: Path) -> list[str]:
    """`transcript_model_ids`, as the labels the cost rows use (`Sonnet 5.5`)."""
    out: list[str] = []
    for raw in transcript_model_ids(path):
        name = rc().label(raw)
        if name not in out:
            out.append(name)
    return out


def claude_authors(root: Path, base: str) -> list[dict]:
    """The Claude conversations that wrote the change, by `review-cost.py:authoring_cost`'s
    rules — edit tools on a changed file, a shell-only one only when a `Claude-Session:`
    trailer vouches for it, and none of that when another agent co-signed the branch."""
    found = rc().authoring_cost(base, root)
    return found.get("sessions") or [] if found.get("measured") else []


# ----------------------------------------------------------------------------- Copilot CLI

def _connect() -> "sqlite3.Connection | None":
    path = copilot_db()
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        con.execute("SELECT 1 FROM assistant_usage_events LIMIT 1")
        return con
    except sqlite3.Error:
        return None


def copilot_sessions(root: Path, branch: str | None = None) -> list[dict]:
    """Every Copilot CLI session started in this repository (or a worktree of it), with
    its span of model calls and what it was for. `branch` keeps sessions on that branch
    and those that recorded none."""
    con = _connect()
    if con is None:
        return []
    roots = worktree_roots(root)
    try:
        spans = {r["session_id"]: (r["a"], r["b"]) for r in con.execute(
            "SELECT session_id, MIN(created_at) a, MAX(created_at) b "
            "FROM assistant_usage_events GROUP BY session_id")}
        first = {}
        for r in con.execute("SELECT session_id, user_message FROM turns "
                             "ORDER BY session_id, turn_index"):
            first.setdefault(r["session_id"], r["user_message"] or "")
        out = []
        for s in con.execute("SELECT id, cwd, branch, summary, created_at FROM sessions"):
            if not _in_repo(s["cwd"], roots):
                continue
            if branch and s["branch"] and s["branch"] != branch:
                continue
            a, b = spans.get(s["id"], (None, None))
            text = (s["summary"] or "").strip() or first.get(s["id"], "")
            out.append({"id": s["id"], "cwd": s["cwd"], "branch": s["branch"] or "",
                        "summary": text, "kind": session_kind(text),
                        "created": parse(s["created_at"]), "first": parse(a),
                        "last": parse(b)})
        return sorted(out, key=lambda s: s["first"] or s["created"] or now())
    finally:
        con.close()


def copilot_events(session: str, lo=None, hi=None) -> list[dict]:
    con = _connect()
    if con is None:
        return []
    lo, hi = parse(lo), parse(hi)
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT model, agent_id, initiator, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, total_nano_aiu, duration_ms, created_at "
            "FROM assistant_usage_events WHERE session_id = ? ORDER BY id", (session,))]
    finally:
        con.close()
    out = []
    for r in rows:
        r["when"] = parse(r["created_at"])
        if _within(r["when"], lo, hi):
            out.append(r)
    return out


def copilot_entry(session: str, lo, hi, what: str, note: str | None = None) -> dict | None:
    events = copilot_events(session, lo, hi)
    if not events:
        return None
    models: dict[str, int] = {}
    tokens = 0
    spans = []
    for e in events:
        t = sum(int(e.get(k) or 0) for k in ("input_tokens", "output_tokens"))
        tokens += t
        models[e["model"]] = models.get(e["model"], 0) + t
        dur = dt.timedelta(milliseconds=int(e.get("duration_ms") or 0))
        spans.append((e["when"] - dur, e["when"]))
    aic = sum(int(e.get("total_nano_aiu") or 0) for e in events) / NANO_PER_AIC
    sub_events = [e for e in events if e.get("agent_id")]
    subs = len({e["agent_id"] for e in sub_events})
    if subs:
        # Subagents share the session id and carry their own agent_id, so the split is
        # exact: on hr-try-4 the four reviewers were 2.2 AIC on gpt-5.6-luna and the main
        # agent orchestrating them 250.4 on claude-sonnet-5 — the review's cost was not
        # the reviewing.
        sub_aic = sum(int(e.get("total_nano_aiu") or 0) for e in sub_events) / NANO_PER_AIC
        sub_models = sorted({e["model"] for e in sub_events})
        note = ((note + "; ") if note else "") + (
            f"{subs} subagent(s) inside: {sub_aic:.1f} AIC on {', '.join(sub_models)}, "
            f"the main agent {aic - sub_aic:.1f}")
    else:
        sub_models = []
    return entry(COPILOT_CLI, session, what,
                 (min(e["when"] for e in events), max(e["when"] for e in events)),
                 tokens, models, aic=aic, calls=len(events),
                 model_seconds=_intervals_union(spans), note=note,
                 subagent_models=[rc().label(m) for m in sub_models])


def copilot_last_subagent_call(sessions: list[str], lo, hi) -> "dt.datetime | None":
    """When the reviewers stopped, in a Copilot run: its last subagent model call."""
    stamps = [e["when"] for s in sessions for e in copilot_events(s, lo, hi)
              if e.get("agent_id") or e.get("initiator") == "sub-agent"]
    return max(stamps) if stamps else None


def copilot_wrote(session: str, files: set[str], root: Path) -> bool:
    """Did this session edit or create a file of the change set? `session_files` keeps
    every path a session touched with the tool that touched it."""
    con = _connect()
    if con is None:
        return False
    try:
        rows = con.execute("SELECT file_path, tool_name FROM session_files "
                           "WHERE session_id = ?", (session,)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    return any(_rel(r["file_path"], root) in files for r in rows
               if (r["tool_name"] or "").lower() in ("edit", "create", "write",
                                                     "str_replace_editor", "apply_patch"))


def _rel(path: str, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):
        return path


# ----------------------------------------------------------------------------- VS Code

_DROP = {"result", "promptTokenDetails", "contentReferences", "codeCitations",
         "outputBuffer", "modelState", "responseMarkdownInfo", "followups", "variableData"}


def _edits(value, sink: list) -> None:
    for item in (value if isinstance(value, list) else [value]):
        if isinstance(item, dict) and item.get("kind") == "textEditGroup":
            uri = item.get("uri") or {}
            p = uri.get("fsPath") or uri.get("path")
            if p and p not in sink:
                sink.append(p)


def replay_chat(path: Path) -> tuple[dict | None, list[str]]:
    """A chatSessions op-log folded back into the chat it describes (line 0 a snapshot,
    `kind:1` sets, `kind:2` appends at a key path), and the files its responses edited.
    The responses are 99% of the bytes and are dropped once their edits are read."""
    state, edits = None, []

    def scrub(r):
        _edits(r.pop("response", None), edits)
        for k in _DROP:
            r.pop(k, None)
        return r

    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, []
    if str(path).endswith(".json"):
        try:
            state = json.loads("\n".join(lines))
            state["requests"] = [scrub(r) for r in state.get("requests") or []
                                 if isinstance(r, dict)]
        except (ValueError, AttributeError):
            return None, []
        return state, edits
    for line in lines:
        try:
            op = json.loads(line)
        except ValueError:
            continue
        kind, keys, val = op.get("kind"), op.get("k") or [], op.get("v")
        if kind == 0:
            state = val if isinstance(val, dict) else None
            if state:
                state["requests"] = [scrub(r) for r in state.get("requests") or []
                                     if isinstance(r, dict)]
            continue
        if not isinstance(state, dict) or not keys:
            continue
        last = keys[-1]
        if last == "response":
            _edits(val, edits)
            continue
        if last in _DROP:
            continue
        try:
            cur = state
            for k in keys[:-1]:
                cur = cur[k]
            if kind == 1:
                cur[last] = val
            elif kind == 2:
                if last == "requests":
                    val = [scrub(r) for r in val if isinstance(r, dict)]
                if isinstance(cur[last], list):
                    cur[last].extend(val if isinstance(val, list) else [val])
        except (KeyError, IndexError, TypeError):
            continue
    return state, edits


def _workspace_folder(chat_dir: Path) -> str | None:
    try:
        meta = json.loads((chat_dir.parent / "workspace.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    uri = meta.get("folder") or ""
    return unquote(urlparse(uri).path) if uri.startswith("file://") else None


def vscode_chats(root: Path) -> list[dict]:
    """Every VS Code chat opened on this repository, as requests with their credits.

    A request's time is when its response landed (`responseTimestamp`), and it started
    `elapsedMs` before that. Forked chats repeat their parent's requests under new ids;
    the pair `(responseTimestamp, elapsedMs)` survives the copy, so it is the dedupe key."""
    roots = worktree_roots(root)
    seen: set = set()
    chats = []
    files = []
    for base in vscode_roots():
        files += sorted((base / "workspaceStorage").glob("*/chatSessions/*.json*"))
    for f in sorted(files, key=lambda p: p.stat().st_mtime if p.exists() else 0):
        folder = _workspace_folder(f.parent)
        if not folder or not _in_repo(folder, roots):
            continue
        state, edits = replay_chat(f)
        if not isinstance(state, dict):
            continue
        reqs = []
        for r in state.get("requests") or []:
            if not isinstance(r, dict):
                continue
            end = parse(r.get("responseTimestamp")) or parse(r.get("timestamp"))
            key = (r.get("responseTimestamp"), r.get("elapsedMs"))
            if key[0] is not None and key in seen:
                continue
            seen.add(key)
            start = (end - dt.timedelta(milliseconds=int(r.get("elapsedMs") or 0))
                     if end else None)
            reqs.append({"start": start, "end": end, "aic": r.get("copilotCredits"),
                         "in": int(r.get("promptTokens") or 0),
                         "out": int(r.get("completionTokens") or 0),
                         "model": str(r.get("modelId") or "").split("/")[-1] or "auto",
                         "text": ((r.get("message") or {}).get("text") or "").strip()})
        if reqs:
            chats.append({"id": state.get("sessionId") or f.stem, "path": str(f),
                          "title": state.get("customTitle") or "",
                          "requests": reqs, "edits": [_rel(e, root) for e in edits]})
    return chats


def vscode_entry(chat: dict, lo, hi, what: str, keep=lambda r: True) -> dict | None:
    lo, hi = parse(lo), parse(hi)
    reqs = [r for r in chat["requests"] if _within(r["end"], lo, hi) and keep(r)]
    priced = [r for r in reqs if r["aic"] is not None]
    if not priced:
        return None
    models: dict[str, int] = {}
    for r in priced:
        models[r["model"]] = models.get(r["model"], 0) + r["in"] + r["out"]
    unpriced = len(reqs) - len(priced)
    return entry(VSCODE, chat["id"], what,
                 (min(r["start"] or r["end"] for r in priced), max(r["end"] for r in priced)),
                 sum(r["in"] + r["out"] for r in priced), models,
                 aic=sum(r["aic"] for r in priced), calls=len(priced),
                 model_seconds=_intervals_union([(r["start"], r["end"]) for r in priced]),
                 note=(f"{unpriced} turn(s) not priced yet" if unpriced else None))


def _is_implementation(r: dict) -> bool:
    return not r["text"].lstrip().startswith(NOT_IMPLEMENTATION)


# ----------------------------------------------------------------------------- components

def measure_implementation(root: Path, base: str, lo, hi,
                           harnesses=(CLAUDE, COPILOT_CLI, VSCODE),
                           vouched: list[str] | tuple = ()) -> dict:
    """Writing the code: every harness that edited a file of the change set between the
    fork and `hi` (prepare, or the implementation commit). Edit evidence is required in
    each store — a session that only read the files, or ran git beside them, wrote nothing.

    A Claude session the branch itself names — `state.json`'s `session`, a
    `Claude-Session:` trailer — is the implementing session, and is billed from the fork to
    `hi` whole: reading the spec before the first edit and running the tests after the last
    are writing the code too. Eval run 8's first edit came 2.5 minutes and $1.09 into the
    session, and prepare 19 minutes after its last edit."""
    lo, hi = parse(lo), parse(hi)
    files = changed_files(root, base)
    out: list[dict] = []
    searched = []
    if CLAUDE in harnesses:
        searched.append("Claude transcripts")
        named = [s for s in dict.fromkeys(vouched or ()) if s]
        for sid in named:
            out.append(claude_entry(sid, lo, hi, "the implementing session, fork → prepare"))
        for s in claude_authors(root, base):
            if s["session"] in named:
                continue
            # Its first edit to its last, never past `hi`: the same conversation goes on to
            # take the review's advice, and those edits are the auto-fix row's.
            a = max([x for x in (parse(s.get("first")), lo) if x], default=None)
            b = min([x for x in (parse(s.get("last")), hi) if x], default=None)
            if a and b and b < a:
                continue
            out.append(claude_entry(s["session"], a, b, "edited the change set"))
    if COPILOT_CLI in harnesses:
        searched.append("the Copilot CLI session store")
        for s in copilot_sessions(root):
            if s["kind"] != "other" or not copilot_wrote(s["id"], files, root):
                continue
            out.append(copilot_entry(s["id"], lo, hi, "edited the change set"))
    if VSCODE in harnesses:
        searched.append("VS Code chat logs")
        for chat in vscode_chats(root):
            if not files & set(chat["edits"]):
                continue
            out.append(vscode_entry(chat, lo, hi, chat["title"] or "edited the change set",
                                    keep=_is_implementation))
    why = (f"no session in {', '.join(searched)} edited these files between the fork "
           f"({iso(lo) or 'undated'}) and {iso(hi) or 'an undated implementation commit'}")
    if CLAUDE in harnesses and vouched:
        why += "; " + _missing_claude(vouched, "implementation")
    return component("implementation", [e for e in out if e], window=(lo, hi), reason=why)


def measure_review_fixes(root: Path, harness: str, sessions: list[str], t_prep, t_done,
                         t_finish, branch: str | None = None,
                         source: str = "recorded") -> tuple[dict, dict]:
    """Finding (`t_prep` → `t_done`) and fixing (`t_done` → `t_finish`), in the harness
    that ran `/record-review`, from its sessions only."""
    t_prep, t_done, t_finish = parse(t_prep), parse(t_done), parse(t_finish)
    if harness == COPILOT_CLI and not sessions:
        cands = [s for s in copilot_sessions(root, branch)
                 if s["kind"] == "record-review" or (
                     s["kind"] == "other" and s["first"] and s["last"]
                     and t_prep and s["last"] >= t_prep
                     and (t_finish is None or s["first"] <= t_finish))]
        sessions = [s["id"] for s in cands]
    if harness == COPILOT_CLI and t_done is None:
        t_done = copilot_last_subagent_call(sessions, t_prep, t_finish)
    if harness == CLAUDE and t_done is None and sessions:
        path = rc().transcript(sessions[0])
        if path is not None:
            agents = [a for a in rc().subagent_transcripts(path)
                      if _within(rc().agent_span([a])[0], t_prep, t_finish)]
            t_done = rc().agent_span(agents)[1] if agents else None

    def pick(lo, hi, what):
        if harness == CLAUDE:
            return [claude_entry(s, lo, hi, what) for s in sessions]
        if harness == COPILOT_CLI:
            return [copilot_entry(s, lo, hi, what) for s in sessions]
        if harness == VSCODE:
            return [vscode_entry(c, lo, hi, what) for c in vscode_chats(root)
                    if not sessions or c["id"] in sessions]
        return []

    who = HARNESS_LABELS.get(harness, harness or "an unnamed harness")
    if harness not in HARNESS_LABELS:
        why = (f"the review was recorded by {who}, whose usage this toolkit cannot read"
               if harness else "the review record does not say which harness ran it")
        return (component("review", [], why, (t_prep, t_done), source),
                component("autofix", [], why, (t_done, t_finish), source))
    if t_done is None:
        review = component("review", [e for e in pick(t_prep, t_finish, "review and its fixes")
                                      if e], window=(t_prep, t_finish), source=source,
                           reason=(_missing_claude(sessions, "review") if harness == CLAUDE
                                   else f"no {who} session found for the review"))
        fixes = component("autofix", [], (
            "the reviewers' end was not stamped (`RR ci` after the reviewers stamps it) and "
            "no reviewer subagent dates it — the fixes are inside the review row"),
            (None, t_finish), source)
        return review, fixes
    if harness == VSCODE:
        review = component("review", [e for e in pick(t_prep, t_finish, "review and its fixes")
                                      if e], window=(t_prep, t_finish), source=source,
                           reason="VS Code writes a chat turn's credits only when the turn "
                                  "ends — not yet, when `finish` ran")
        fixes = component("autofix", [], (
            "VS Code prices a whole user turn, and the review and its fixes ran in one — "
            "both are in the review row"), (t_done, t_finish), source)
        return review, fixes
    review = component("review", [e for e in pick(t_prep, t_done, "the reviewers and their brief")
                                  if e], window=(t_prep, t_done), source=source,
                       reason=(_missing_claude(sessions, "review") if harness == CLAUDE
                               else f"no {who} turn between prepare and the reviewers' end"))
    # Half-open: the reviewers' last call is the review's, not the fixes' too.
    after = t_done + dt.timedelta(microseconds=1)
    fixes = component("autofix", [e for e in pick(after, t_finish, "deciding and fixing")
                                  if e], window=(t_done, t_finish), source=source,
                      reason=(_missing_claude(sessions, "auto-fixes") if harness == CLAUDE
                              else f"no {who} turn between the reviewers' end and finish"))
    return review, fixes


def record(root: Path, base: str, state: dict, harness: str, at=None) -> dict:
    """What `record-review.py finish` commits as `review-cost.json`: the three components
    it could see, measured now, in the harness that ran it."""
    at = parse(at) or now()
    t_prep = parse(state.get("reviewStartedAt"))
    impl_hi = t_prep or committed_at(root, state.get("implementation"))
    vouched = [s for s in [state.get("session"), *claimed_sessions(root, base)] if s]
    impl = measure_implementation(root, base, fork_time(root, base), impl_hi,
                                  vouched=vouched)
    sessions = [s for s in state.get("sessions") or [] if s]
    if not sessions and state.get("session"):
        sessions = [state["session"]]
    if harness == COPILOT_CLI and os.environ.get("COPILOT_AGENT_SESSION_ID"):
        sessions = sorted(set(sessions) | {os.environ["COPILOT_AGENT_SESSION_ID"]})
    if t_prep is None:
        why = ("prepare stamped no start for the review (a state.json from before "
               "reviewStartedAt existed, or none) — its window cannot be drawn")
        review, fixes = component("review", [], why), component("autofix", [], why)
    else:
        review, fixes = measure_review_fixes(root, harness, sessions, t_prep,
                                             state.get("reviewersDoneAt"), at,
                                             git(root, "rev-parse", "--abbrev-ref", "HEAD"))
    return {"schema": RECORD_SCHEMA, "harness": harness, "recordedAt": iso(at),
            "base": state.get("base"), "implementation": state.get("implementation"),
            "rounds": list(state.get("finishes") or []) + [iso(at)],
            "aicUsd": AIC_USD,
            "components": [impl, review, fixes]}


def last_round_at(root: Path, state: dict) -> "dt.datetime | None":
    """The end of the last CI round: the latest of the last `ci` stamp, the last
    `[auto-fix]` commit on the branch, and — in Claude Code — the end of the turn that ran
    that `ci`. `lastCiAt` is stamped when `ci` starts; the wait for CI and the turns that
    read its verdict come after it (eval run 8: $0.48 of five turns, 02:54 → 03:01)."""
    last_ci = parse(state.get("lastCiAt"))
    stamps = [last_ci]
    out = git(root, "log", "--format=%cI", "--grep=^\\[auto-fix\\]", "-1", "HEAD")
    stamps.append(parse(out.strip()) if out.strip() else None)
    if last_ci and normalize_harness(state.get("harness")) in (CLAUDE, ""):
        for sid in dict.fromkeys([*(state.get("sessions") or []), state.get("session")]):
            stamps.append(claude_turn_bounds(sid, last_ci)[1] if sid else None)
    stamps = [t for t in stamps if t]
    return max(stamps) if stamps else None


def extend_to_last_round(root: Path, base: str, rec: dict) -> dict:
    """The committed record, its auto-fix window extended past `recordedAt` when a CI round
    ran after it. `finish` writes the record; a round fixed and committed without another
    `finish` left 45 AIC of hr-try-4 outside it (207.7 recorded, 252.6 spent)."""
    try:
        state = json.loads((Path(root) / ".human-review" / "review" / "state.json")
                           .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return rec
    end, recorded = last_round_at(root, state), parse(rec.get("recordedAt"))
    if not end or not recorded or end <= recorded:
        return rec
    again = record(root, state.get("base") or base,
                   {**state, "finishes": rec.get("rounds") or []},
                   rec.get("harness") or "", at=end)
    for c in again["components"]:
        if c["key"] == "autofix":
            c["source"] = "recorded, extended to the last CI round at build"
    return {**rec, "components": [rec["components"][0], *again["components"][1:]],
            "extendedTo": iso(end)}


def read_record(root: Path) -> dict | None:
    try:
        doc = json.loads((Path(root) / RECORD_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == RECORD_SCHEMA else None


def _front(root: Path) -> dict:
    try:
        text = (Path(root) / "review-points.md").read_text(encoding="utf-8")
    except OSError:
        return {}
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    return dict(re.findall(r"^([A-Za-z-]+):\s*(.*)$", m.group(1), re.M)) if m else {}


#: The families a `reviewers:` line in the front matter may name in prose.
FAMILIES = ("Opus", "Sonnet", "Haiku", "Fable", "Mythos")


def _session_model_ids(session: str) -> list[str]:
    """Every Claude model id the session and the agents it forked answered with."""
    path = rc().transcript(session) if session else None
    if path is None:
        return []
    ids = transcript_model_ids(path)
    for agent in rc().subagent_transcripts(path):
        ids += [m for m in transcript_model_ids(Path(agent)) if m not in ids]
    return ids


def relabel(row: dict) -> dict:
    """A recorded row's model names, as `review-cost.py:label` spells them now.

    A `review-cost.json` recorded before `label` read the version names
    `claude-opus-5-5` `Opus 5`. The transcript is still on disk on the machine that
    recorded it, and says which id that name stood for: when exactly one id of the session
    carries the old name, the row takes the new one. Anything ambiguous, or a transcript
    that is gone, keeps the name it was recorded with."""
    for e in row.get("entries") or []:
        models = e.get("models") or {}
        if e.get("harness") != CLAUDE or not models or not e.get("session"):
            continue
        upgrade: dict[str, set] = {}
        for raw in _session_model_ids(e["session"]):
            upgrade.setdefault(rc().legacy_label(raw), set()).add(rc().label(raw))
        renamed: dict[str, int] = {}
        for name, tok in models.items():
            names = upgrade.get(name) or set()
            key = next(iter(names)) if len(names) == 1 else name
            renamed[key] = renamed.get(key, 0) + tok
        e["models"] = renamed
    return row


def reviewer_models(root: Path, review: Path | None = None) -> list[str]:
    """The model(s) that did the reviewing — the reviewers, not the session briefing them.

    The review chip used to name the first model of the run's whole bill, which is the
    implementation's: `Reviewed by Opus 5` over four Sonnet reviewers. In order:

      1. `subagentModels` on the recorded review row — the agents forked in its window;
      2. for a record that predates that field, the same answer read off the session's
         subagent transcripts that started inside the review window (`state.json`);
      3. the families the front matter's `reviewers:` line names in prose, versioned from
         the review row's own models where one matches;
      4. the review row's models, largest first — an inline review, with no agents to
         name, was done by the session itself.
    Empty when nothing says."""
    root = Path(root)
    review = Path(review) if review else root / ".human-review"
    rec = read_record(root) or {}
    row = next((c for c in rec.get("components") or [] if c.get("key") == "review"), {})
    names: list[str] = []

    def add(found) -> None:
        names.extend(m for m in found if m and m not in names)

    for e in row.get("entries") or []:
        add(e.get("subagentModels") or [])
    if names:
        return names
    try:
        state = json.loads((review / "review" / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    # Never open-ended: an agent the page build forked hours later is not a reviewer.
    lo = parse(state.get("reviewStartedAt"))
    hi = parse(state.get("reviewersDoneAt")) or parse((state.get("finishes") or [None])[0])
    harness = normalize_harness(state.get("harness") or rec.get("harness") or CLAUDE)
    if lo and hi and harness == CLAUDE:
        for sid in state.get("sessions") or [state.get("session")]:
            path = rc().transcript(sid) if sid else None
            if path is None:
                continue
            for agent in rc().subagent_transcripts(path):
                first, _last = rc().agent_span([agent])
                if _within(first, lo, hi):
                    add(transcript_models(Path(agent)))
        if names:
            return names
    recorded = {}
    for e in row.get("entries") or []:
        for name, tok in (e.get("models") or {}).items():
            recorded[name] = recorded.get(name, 0) + tok
    prose = _front(root).get("reviewers") or ""
    for fam in FAMILIES:
        if re.search(rf"\b{fam}\b", prose, re.I):
            add([n for n in recorded if n.lower().startswith(fam.lower())] or [fam])
    if names:
        return names
    add(sorted(recorded, key=lambda n: -recorded[n]))
    return names


_COUNTED_AGENTS = re.compile(r"\b(\d+)\b[^.;+]*?\b(?:sub-?agents?|reviewers?|agents?)\b", re.I)


def _review_agent_count(root: Path, review: Path, row: dict) -> int:
    """How many agents did the reviewing: the recorded count, else the session's subagent
    transcripts that started inside the review window, else the number the front matter's
    `reviewers:` line gives in prose ("4 read-only Sonnet subagents"). 0 when nothing says."""
    n = sum(int(e.get("subagents") or 0) for e in row.get("entries") or [])
    if n:
        return n
    try:
        state = json.loads((review / "review" / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    lo = parse(state.get("reviewStartedAt"))
    hi = parse(state.get("reviewersDoneAt")) or parse((state.get("finishes") or [None])[0])
    harness = normalize_harness(state.get("harness") or CLAUDE)
    if lo and hi and harness == CLAUDE:
        for sid in state.get("sessions") or [state.get("session")]:
            path = rc().transcript(sid) if sid else None
            for agent in (rc().subagent_transcripts(path) if path else []):
                first, _last = rc().agent_span([agent])
                if _within(first, lo, hi):
                    n += 1
        if n:
            return n
    hit = _COUNTED_AGENTS.search(_front(root).get("reviewers") or "")
    return int(hit.group(1)) if hit else 0


def review_line(root: Path, review: Path | None = None) -> str:
    """Who reviewed, said the way the run was shaped — for the review chip's hover.

    Eval run 6's chip said `Reviewed by Sonnet 5.5` while its own cost row for the review
    read `Opus 5.5 73% / Sonnet 5.5 27%`: four Sonnet subagents read the diff, and an Opus
    session briefed them and merged what they found. Both halves are true and the chip kept
    one. With agents: `Reviewers: Sonnet 5.5 (4 subagents), orchestrated by Opus 5.5`.
    Without (an inline review, or nothing recorded but a name): `Reviewed by <names>`.
    Empty when nothing says who reviewed."""
    root = Path(root)
    review = Path(review) if review else root / ".human-review"
    names = reviewer_models(root, review)
    if not names:
        return ""
    rec = read_record(root) or {}
    row = next((c for c in rec.get("components") or [] if c.get("key") == "review"), {})
    n = _review_agent_count(root, review, row)
    if not n and not any(e.get("subagentModels") for e in row.get("entries") or []):
        return "Reviewed by " + ", ".join(names)
    spent: dict = {}
    for e in row.get("entries") or []:
        for name, tok in (e.get("models") or {}).items():
            spent[name] = spent.get(name, 0) + tok
    orchestrators = [m for m in sorted(spent, key=lambda m: -spent[m]) if m not in names]
    who = ", ".join(names) + (f" ({n} subagent{'s' if n != 1 else ''})" if n else "")
    return (f"Reviewers: {who}"
            + (f", orchestrated by {', '.join(orchestrators)}" if orchestrators else ""))


def _from_phase(row: dict | None, key: str, session: str | None, what: str) -> dict | None:
    if not row or not row.get("measured"):
        return None
    return entry(CLAUDE, session or "", what, tuple(row.get("window") or (None, None)),
                 row.get("tokens") or 0, row.get("models"), usd=row.get("cost") or 0.0,
                 calls=row.get("messages") or 0)


def derive(root: Path, base: str, phases: dict | None, commits: dict | None = None) -> dict:
    """1–3 for a branch recorded before `review-cost.json` existed — a scan, said so.

    A Claude-recorded branch whose `state.json` stamped prepare is measured from the
    transcripts over the same windows `finish` uses (`record`). Without that stamp it keeps
    its phase cut (`session-cost.py`, trailers and reviewer transcripts), other harnesses'
    implementation sessions added beside it, and every row it cannot fill says which file
    was missing and what to run. A Copilot
    one is read from the session store: the `/record-review` sessions after the
    implementation commit, split at their last subagent call."""
    commits = commits or {}
    front = _front(root)
    state = {}
    try:
        state = json.loads((Path(root) / ".human-review/review/state.json").read_text())
    except (OSError, ValueError):
        pass
    harness = normalize_harness(front.get("harness") or state.get("harness"))
    impl_sha = commits.get("implementation") or front.get("implementation")
    t_impl = committed_at(root, impl_sha)
    review_sha = commits.get("review") or state.get("reviewCommit")
    t_review = committed_at(root, review_sha)
    rows = {r.get("key"): r for r in (phases or {}).get("rows") or [] if isinstance(r, dict)}
    claude_branch = harness == CLAUDE or (not harness and rows.get("implementation", {})
                                          .get("measured"))
    # Why there is no record to read, said on every derived row: run 8's finish refused
    # its own review-cost.json (a key the schema did not know), and the page blamed the
    # branch for predating the record.
    because = ("record-review.py finish did not write review-cost.json"
               + (f" ({state['costError']})" if state.get("costError") else ""))
    if claude_branch and state.get("reviewStartedAt"):
        # The review's own stamps are on disk: measure the four windows from the
        # transcripts exactly as `finish` would have, rather than ask `session-cost.py`
        # for a phase cut it only makes when someone ran it.
        sessions = [s for s in dict.fromkeys([*(state.get("sessions") or []),
                                              state.get("session"), commits.get("session")])
                    if s]
        at = (last_round_at(root, state) or parse((state.get("finishes") or [None])[-1])
              or t_review or now())
        doc = record(root, state.get("base") or base, {**state, "sessions": sessions},
                     CLAUDE, at=at)
        for c in doc["components"]:
            c["source"] = "derived"
            c["derivedBecause"] = because
        return {"schema": RECORD_SCHEMA, "harness": CLAUDE, "derived": True,
                "components": doc["components"]}
    if claude_branch:
        sid = (phases or {}).get("session")
        impl = measure_implementation(root, base, fork_time(root, base), t_impl,
                                      harnesses=(COPILOT_CLI, VSCODE))
        impl_entries = [_from_phase(rows.get("implementation"), "implementation", sid,
                                    "first edit → commit #1")] + impl["entries"]
        fix_rows = [r for r in (rows.get("post_review_fixes"), rows.get("review_points"))
                    if r and r.get("measured")]
        fixes = None
        if fix_rows:
            fixes = entry(CLAUDE, sid or "", "last reviewer turn → the review commit",
                          tuple(fix_rows[0].get("window") or (None, None)),
                          sum(r.get("tokens") or 0 for r in fix_rows),
                          rc()._merge_models([r.get("models") or {} for r in fix_rows]),
                          usd=sum(r.get("cost") or 0 for r in fix_rows),
                          calls=sum(r.get("messages") or 0 for r in fix_rows))
        missing = (because + ", and " + (
            "there is no .human-review/review/state.json" if not state else
            "state.json has no reviewStartedAt (prepare predates the stamp)")
            + " — so the windows come from session-cost.py's phase cut, and ")

        def why(key):
            row = rows.get(key) or {}
            return missing + (row.get("reason")
                              or (phases or {}).get("reason")
                              or "phases.json has no dated row for it — run session-cost.py")
        comps = [component("implementation", [e for e in impl_entries if e],
                           why("implementation"), source="derived"),
                 component("review", [e for e in [_from_phase(rows.get("code_review"),
                                                              "review", sid,
                                                              "forked reviewers")] if e],
                           why("code_review"), source="derived"),
                 component("autofix", [fixes] if fixes else [], why("post_review_fixes"),
                           source="derived")]
        for c in comps:
            c["derivedBecause"] = because
        return {"schema": RECORD_SCHEMA, "harness": CLAUDE, "derived": True,
                "components": comps}

    impl = measure_implementation(root, base, fork_time(root, base), t_impl)
    impl["source"] = "derived"
    if harness == COPILOT_CLI:
        branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
        sess = [s for s in copilot_sessions(root, branch) if s["kind"] == "record-review"
                and s["last"] and (t_impl is None or s["last"] >= t_impl)
                and (t_review is None or s["first"] <= t_review + dt.timedelta(minutes=10))]
        t_prep = sess[0]["first"] if sess else None
        t_end = max(s["last"] for s in sess) if sess else None
        review, fixes = measure_review_fixes(root, COPILOT_CLI, [s["id"] for s in sess],
                                             t_prep, None, t_end, branch, source="derived")
        if not sess:
            why = "no Copilot CLI session on this branch names /record-review"
            review = component("review", [], why, source="derived")
            fixes = component("autofix", [], why, source="derived")
    elif harness == VSCODE:
        chats = vscode_chats(root)
        hits = [(c, r) for c in chats for r in c["requests"]
                if r["text"].lstrip().startswith("/record-review")
                and _within(r["end"], t_impl, (t_review or now()) + dt.timedelta(minutes=10))]
        entries = [vscode_entry(c, r["start"], (t_review or r["end"]), "review and its fixes")
                   for c, r in hits[:1]]
        review = component("review", [e for e in entries if e],
                           "no VS Code chat turn starts /record-review on this branch",
                           source="derived")
        fixes = component("autofix", [], "VS Code prices a whole user turn, and the review "
                          "and its fixes ran in one — both are in the review row",
                          source="derived")
    else:
        why = ("no review-cost.json, and the review record names no harness this toolkit "
               "can read" if harness else "no review-cost.json and no review-points.md "
               "harness — nothing says which harness reviewed this branch")
        review = component("review", [], why, source="derived")
        fixes = component("autofix", [], why, source="derived")
    return {"schema": RECORD_SCHEMA, "harness": harness, "derived": True,
            "components": [impl, review, fixes]}


# ----------------------------------------------------------------------------- 4: the guide

#: The paid model steps a run shells out to: their ledger, what they write, the program.
#: `program` travels on the entry so `review-cost.py` can bill each to the tab it feeds
#: (`MODEL_RUN_TABS`) — eval run 6 printed the mapping under "this guide" and then
#: "Tests: no model spend" two rows further down.
MODEL_RUN_LEDGERS = (
    (".model-runs.json", "requirements↔tests mapping", "rerun-model.py"),
    (".film-runs.json", "film script", "rerun-film.py"),
)


def _run_models(r: dict) -> tuple[int, dict]:
    """A ledger run's tokens and `{printed model name: tokens}`. Runs recorded before
    `rerun-model.py` kept the CLI's token counts have none, and say 0 under the model the
    step was asked for — a row with a price and no count, rather than an invented count."""
    tokens = int(r.get("tokens") or 0)
    models: dict = {}
    for mid, n in (r.get("models") or {}).items() if isinstance(r.get("models"), dict) else []:
        name = rc().label(str(mid)) if str(mid).startswith("claude-") else str(mid)
        models[name] = models.get(name, 0) + int(n or 0)
    if not models:
        models = {str(r.get("model") or "?"): tokens}
    return tokens, models


def _ledger_runs(review: Path, lo, hi) -> list[dict]:
    out = []
    for name, what, program in MODEL_RUN_LEDGERS:
        try:
            doc = json.loads((review / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        runs = doc.get("runs") if isinstance(doc, dict) else doc
        for r in runs if isinstance(runs, list) else []:
            if not isinstance(r, dict) or not isinstance(r.get("cost"), (int, float)):
                continue
            when = parse(r.get("when"))
            if not _within(when, lo, hi):
                continue
            secs = float(r.get("seconds") or 0)
            tokens, models = _run_models(r)
            e = entry(CLAUDE, f"claude -p ({name})", f"{what} ({program})",
                      (when - dt.timedelta(seconds=secs) if when else None, when),
                      tokens, models, usd=float(r["cost"]), calls=1, model_seconds=secs)
            e["program"] = program
            # `entry` drops a zero count, and a run with no recorded tokens still has a
            # model: keep its name so the row can say what ran.
            e["models"] = e["models"] or {k: 0 for k in models}
            out.append(e)
    return out


def _raw_steps(review: Path) -> list[dict]:
    try:
        steps = json.loads((review / ".steps.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []


def run_end(review: Path, started) -> "dt.datetime | None":
    """When a run that recorded no end of its own ended, derived after the fact: its
    first `guide` step after `.started` closed — Step 5's own `end`. Not the ledger's
    last stamp: a page rebuilt for a week keeps stamping, and test-pr's read 9,228 min."""
    started = parse(started)
    steps = [s for s in _raw_steps(review) if parse(s.get("end"))
             and (started is None or parse(s.get("start")) and parse(s["start"]) >= started)]
    guide = [parse(s["end"]) for s in steps if "guide" in (s.get("tabs") or [])]
    if guide:
        return min(guide)
    return max([parse(s["end"]) for s in steps], default=None)


def _steps(review: Path, lo, hi) -> list[dict]:
    """The step ledger's producers that ran inside the run, each with its seconds."""
    out = []
    for s in _raw_steps(review):
        a, b = parse(s.get("start")), parse(s.get("end"))
        if a is None or b is None or not _within(a, lo, hi):
            continue
        out.append({"label": s.get("label") or ",".join(s.get("tabs") or []),
                    "tabs": s.get("tabs") or [], "seconds": round((b - a).total_seconds())})
    return out


def run_session_harness(review: Path) -> tuple[str, str | None]:
    """Which harness ran this page, and its Claude session when it was Claude: a pinned
    non-blank `.session` is Claude; blank is pinned too and means another harness."""
    try:
        sid = (review / ".session").read_text(encoding="utf-8").strip()
        return (CLAUDE, sid) if sid else ("", None)
    except OSError:
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
        return (CLAUDE, sid) if sid else ("", None)


def measure_guide(root: Path, review: Path, harness: str | None = None, end=None,
                  source: str = "recorded") -> tuple[dict, dict]:
    """The page's own model work from `.started` to `end`, and the run's wall-clock."""
    try:
        started = parse((review / ".started").read_text(encoding="utf-8").strip())
    except OSError:
        started = None
    if end is None:
        end = run_end(review, started) or now()
    end = parse(end)
    guessed, sid = run_session_harness(review)
    harness = normalize_harness(harness) or guessed
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    entries: list[dict] = []
    if harness == CLAUDE or (not harness and sid):
        # From the prompt that started the run, not from `.started`: Step 2 writes that
        # marker after the skill was read and the change set resolved, and those turns
        # are the run's too.
        opened = claude_turn_bounds(sid, started)[0] if started else None
        lo = min([t for t in (opened, started) if t], default=None)
        entries.append(claude_entry(sid, lo, end, "the /human-review run"))
        harness = CLAUDE
    if harness in (COPILOT_CLI, "") and started:
        for s in copilot_sessions(root, branch):
            if s["kind"] != "human-review" or not s["first"] or not s["last"]:
                continue
            if s["first"] <= end and s["last"] >= started:
                entries.append(copilot_entry(s["id"], None, None, "the /human-review run"))
                harness = COPILOT_CLI
    if harness in (VSCODE, "") and started and not [e for e in entries if e]:
        for c in vscode_chats(root):
            hit = [r for r in c["requests"] if r["text"].lstrip().startswith("/human-review")
                   and _within(r["end"], started, end + dt.timedelta(hours=1))]
            if hit:
                entries.append(vscode_entry(c, hit[0]["start"], end + dt.timedelta(hours=1),
                                            "the /human-review run"))
                harness = VSCODE
    entries = [e for e in entries if e] + _ledger_runs(review, started, end)
    comp_reason = ("no .started marker — the run that built this page did not open a window"
              if not started else
              "no Claude session pinned, and no Copilot CLI or VS Code session on this "
              "branch ran /human-review while the page was being built")
    comp = component("guide", entries, comp_reason, (started, end), source)
    return comp, wallclock(review, started, end, comp["modelSeconds"])


def wallclock(review: Path, started=None, end=None, model_seconds: float | None = None) -> dict:
    """How long the run took: `.started` to its end, the step ledger's producers inside
    it, and — when the money was measured — how much of it the model was working."""
    if started is None:
        try:
            started = parse((review / ".started").read_text(encoding="utf-8").strip())
        except OSError:
            started = None
    started = parse(started)
    if end is None:
        end = run_end(review, started)
    end = parse(end)
    steps = _steps(review, started, end)
    return {"started": iso(started), "ended": iso(end),
            "seconds": round((end - started).total_seconds()) if started and end else None,
            "modelSeconds": model_seconds, "steps": steps,
            "stepSeconds": sum(s["seconds"] for s in steps)}


def record_run(root: Path, review: Path, harness: str | None = None, force: bool = False,
               at=None) -> dict:
    """`.human-review/report-cost.json`: written once per run, at its end. A second call
    for the same `.started` keeps the first measurement unless `force` — the money is the
    run's, and a later caller would only re-bill it."""
    path = review / REPORT_FILE
    old = read_report(review) or {}
    try:
        started = (review / ".started").read_text(encoding="utf-8").strip()
    except OSError:
        started = None
    if old.get("guide") and old.get("started") == iso(started) and not force:
        return old
    comp, wall = measure_guide(root, review, harness, end=at or now())
    doc = {"schema": REPORT_SCHEMA, "started": iso(started), "recordedAt": iso(at or now()),
           "harness": comp["harnesses"][0] if comp["harnesses"] else normalize_harness(harness),
           "guide": comp, "wallclock": wall,
           "refreshes": old.get("refreshes", []) if old.get("started") == iso(started) else []}
    _write_json(path, doc)
    return doc


def complete_guide(root: Path, review: Path, report: dict) -> tuple[dict, dict | None]:
    """The recorded guide row, completed with what its Copilot run did after recording.

    `report-cost.py` runs in Step 5, before the build and the close, so a Copilot CLI
    session keeps calling the model after the snapshot: hr-try-4's guide recorded 198.3
    of the 296.7 AIC its session spent (window stopped 20:40:22, session ran to 20:43:42).
    A Copilot CLI /human-review session is the run and nothing else, so its later events
    are this page's; a Claude session is not extended, because the conversation that ran
    the page goes on to other work and its later turns are not this report's."""
    guide, wall = report["guide"], report.get("wallclock")
    if CLAUDE in (guide.get("harnesses") or [report.get("harness")]):
        return _complete_claude_guide(root, review, report)
    if COPILOT_CLI not in (guide.get("harnesses") or [report.get("harness")]):
        return guide, wall
    recorded = parse(report.get("recordedAt"))
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    lasts = [s["last"] for s in copilot_sessions(root, branch)
             if s["kind"] == "human-review" and s["last"]
             and any(e.get("session") == s["id"] for e in guide.get("entries") or [])]
    end = max(lasts, default=None)
    if not end or not recorded or end <= recorded:
        return guide, wall
    again, wall2 = measure_guide(root, review, COPILOT_CLI, end=end)
    if (again.get("aic") or 0) <= (guide.get("aic") or 0):
        return guide, wall
    again["source"] = "recorded, completed at build to the session's last call"
    return again, wall2


def _complete_claude_guide(root: Path, review: Path, report: dict) -> tuple[dict, dict | None]:
    """A Claude guide row recorded in Step 5, carried to the end of the turn that ran
    `/human-review` — the build and the close come after the snapshot. Only that turn:
    the next prompt in the same conversation is other work, and not this page's.
    Eval run 8 recorded $0.81 of the session's $1.35 this way round."""
    guide, wall = report["guide"], report.get("wallclock")
    recorded = parse(report.get("recordedAt"))
    sid = run_session_harness(review)[1]
    started = parse(report.get("started"))
    if not sid or not started or not any(e.get("session") == sid
                                         for e in guide.get("entries") or []):
        return guide, wall
    end = claude_turn_bounds(sid, started)[1]
    if not recorded:
        return guide, wall
    later = bool(end and end > recorded)
    again, wall2 = measure_guide(root, review, CLAUDE, end=end if later else recorded)
    # Re-read while the transcript is on disk, even over the recorded window: the record
    # keeps the window, the store keeps the tokens, and a price corrected since (Opus 5.5
    # at $4/$20, not the family's $5/$25) must not stay frozen in the report.
    if not any(e.get("session") == sid for e in again.get("entries") or []):
        return guide, wall
    again["source"] = ("recorded, completed at build to the end of the /human-review turn"
                       if later else "recorded, re-priced at build from the transcript")
    return again, (wall2 if later else wall)


def note_refresh(review: Path, seconds: float, steps: str = "none") -> None:
    """A refresh's own time, added to the run's record. Never its money: a refresh calls
    no model, and the run it refreshes has already been billed."""
    path = Path(review) / REPORT_FILE
    doc = read_report(Path(review)) or {"schema": REPORT_SCHEMA, "refreshes": []}
    doc.setdefault("refreshes", []).append({"at": iso(now()), "seconds": round(seconds, 1),
                                            "steps": steps})
    doc["refreshes"] = doc["refreshes"][-50:]
    _write_json(path, doc)


def read_report(review: Path) -> dict | None:
    try:
        doc = json.loads((Path(review) / REPORT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == REPORT_SCHEMA else None


def _write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _guide_from_phases(phases: dict | None) -> dict | None:
    rows = {r.get("key"): r for r in (phases or {}).get("rows") or [] if isinstance(r, dict)}
    sid = (phases or {}).get("run_session")
    picked = [r for k in ("video", "images", "page_build") for r in [rows.get(k)]
              if r and r.get("measured")]
    if not picked:
        return None
    # A measured zero (the UX audit is a script) is an answer, but not a line worth a row.
    what = lambda r: (f'{r.get("label") or r["key"]} — the last full regeneration; the '
                      "run's own window held no turn")
    return component("guide", [_from_phase(r, r["key"], sid, what(r))
                               for r in picked if r.get("cost") or r.get("tokens")]
                     or [_from_phase(picked[0], "", sid, what(picked[0]))],
                     source="derived")


# ----------------------------------------------------------------------------- the page

def components(root: Path, base: str, review: Path, phases: dict | None = None,
               commits: dict | None = None) -> dict:
    """The four rows the `$` tab leads with, and how each was obtained."""
    rec = read_record(root)
    if rec:
        rec = extend_to_last_round(root, base, rec)
        first3 = rec["components"]
        # VS Code prices a turn only when it ends, which is after `finish` recorded it:
        # such a row is measured again now, over the same window, when it can be.
        for i, c in enumerate(first3):
            if not c.get("measured") and rec.get("harness") == VSCODE and c["key"] == "review":
                lo, hi = (c.get("window") or [None, None])[:2]
                first3[i] = component("review", [vscode_entry(ch, lo, (parse(hi) or now())
                                                              + dt.timedelta(hours=2),
                                                              "review and its fixes")
                                                 for ch in vscode_chats(root)],
                                      c.get("reason"), (lo, hi), "recorded, priced later")
    else:
        rec = derive(root, base, phases, commits)
        first3 = rec["components"]
    report = read_report(review)
    wall = None
    if report and report.get("guide"):
        guide, wall = complete_guide(root, review, report)
    else:
        # The run's own window, `.started` to its guide step, in whichever harness ran it;
        # the phase cut's last regeneration only when that window holds nothing.
        guide, wall = measure_guide(root, review, source="derived")
        if not guide["measured"] and (phases or {}).get("run_session"):
            guide = _guide_from_phases(phases) or guide
        guide["source"] = "derived"
    refreshes = (report or {}).get("refreshes") or []
    rows = [relabel(c) for c in list(first3) + [guide]]
    usd = sum(c.get("usd") or 0.0 for c in rows if c.get("measured"))
    aic = sum(c.get("aic") or 0.0 for c in rows if c.get("measured"))
    return {"rows": rows, "recorded": not rec.get("derived"),
            "reportRecorded": bool(report and report.get("guide")),
            "harness": rec.get("harness"), "usd": round(usd, 4), "aic": round(aic, 2),
            "usdEquivalent": round(usd + aic * AIC_USD, 2), "aicUsd": AIC_USD,
            "mixed": bool(usd and aic), "rateNote": AIC_RATE_NOTE,
            "unmeasured": [c["key"] for c in rows if not c.get("measured")],
            "wallclock": wall, "refreshes": refreshes,
            "refreshSeconds": round(sum(r.get("seconds") or 0 for r in refreshes))}
