# Plan Council — Design Document (V3 — web control plane)

> V1/V2 made the council a CLI conductor driven by a slash command, with a read-only
> dashboard bolted on the side. V3 inverts that: **the web UI is the control plane**, and
> every other entry point — the `council` CLI, the `/council` slash command, the main
> agent — becomes a client of the same local daemon.
>
> **Scope:** this document settles the *web layer*: topology, transport, process
> ownership, the event contract, and how the main agent starts and talks to the UI.
> Feature detail (screen-by-screen behaviour) is deliberately downstream of this.
>
> See [plan-council-design.md](plan-council-design.md) for the protocol itself
> (phases, anonymity, envelopes, budgets) — V3 does not change the debate; it changes
> who is holding the remote control.

---

## 0. The one decision everything else follows from

**The browser never touches a CLI. A local daemon owns every process, and the browser
commands the daemon over HTTP.**

```
┌───────────────────────────────────────────────────────────────────┐
│  Browser — React SPA (127.0.0.1)                                   │
│  new council · live debate · per-agent threads · controls          │
└───────────────┬───────────────────────────────────────────────────┘
                │  REST (commands)  +  SSE (live events)
┌───────────────▼───────────────────────────────────────────────────┐
│  the daemon — FastAPI + uvicorn, one asyncio loop                  │
│  session registry · orchestrator tasks · event log · control       │
└───────────────┬───────────────────────────────────────────────────┘
                │  asyncio.create_subprocess_exec  (unchanged adapters)
┌───────────────▼───────────────────────────────────────────────────┐
│  codex exec │ claude -p │ opencode run     (read-only, per-panelist)│
└───────────────────────────────────────────────────────────────────┘
                ▲
                │  the same REST API, via the `council` CLI
┌───────────────┴───────────────────────────────────────────────────┐
│  Main agent (Claude Code) — a peer client, not the owner           │
└───────────────────────────────────────────────────────────────────┘
```

### Why this answers "how does the web UI reach codex / claude / opencode?"

It doesn't — and it must not. Those CLIs are usable only because they run **as the
user, on the user's machine, with the user's environment**: `PATH` (and on Windows,
`PATHEXT` so `codex.cmd` resolves), `~/.codex/auth.json`, the Claude Code credential
store, opencode's provider config. A browser has none of that and never can.

The daemon is an ordinary local Python process started by the user, so it inherits all
of it for free. This is *exactly what `python -m council run` already does today* —
`council/adapters/` needs no conceptual change. The redesign moves the **owner** of
those subprocesses, not the mechanism.

Consequence, stated plainly: **there is no remote/hosted deployment.** The daemon binds
`127.0.0.1` and is single-user by construction. Anything else would mean shipping the
user's provider credentials somewhere else, which is not a feature this project wants.

---

## 1. Who owns a running council?

**Decision: the daemon runs the orchestrator in-process, as an `asyncio` task — not as
a spawned `council run` child.**

The orchestrator does no work of its own; it is an I/O supervisor that awaits
subprocesses. Keeping it inside the daemon's event loop means live state, `pause`,
`stop`, `drop panelist` and `inject message` are **function calls on an object**, not an
IPC protocol that has to be designed, versioned and debugged. The harnesses are still
separate OS processes, so a wedged panelist is still killable by tree (`_terminate_tree`
already handles both platforms).

**What this costs:** restarting the daemon kills every running council. Mitigation, not
elimination:

- `events.jsonl` is a write-ahead log, so a session's history survives intact;
- harness session ids are already persisted per turn, so a restarted daemon can offer
  **resume** — re-attach to each panelist's existing `codex`/`claude`/`opencode`
  conversation and continue from the next turn;
- what is genuinely lost is one in-flight turn. Acceptable: a turn is 1–3 minutes.

**Rejected:** detached child process + IPC. It buys durability we do not need at the
price of inventing a control channel; and a detached child that outlives its daemon is a
support problem (orphans still spending tokens), not a feature.

**Concurrency:** several sessions may run at once. A global semaphore caps concurrent
harness processes (`server.max_concurrent_calls`, default 6), with a per-session cap too,
so four parallel Phase-1 explorations across two sessions cannot fork-bomb the machine.

---

## 2. Daemon lifetime, and how the main agent starts the UI

**Decision: `council up` — idempotent, detached, discoverable through a state file.**

```
~/.council/
  daemon.json     {"app":"council","version":"0.2.0","pid":…,"port":8787,"token":"…","started_at":…}
  daemon.log
  registry.json   session id → project dir
  daemon.lock     start-race guard
```

`council up` does, in order:

1. read `daemon.json`; if a process answers `GET /api/health` on that port **and**
   identifies itself as `council` with a compatible version → reuse it, print the URL,
   exit 0. (Identity check matters: 8787 may belong to something else entirely.)
2. otherwise take `daemon.lock`, spawn the daemon **detached** — POSIX `setsid`;
   Windows `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` — with stdout/stderr to
   `daemon.log`;
3. poll `/api/health` for up to ~10 s, write `daemon.json`, print
   `http://127.0.0.1:8787/?token=…`;
4. `--open` also opens a browser.

**Why detached rather than Claude Code's `run_in_background: true`:** the UI must
outlive the agent turn *and* the Claude Code session. The user should be able to close
Claude Code, keep watching a 40-minute debate, and start the next council from the
browser. A background bash job cannot promise that.

`council down` stops it; `council up --foreground` runs it attached for development.

---

## 3. How the main agent talks to the council

**Decision: the main agent's only interface is the `council` CLI, which is a thin HTTP
client of the daemon.** No second implementation, no file-drop protocol, no MCP in V1.

```bash
council up [--open]                      # ensure daemon + browser; prints URL
council start --project-dir . --task task.md \
              --mode independent|review|hybrid \
              [--agents "gpt, claude opus"] [--seed plan.md] --json
                                         # -> {"id":"…","url":"…"} and returns immediately
council watch <id>                       # streams progress lines to stdout (SSE → text)
council status <id> --json               # one-shot snapshot; the polling path
council digest <id>                      # prints/locates digest.md
council models                           # unchanged
```

Exit codes are preserved (`0` ok, `2` too few panelists, `3` config error) so the
existing slash-command error handling keeps working.

**Why the CLI and not MCP:**

- **Harness-agnostic.** A codex or opencode main agent must be able to convene a council
  too. Shell commands are the only universal interface; MCP would bind the design to one
  harness.
- **Duration.** An MCP tool call is request/response. A 15–45 minute run does not fit;
  it fits `start` + `poll`, which is what the CLI gives.
- **Debuggable.** The user can run every one of these commands by hand in a terminal.

MCP stays available as a *later, additive veneer*: an MCP server that is itself a client
of this same HTTP API. It must never become a parallel implementation.

**Why not "the agent writes a file, the daemon watches the directory":** a filesystem is
a terrible command channel — no acknowledgement, no errors, no ordering, and a
half-written file is indistinguishable from a malformed one. Files stay what they are
today: *outputs*.

### The reverse direction: daemon → main agent

**Decision: pull, never push.** The daemon has no way to talk to a Claude Code session
and should not grow one.

- For agent-initiated sessions: the agent polls `council status <id> --json` (≥30 s
  interval, as today) or holds `council watch <id>` open, then reads the digest.
- For **browser-initiated** sessions: when the session ends, the UI shows a hand-off
  panel with a copy-paste command — a new slash command **`/council-apply <id>`** that
  reads that session's digest and does the synthesis step. This closes the loop without
  inventing agent-push.
- Optionally (off by default) the UI can run the synthesis itself, via one more
  `claude -p` call. Deliberately not the default: synthesis belongs in the Claude Code
  session where you are actually going to implement the plan.

---

## 4. Transport: REST for commands, SSE for live

**Decision: `POST`/`GET` JSON for every command; **Server-Sent Events** for every live
stream.** FastAPI ships `EventSourceResponse` natively, so this is a few lines, not a
library.

```
GET /api/sessions/{id}/events?from_seq=<seq>     # replay from seq, then follow live
```

**Why SSE and not WebSocket:** we need exactly one direction of streaming — the daemon
narrating. Commands are ordinary POSTs, which get real status codes and error bodies. In
exchange SSE gives, for free, the two things this UI actually needs: **automatic
reconnect** and **`Last-Event-ID` replay** — and our event log is already an append-only
sequence, so "replay from seq N" is a file seek, not a cache. A WebSocket would mean
hand-rolling both.

A second, daemon-level stream `GET /api/events` carries session-list changes so the UI's
sidebar updates when a run is started from the CLI.

---

## 5. Live output from the harnesses — the technical linchpin

The current dashboard can only show **finished** turns. That is not a UI limitation:
`run_process()` uses `proc.communicate()`, which returns nothing until the process exits.
"Who is writing right now" is impossible on top of that.

**Decision: `run_process()` becomes an incremental reader.** It gains an `on_line`
callback and consumes stdout line by line as the harness produces it. Each adapter gains
`parse_line(line) -> Delta | None` normalising the harness's own event stream into:

| Delta kind | Meaning |
|---|---|
| `text` | assistant prose, incremental |
| `tool` | the panelist read/searched a file (name + target) |
| `usage` | token counts as they are reported |
| `session` | the harness session id, as soon as it is known |

This invents no protocol, because **all three harnesses already emit line-delimited JSON
as they work** — we simply stopped buffering it:

| Harness | Flags | Note |
|---|---|---|
| `codex` | `exec --json` | already used; `thread.started` / `turn.completed` events, `-o` file still holds the authoritative final message |
| `opencode` | `run --format json` | already used; `part.type == "text"` / `step-finish` |
| `claude` | `-p --output-format stream-json --verbose --include-partial-messages` | **change** from `--output-format json`; verified against the installed CLI |

The buffered path stays as the fallback: if a stream turns out not to be JSONL (older
CLI, error page), the adapter parses the accumulated output exactly as it does today.

Everything the user asked to see — *who is writing now*, live text, and each panelist's
own file-exploration activity — falls out of this one change.

---

## 6. The event contract (what the UI is a fold over)

**Decision: the append-only log is the single source of truth. Daemon memory, HTTP
snapshots, SSE replay and post-hoc viewing are all `fold(events)` through one reducer.**
There is no second state representation to drift.

Per session, under `<project>/.council/<id>/`:

| File | Role |
|---|---|
| `events.jsonl` | **semantic** log — small, durable, the contract. Digest, tests and replay depend only on this. |
| `stream.jsonl` | **verbose** log — prompts, text and tool deltas. High volume, and what the per-panelist thread view is rebuilt from. |
| `status.json` | derived snapshot, kept for `council status` and backwards compatibility |
| `task.md`, `seed.md`, `plans/`, `transcript.md`, `digest.md` | unchanged |

Both logs draw from **one monotonic `seq` counter per session**, so a single
`Last-Event-ID` resumes both streams coherently.

Event kinds (`schema: 3`):

```
session_created   full resolved config: mode, panel, protocol, budgets, project_dir
phase_start       {phase}
round_start       {round, panel}
turn_start        {agent, round, phase, prompt_chars}      ← "who is speaking now"
turn_delta        {agent, kind: text|tool|usage, …}        → stream.jsonl
turn_end          {agent, verdict, comment, reason, tokens, seconds, session, resumed}
plan_received     {agent, chars, seconds, tokens, session}
chair_message     {text, by: user|agent}
control           {action, by, …}
compacted · panelist_dropped · session_fallback · early_ready_ignored
session_end       {rounds, termination, tokens}
```

`turn` is split into `turn_start` / `turn_end` — that split is what makes a live roster
possible at all. V1/V2 sessions remain viewable: the reducer treats a legacy `turn` as
start-immediately-followed-by-end.

**Prompts are logged in full** (bounded per record, spilling to a file beyond ~200 KB).
Without the exact prompt we sent, the per-agent view is half-blind and "why did this
panelist go off the rails" is unanswerable.

---

## 7. Per-model chat history

Two views over the same log, filtered by `agent`:

- **Council view** — the debate: plans, turns, verdicts, round structure. What today's
  dashboard shows, but live.
- **Agent view** — one panelist's own thread: every prompt the daemon sent it, its
  streamed reply, its file/tool activity, its token spend, its harness session id, and
  every failure or session fallback it hit.

`GET /api/sessions/{id}/agents/{label}` serves the second. It is the answer to
"modellerin tek tek sohbet geçmişini görebilme" **and** the project's debugging surface.

---

## 8. Session modes — plan from scratch, or start from the main agent's output

**Decision: `mode` is a first-class field on session creation.**

| mode | Phase 1 | Seed | When |
|---|---|---|---|
| `independent` | yes | — | today's behaviour: nobody is anchored, the panel generates the plan |
| `review` | **skipped** | required | the main agent already has a plan/analysis; the panel goes straight to critiquing it |
| `hybrid` | yes | required, revealed **after** the plans are written | the panel forms its own view first, then meets the proposal |
| `consult` | **skipped** | optional | the panel opens on a brief instead of on plans — see §21 |

The seed (`seed.md`) is whatever the main agent produces — a draft plan, an analysis, a
diff. Delivered as `council start --seed plan.md`, `--seed -` (stdin, so the agent can
pipe its own output), or the browser's textarea.

**Stated trade-off, and it must be visible in the UI:** `review` deliberately
reintroduces the steering that V2 removed. The panel now argues inside the main agent's
framing. That is the correct choice when you want a plan **verified**, and the wrong one
when you want a plan **generated**. `hybrid` is the honest middle and should be the
default whenever a seed exists.

Mechanically this needs one new prompt template (`review_turn.md`) and a digest framed as
*verdict + blockers* rather than *convergence*.

---

## 9. Control during a run

**Decision: control commands take effect at turn boundaries.** A harness call is atomic
— it can be killed, never steered mid-flight. Everything else waits for the current turn
to end, which keeps the transcript coherent.

```
POST /api/sessions/{id}/control
  pause | resume            stop after the current turn / continue
  stop  {graceful|hard}     graceful → write the digest from what exists
  skip  {agent}             sit this panelist out for one round
  drop  {agent} / restore
  extend {max_rounds|token_budget|wall_clock_budget}
  digest                    end the debate now and synthesise
  chair {text}              inject a message into the debate
```

`chair` is the interesting one: the message becomes a transcript turn labelled `Chair`
and reaches every panelist through the delta the existing `_session_prompt()` already
computes — no new plumbing. It is how a human (or the main agent) redirects a debate that
has gone sideways.

Every control is a `control` event in the log, so the transcript permanently records who
intervened, when, and how.

---

## 10. One daemon, many projects

**Decision: the daemon is user-level and project-aware; it is not started per repository.**

- Artifacts stay where they always were: `<project>/.council/<id>/` — next to the code,
  git-ignorable, unchanged on disk.
- `~/.council/registry.json` maps session id → project dir, so the UI lists sessions
  across every project and offers a project switcher.
- A per-project daemon would mean N ports, N UIs and N browser tabs — the opposite of
  "everything is managed from one place".

**Security.** Binding to `127.0.0.1` is *not* sufficient on its own: any page open in the
user's browser can POST to localhost, and this daemon spawns agents that read source
trees. Therefore:

- a token in `~/.council/daemon.json` (mode 0600) is required on every request — sent as
  a header by the CLI, or set once as a cookie from the `?token=` on the URL `council up`
  prints;
- `Origin` / `Host` allowlist, so DNS-rebinding cannot reach the API;
- `project_dir` must be an **already-registered** directory. The API will not run agents
  in an arbitrary path just because someone posted one;
- panelists remain read-only through the existing per-adapter flags — that is a property
  of the design, not of the UI, and the UI must not be able to relax it.

---

## 11. Stack

| Layer | Choice | Why |
|---|---|---|
| Backend | **FastAPI + uvicorn** | shares one asyncio loop with the orchestrator (the adapters are already async); native `EventSourceResponse`; pydantic validates the session-creation payload, which is now a real form |
| Frontend | **Vite + React + TypeScript** | multi-session routing, live panes, per-agent threads and settings forms are an application now, not a page |
| Delivery | built to `council/web/dist`, shipped as package data | Node is a **build** dependency only; `pip install` + `council up` stays the whole story for a user |
| Styling | keep the existing visual language from `dashboard.html` | it is good; it becomes the CSS baseline rather than being thrown away |

Runtime dependencies go from `pyyaml` to `pyyaml, fastapi, uvicorn`. The V1 "no
framework" decision was made when this was a ~200-line CLI conductor; it is now a control
plane with streaming, replay and lifecycle management, and hand-rolling that on
`http.server` means a threading/asyncio bridge nobody wants to maintain.

**Rejected:** Electron/Tauri (nothing here needs a native shell); Next.js (SSR is
meaningless for a localhost tool); stdlib `http.server` (see above); htmx/vanilla (the
per-agent live panes push it past what is pleasant without a component model).

---

## 12. API surface

```
GET  /api/health                          {app, version, pid}
GET  /api/catalog                         selectable agents/models + per-CLI availability
                                          and auth status (powers the "new council" form)
GET  /api/projects        POST /api/projects
GET  /api/sessions?project=               list
POST /api/sessions                        create + start  →  {id, url}
GET  /api/sessions/{id}                   snapshot (fold of the log)
GET  /api/sessions/{id}/events?from=N     SSE
POST /api/sessions/{id}/control           pause|resume|stop|skip|drop|extend|digest|chair
GET  /api/sessions/{id}/agents/{label}    one panelist's own thread
GET  /api/sessions/{id}/digest
GET  /api/events                          SSE, daemon-level (session list)
```

Session creation payload — this is the concrete answer to *"with what settings will a new
council start?"*:

```jsonc
{
  "project_dir": "C:/Users/…/agent-council",
  "mode": "independent",              // independent | review | hybrid
  "task": "…the user's words, verbatim…",
  "seed": null,                       // required for review/hybrid
  "panel": [
    {"name": "gpt",    "adapter": "codex_cli",  "model": null},
    {"name": "claude", "adapter": "claude_cli", "model": "opus"}
  ],
  "protocol": {
    "min_rounds": 2, "max_rounds": 5,
    "token_budget": 8000000, "wall_clock_budget": 1800,
    "compaction_threshold": 200000,
    "anonymize": true, "session_continuity": true,
    "compaction_panelist": "claude"
  },
  "timeouts": {"per_call": 300, "per_call_phase1": 900},
  "on_failure": "skip_with_note"
}
```

`council.yaml` survives as the **default** for this payload: the form is pre-filled from
it, per-run edits never touch the file, and an explicit "save as default" writes back.

---

## 13. What happens to the current code

| Component | Fate |
|---|---|
| `council/adapters/*` | **kept**; gains the `on_line` / `parse_line` streaming path; `claude_cli` switches to `stream-json` |
| `config.py`, `catalog.py`, `panel.py`, `transcript.py`, `envelope.py`, `prompts/` | **kept** |
| `orchestrator.py` | **reworked** — new event set, modes, control hooks, driven by the daemon rather than by `main()` |
| `dashboard.py` | **replaced** by `council/server/` (app, api, sse, registry, control) |
| `static/dashboard.html` | **replaced** by `council/web/` (React), styling carried over |
| `__main__.py` | **thin client**; keeps a `--local` standalone path so `--mock` and CI run with no daemon |
| `claude/commands/council.md` | **rewritten**; new `/council-apply <id>` alongside it |

The `--local` escape hatch matters: the mock end-to-end test must not require a daemon,
a port and an HTTP round-trip to prove the protocol works.

---

## 14. End-to-end sequences

**From Claude Code** (`/council I want to add rate limiting`):

1. the agent writes `task.md` **verbatim** — the zero-steering rule is untouched;
2. `council up --open` → daemon (or reuse) + browser;
3. `council start --project . --task .council/<ts>/task.md --mode independent --json`
   → `{id, url}`; the agent tells the user the URL in its first message;
4. the daemon runs the session in its own loop, streaming to the browser; the agent polls
   `council status <id> --json` every ~30 s and narrates each phase/round change;
5. `council digest <id>` → the agent synthesises the unified plan, as today.

**From the browser** (no Claude Code involved):

1. *New council* → project, mode, panel, budgets, task text → Start;
2. watch it live; intervene with pause / chair / drop as needed;
3. at the end, the hand-off panel offers `/council-apply <id>` to paste into Claude Code.

---

## 15. Reversible calls, flagged

These are decided, not open — but they are the ones to overturn first if the shape turns
out wrong, and none of them is load-bearing for the rest of the design:

1. **FastAPI + React** rather than a dependency-free stack. Reversible while the API
   contract stays the same; the event log is the real contract.
2. **User-level daemon** rather than one per project. If project isolation ever matters
   more than a single pane of glass, the registry is the only thing that changes.
3. **`review` mode's anchoring.** It contradicts the founding zero-steering principle on
   purpose. If, in use, review-mode digests turn out to be rubber stamps, the answer is
   to make `hybrid` the only seeded mode.

---

## 16. What the build changed

Six things came out differently once this was implemented. Each is a correction to the
plan above, not a deviation from it.

1. **`council run` stayed put.** The plan had it become a thin client with a `--local`
   flag. It is cleaner as two verbs: `council start` is the daemon path (and what
   `/council` uses), `council run` is the headless in-process path. Same escape hatch,
   no flag, and every existing script and test keeps working.

2. **Detaching the daemon was not enough.** `DETACHED_PROCESS` and even
   `CREATE_BREAKAWAY_FROM_JOB` do not save it: terminals and agent harnesses clean up
   with `taskkill /T`, which walks **parent → child** links, and breaking away from a
   job does not change who your parent is. The daemon is therefore started through
   `council/server/launcher.py`, which starts it and exits immediately — leaving it
   parented to a process that no longer exists, and so on nobody's tree. Without this,
   a control plane opened by `/council` dies with the turn that opened it.

3. **`os.kill(pid, 0)` cannot be used on Windows.** For a pid that has gone, CPython
   raises `SystemError: <class 'OSError'> returned a result with an exception set` —
   which no `except OSError` catches, and which took a request down with it. Liveness
   now goes through `OpenProcess`/`GetExitCodeProcess`. This bug was inherited from the
   V1 dashboard; it only ever fired on an interrupted session.

4. **The token is reused across restarts,** not regenerated. It is a local secret in a
   0600 file, and rotating it on every restart silently breaks every open browser tab.

5. **A `heartbeat` event was added** (verbose log, one per turn). `status.json` is only
   rewritten when the phase or round changes — minutes apart in a real session — so the
   token and elapsed counters had nothing to move on between rounds.

6. **The mock panel is reachable through the daemon** (`council start --mock`, and a
   checkbox in the form). Developing the UI against a real panel would cost a
   subscription on every reload; the scripted panel exercises the same streaming,
   control and event paths for free.

## 17. Verified against a real harness

The streaming design was written from the docs and then checked against
**codex-cli 0.146.0**, which corrected two guesses and confirmed one limitation.

- An item's kind lives under `item.type`, not `item_type`. The parser reads either;
  the fixture in `tests/test_adapters.py` is now a verbatim capture rather than an
  invention, because a fixture written from the docs would not have caught this.
- An `mcp_tool_call` carries `server`, `tool` and an `arguments.title` the model wrote
  itself. The title is what a person can actually read — "Verify API control flow"
  beats "node_repl" — so it is preferred over both.
- **Codex does not stream assistant text.** `agent_message` arrives once, complete, as
  an `item.completed`. Token-level streaming works for `claude`; for codex the live
  view shows tool activity throughout the turn and then the whole message at once.
  Nothing to fix — it is what the harness emits.

Session continuity was confirmed the only way it can be: one call planting a codeword,
a second call resuming that thread id in a fresh process, and the model recalling it.
That is the mechanism the whole of Phase 2 rests on.

A real two-panelist session then ran end to end — parallel exploration, a resumed
discussion turn, envelope parsing, digest. It cost **665k tokens on a five-line
repository**, which is the strongest available argument for sizing `token_budget` in
the millions.

---

## 18. What driving the finished UI changed

The design was then exercised end to end in a browser — every control, both reducers,
three viewport widths. What that found was not layout; it was **six features that were
built, wired and inert**, each one visible only from the outside.

- **`restore` worked everywhere except on screen.** The orchestrator really did put the
  panelist back and it really did speak again, but neither reducer handled
  `panelist_restored`, so every view kept it greyed out — still offering the restore
  that had already happened. `turn_skipped` was unhandled in both too: skip left no
  turn behind, so it left no trace at all.
- **`DELETE /api/sessions/{id}` had no caller.** The list grew to fourteen rows of the
  same sentence with no way to remove any, and no timestamp to tell them apart —
  `list_sessions` did not return one. Both were one-line gaps that made the front door
  unusable.
- **`extend` did not update the settings it extended.** A council raised to eight rounds
  went on reporting the five it was convened with, on the one screen whose whole job is
  to say how this council is configured.
- **The plans had nowhere to live.** Phase 1 writes an independent plan per panelist and
  runs for minutes; the debate tab said "nothing is said out loud yet" and showed a
  blank page. They are the clearest picture of where the models disagree — written
  before any of them saw another's — and they now appear as they land.
- **`use council.yaml` never said what it meant.** A checkbox that decides which
  providers read your repository, and the form would not name them.
- **A failed harness pasted its whole stdout into the UI.** `claude -p` exits 1 on an
  expired login while emitting a `result` event naming the cause; `run_process` dropped
  the output and reported the exit code with two kilobytes of JSONL tail. One
  misconfiguration then filled the screen with the same four hundred characters six
  times over — once per panelist, per stage.

The lesson is narrow and worth writing down: **an event the orchestrator emits and no
reducer folds is a feature that exists only in the log.** The cross-check is mechanical
— every `_event(...)` name against every `_on_*` handler and every `case` — and
`tests/test_event_coverage.py` now runs it, because no test that never renders will
catch it.

---

## 19. Two more, from finally running real panelists

A real two-panelist codex session — 1.0M tokens, both READY in two rounds — found two
things no mock could have.

- **The elapsed clock read `0s` for the first three minutes.** Elapsed only advances on
  the heartbeat at the end of a turn, and phase 1's "turn" is a panelist reading an
  entire repository. Mock panelists answer in seconds, so the gap never opened wide
  enough to see. A live session is now timed from `started_at` in the browser; a
  finished one keeps the number it recorded.
- **`gpt-5.6-luna` escapes its newlines twice**, on every turn — `\n` arrives as two
  characters inside an envelope that is otherwise valid JSON, and reaches the digest as
  a literal `\n` mid-sentence. `gpt-5.6-sol`, on the same panel, wrote real newlines
  throughout. So it is a habit of particular models, not a bug on either side, and
  `_unescape_newlines` repairs it only where the field has no real line break — a field
  already laid out in lines is being read literally.

The second is the argument for testing against real models rather than only faithfully
scripted ones: a mock reproduces the protocol, never the model's habits.

---

## 20. What four parallel auditors found

Four agents then went at the CLI, the HTTP surface, the failure paths and the codebase
as a whole, in parallel, against mock panels. The findings that mattered were not in
any of the features — they were in the places where the program was *confidently
wrong*.

**The one safety promise was broken twice.** `envelope.py` states it "never invents a
READY". It did:

- `"verdict": "NOT READY"` parsed as READY, because the check was a substring test —
  and it was not even flagged malformed, so the log and the digest both recorded a
  blocked panelist as consenting.
- The prose fallback matched any line *starting* with the word. "Ready or not, this
  will lose customer data" ended the council. So did a polite "Ready to discuss
  further next round." at the foot of a list of blockers — and that path is reached by
  a trailing comma, the commonest JSON error models make.

Verdicts are now read asymmetrically, which is the principle the code should have had
from the start: **liberal about what counts as "keep arguing", strict about what counts
as consent.**

**The digest asserted unanimity while deleting the dissent.** `render_digest` iterated
the surviving panel, so a panelist that argued a blocker and then lost one call was
dropped and its objection vanished — from the one artefact that gets handed to another
agent to implement. Compounding it, `on_failure: skip_with_note` did not skip: one
transient timeout removed a model for the whole session, so two unrelated flakes on a
three-seat panel lost quorum — and losing quorum then discarded the transcript and the
digest for rounds that had really happened and really been paid for.

**A session id was a path.** `DELETE /api/sessions/..%5C..%5Cx` reached `shutil.rmtree`
and removed that directory and everything under it; the matching GET read files from
anywhere on disk. Routing blocks `/`; on Windows a backslash separates paths just as
well and nothing blocked it. Session ids are now matched against the pattern `new_id`
mints, because the set of valid ids is small and exactly known — a whitelist is both
simpler and stricter than trying to strip what is dangerous.

**Falling back was the wrong default in three places.** `panel: []` fell through to
council.yaml and convened four real models; a misspelled top-level key in council.yaml
was ignored, so every setting inside it silently did nothing; `--max-rounds 0` was
accepted and explored the whole repository before holding no rounds at all. Each is the
same mistake: treating an obviously-wrong input as an absent one.

**And the program could not print its own prose.** Every progress line and the whole
model catalogue contain em dashes; a Windows console is cp857 or cp1254 by default —
Turkish machines, where this is developed — and `print` raised `UnicodeEncodeError`.
That killed a council *after* Phase 1, having spent the money, over one status line.

The pattern worth keeping: **an auditor is only useful if it can run the thing.** Every
one of these came from executing a path, not from reading it — and the four ran in
parallel against mock panels, so the whole sweep cost no tokens and no model calls.

---

## 21. Bringing the council into a session already in progress

The tool assumed a cold start: the panel is told the task and nothing else, and half an
hour later there is a digest. Used from inside a Claude Code session that has been
running for an hour, that is wrong twice. The panel rediscovers what the room already
knows — reopening settled arguments, re-proposing rejected options — and the only speed
on offer is the slow one, so "is there a reason not to do this?" never gets asked at all.

Two orthogonal additions, and keeping them orthogonal is the point: one is about **what
the panel is told**, the other about **how long it argues**.

### The context channel, and the one rule on it

`context.md` — a brief on where the work already stands, written by the agent that
convened the council. Optional in every mode, carried like `seed.md`, and bound by a
single rule:

> **A panelist writing its independent plan never sees it.** The brief enters at the
> start of the discussion.

That boundary is the whole feature, and the reason it is enforced in `_context_block`'s
callers rather than left to the caller's discretion. Without it, briefing the panel
replaces four independent readings of the repository with four elaborations of one
reading — which is precisely what this tool exists to prevent. With it, the brief is
something the panel meets *after* it has a view of its own, so it can be argued with.
This is not a new idea in the system: it is exactly the treatment `hybrid` already gives
a supplied proposal, generalised.

The digest records which way round it happened, because by the time anyone reads it
nobody remembers how the run was set up, and it changes how much of the agreement below
is the panel's own.

A deliberate non-feature: nothing auto-assembles the brief. No `git diff` capture, no
transcript dump. The convening agent chooses what is worth saying, which is a judgement
the tool is in no position to make. (One constraint that forces a habit: the `claude`
adapter runs with `Bash` disallowed, so a Claude panelist cannot run `git diff` itself.
Whatever is not pasted, half the panel cannot see.)

### `consult` — the panel opens on the brief instead of on plans

The first cut of this mode held exactly one round and returned from its own method,
bypassing the protocol entirely. That was a mistake with a clean tell: `min_rounds`,
`max_rounds`, `all_ready`, compaction and the budget checks all had to be either
reimplemented or done without, and there was no good answer to "why is this mode not
like the others?".

**How long a council argues is a number, not an identity.** So `consult` now differs from
`independent` in exactly one way — what the panel opens with — and `--max-rounds 1`
recovers the original single-round behaviour as a setting rather than a mode.

Mechanically that is one branch inside `phase2`: round 1 of a `consult` runs
`_round_in_parallel` instead of the round-robin body, and everything after it is the
ordinary loop. The opening round is Phase 1's independence at one call's price — each
panelist gives its own reading of the brief without seeing the others — and it costs
about one Phase 1, measured at 3–8 minutes against a real panel.

Two properties of a parallel round have to be maintained by hand, and both are subtle
enough to be worth naming:

- **`_seen` must not advance past the round's own turns.** `_mark_caught_up` records
  "you have seen everything so far", which is true after a round-robin turn and false
  after this one — these turns happened *simultaneously*, so none of them was in anyone's
  prompt. Marking them seen would drop every one from round 2 and leave a panel that had
  never heard a word the others said, with a transcript that still read like a debate.
  The regression test for this only works against a session council: without one, every
  prompt is a full reassembly and `_seen` is never consulted, so the first version of the
  test passed against deliberately broken code.
- **Results are committed in panel order.** Events fire as each panelist finishes, which
  is when it really happened and is what makes the live view live; the transcript and the
  digest must not be ordered by which model was quickest that afternoon.

`READY` here means *"I looked, and I see no reason not to proceed"*; `CONTINUE` means
*"I found something that should change this decision"*. That maps onto the question a
working agent actually has, which is why the existing envelope fits a mode it was not
designed for.

What a **one-round** run gives up is stated at the top of its digest: no panelist heard
any other, so agreement is independent agreement rather than a settled argument and
disagreement was never tested. That warning is keyed on the round count, not the mode — a
consultation that went on to debate has earned an ordinary digest.

### One `/council`

Two slash commands with opposite instructions — one that briefed the panel and one that
was forbidden to — could not survive the context channel being safe in every mode. They
are now one command whose first decision is the same as the CLI's: what should the panel
start from? It also documents every control verb, because steering a running council from
a *later* session had no discoverable entry point at all.

---

*V3, 31 July 2026 — supersedes the "Integration" and "Directory template" rows of
[plan-council-design.md](plan-council-design.md); the debate protocol itself is unchanged.
§21 added 4 August 2026.*
