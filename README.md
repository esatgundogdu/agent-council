# Plan Council

Several model agents read your repository independently, write their own plans, argue
about them, and stop when they agree. You get a digest; Claude Code turns it into one
plan.

The point is the disagreement. Each panelist explores the repo itself, so nobody
inherits anyone else's framing, and the plans collide on real differences rather than
on wording.

Everything is driven from a local control plane — a small daemon with a web UI. Start a
council, watch who is writing right now, read any panelist's own history, interrupt the
debate, and hand the result back to Claude Code, all from one window.

```bash
council up --open
```

```
/council I want to add rate limiting to the public API
/council is a token bucket right here, given what we just decided? one round
```

One command, and the mode follows from what you asked for: design something from
nothing, or get the panel's read on the decision in front of you right now — briefed on
where this session has already got to, and back in minutes.

See [docs/web-control-plane-design.md](docs/web-control-plane-design.md) for how the
pieces fit together, and [docs/plan-council-design.md](docs/plan-council-design.md) for
the reasoning behind the debate protocol. It is the V2 document and is archived: the protocol still holds, the commands and layout it describes do not.

## How it works

```
Browser ──REST + SSE──▶ the daemon ──subprocess──▶ codex · claude · opencode
                            ▲
   council CLI (and the /council slash command) ─┘
```

The browser never touches a CLI, and it could not: those harnesses work only because
they run as you, on your machine, with your `PATH` and your provider logins. The daemon
is an ordinary local process, so it inherits all of that; the browser just tells it what
to do. The CLI and the main agent are clients of the same API, which is why a council
started from a terminal appears in the browser immediately, and one started in the
browser can be handed back to Claude Code when it finishes.

1. **Phase 1 — independent plans.** Every panelist gets the task text byte-for-byte,
   explores the repository read-only through its own CLI harness, and writes a plan.
   In parallel, and nobody sees anyone else's plan.
2. **Phase 2 — the discussion.** Round-robin. Each panelist argues, verifies claims
   against the repo, and ends its turn with `CONTINUE` or `READY`. The debate ends when
   everyone says `READY` (never before round `min_rounds`), or a limit is hit.

   One exception, and it is the point of `consult`: that mode has no Phase 1, so its
   **first** round is answered in parallel with nobody seeing anyone else — the same
   independence Phase 1 buys, for the price of one call. Round 2 onward is round-robin
   like everything else.

   Each panelist speaks from **the same harness conversation in which it explored the
   repo**, so it keeps everything it learned writing its plan; it is sent only what it
   has not yet seen. If a session is lost or a harness cannot resume, that turn falls
   back to a full reassembly of task + plans + transcript.
3. **Phase 3 — the digest.** Final positions, unresolved objections, and pointers to
   the full transcript. Claude Code reads the digest and writes the unified plan.

Panelists are anonymous (`Agent-A`, `Agent-B`, …), shuffled per session. The mapping
back to real models lives only in `events.jsonl`, so the argument is judged rather than
the brand.

## Four ways to start

One axis: **what does the panel start from?** All four then debate the same way and end
when everyone says READY.

| Mode | The panel opens with | Phase 1 | Needs |
|---|---|---|---|
| `independent` | a plan each panelist wrote alone | yes | — |
| `consult` | its own reading of your brief, answered in parallel | no | `--context` |
| `review` | a critique of your proposal | no | `--seed` |
| `hybrid` | its own plans, *then* your proposal | yes | `--seed` |

`review` is how you hand the main agent's own output to the panel: it skips straight to
the critique. It also deliberately anchors the panel on that proposal's framing — the
right mode for **verifying** a plan and the wrong one for **generating** one. `hybrid`
is the honest middle when you have a proposal but still want independent thinking first.

`consult` is the cheapest way in, because nobody writes a plan first. Its opening round
is answered **in parallel, with no panelist seeing another** — Phase 1's independence at
one call's price — and from round 2 it is an ordinary debate.

```bash
council start --task task.md --mode review --seed plan.md
claude -p "write me a plan" | council start --task task.md --mode hybrid --seed -
council start --task question.md --mode consult --context brief.md
```

### How long it argues is one number, not a mode

`--max-rounds` is the whole answer, and it works the same in every mode:

```bash
council start --task question.md --mode consult --context brief.md --max-rounds 1 --follow
```

That stops after the parallel opening round: **3–8 minutes**, and a straight answer to
"is there a reason not to do this?". What you give up is stated at the top of the digest
every time — nobody heard anybody, so agreement there is independent agreement rather
than a settled argument, and disagreement was never tested. Two rounds or more and it is
an ordinary council that happened to skip the plans.

### Telling the panel where you already are

`--context <file>` is a brief on the work in progress — what has been decided and why,
what was tried and rejected, what is still uncertain. It is optional in every mode, and
there is exactly one place it never goes:

> **A panelist writing its independent plan never sees it.** It arrives at the start of
> the discussion, where the panel already has its own view and the brief is something to
> argue with rather than something to assume.

That boundary is the feature. Without it, briefing the panel would quietly replace four
independent readings of your repository with four elaborations of yours — which is the
one thing this tool exists to avoid. `hybrid` already gives a supplied proposal the same
treatment, for the same reason.

Because the tool enforces it, briefing is safe in every mode: `/council` writes the brief
whenever the session has something worth passing on.

## Requirements

| Panelist | Harness | Auth |
|---|---|---|
| GPT | [`codex`](https://developers.openai.com/codex) | ChatGPT subscription (`codex login`) |
| Kimi / GLM / DeepSeek | [`opencode`](https://opencode.ai) | Ollama Cloud API key (`opencode auth login`) |
| Claude | `claude` | Claude subscription (shares your Claude Code quota) |

Python ≥ 3.10. Node is needed only to rebuild the UI, never to run it.

## Setup

```bash
pip install -e .
```

Check the harnesses are working before your first session:

```bash
codex exec - --sandbox read-only --skip-git-repo-check <<< "Reply with OK"
opencode run --agent council-plan -m ollama-cloud/kimi-k2.6 "Reply with OK" < /dev/null
```

Install the slash commands:

```bash
cp claude/commands/*.md ~/.claude/commands/
```

| Command | For |
|---|---|
| `/council` | convene one, in any mode, and steer it. Everything the CLI can do. |
| `/council-apply` | turn a session that finished elsewhere into one plan. |

The same steps in PowerShell — the heredoc and `/dev/null` are POSIX-only, and
`council` closes the harness's stdin for you either way:

```powershell
"Reply with OK" | codex exec - --sandbox read-only --skip-git-repo-check
opencode run --agent council-plan -m ollama-cloud/kimi-k2.6 "Reply with OK"
Copy-Item claude\commands\*.md $HOME\.claude\commands\
```

Try the whole pipeline without spending anything:

```bash
council start --task task.md --mock
```

## The control plane

```bash
council up          # start it (idempotent) and print the URL
council up --open   # …and open a browser
council down        # stop it
council serve       # run it in this terminal instead, for development
```

One daemon serves every project. It binds `127.0.0.1` and nothing else: it runs CLI
agents over your source, so it is single-user by construction and there is no hosted
mode. Requests carry a token from `~/.council/daemon.json`; the browser gets it once,
from the URL `council up` prints, and keeps it in a cookie. A project must be registered
before agents will be run inside it — `council start` does that for the directory you
name, and the API will not take an arbitrary path from anyone else.

The window opens on every council on the machine, newest first, each with the time it
ran — and a delete, because councils are cheap to start and their tasks rhyme. Opening
one gives you three tabs:

- **the discussion** — the debate as the group conversation it is. Panelists on the
  left, each with its own colour; you and the main agent on the right, because a chair
  message is an instruction into the room rather than one of the arguments. Rounds are
  the divider chips a chat puts between days, a long turn collapses to its opening and
  the one-line reason its author wrote, and whoever has the floor is shown typing —
  with the file it is reading. Controls sit in the header: pause, stop, raise the round
  limit, wrap up early. The composer at the bottom talks to the whole panel.
- **their plans** — one tab per panelist for the independent plans of Phase 1. Only on
  the modes that have one.
- **digest** — the synthesis, and the `/council-apply` line that hands it to Claude Code.
- **what it was given** — the task verbatim, the brief and the proposal if there were
  any, and every setting this council is running under, including ones raised mid-run.

Above the conversation is the room it is happening in: the panel at a round table,
whoever convened it at the head — the main agent when `/council` did, you when you did.
Whoever has the floor is on their feet; in Phase 1 everyone is writing. Clicking a
figure opens that panelist and nothing else: the exact prompts it was sent, the files it
opened, what it said between them, what it replied, plus **console**, the raw output of
each of its harness processes. Skip and drop live there too.

The scene is WebGL — a real round table, a chair and a figure per seat, one
shadow-casting light. Every shape is a primitive, so there is no model to download, no
texture and no loader; `three` itself sits in a chunk behind a dynamic import, and with
the room switched off the application downloads exactly what it did before it existed.

**The render loop stops.** Every frame asks whether anything still has to move — a pose
still settling, a pen still writing, a speaker still breathing — and when the answer is
no it cancels itself. Measured: a council in Phase 1 with three panelists writing draws
164 frames in five seconds; a finished council draws **zero**, and costs 0.3ms of script
over the same five seconds. It also stops when the tab is hidden or the canvas scrolls
out of view, the pixel ratio is capped at 1.5, and `prefers-reduced-motion` goes
straight to the final pose.

The **3D / flat** switch in the corner of the room turns it off for good, and the choice
sticks. Off, or on a machine with no WebGL, or after a lost GL context, the same scene
is drawn in CSS 3D and SVG instead — not an error state, a second complete room.

Dark by default, light behind the ☀/☾ in the corner; the choice sticks.

## The CLI

Everything the UI does, the CLI does, because both are clients of the same API.

| Command | |
|---|---|
| `council start --task F --project-dir D` | convene; returns a session id at once |
| `council start … --follow` | …and stream progress until it ends |
| `council watch [id]` | follow a running session in this terminal |
| `council status [id] [--json]` | one-shot progress report |
| `council sessions` | every session, across every project |
| `council digest [id]` | print a finished session's digest |
| `council control <action> [id]` | `pause resume stop skip drop restore extend digest chair` |
| `council models` | what you can pass to `--agents` |
| `council run --task F` | run headlessly in this process, no daemon, no port |

`council run` is the escape hatch: no daemon, no port, nothing to watch. Use it in CI
and scripts. Everything else goes through the control plane.

### Choosing the panel

The default panel lives in [`council.yaml`](council.yaml), and the UI's form is
pre-filled from it. To see what you can pick:

```bash
council models
```

Every name in that listing comes from the harness itself. There is no table of model
ids in this repository, because a shipped list of model names is stale the week after
it is written:

- **codex** enumerates — `codex debug models` renders its own catalogue, the same one
  its picker uses, locally and in milliseconds.
- **opencode** enumerates — `opencode models`.
- **claude** cannot. `--model` takes either a family alias or a full id, and the server
  resolves it. So ids are *derived*: `claude opus 5` becomes `claude-opus-5` by rule,
  and the CLI decides whether that exists.

### When a harness is not on PATH

Panelists look their harness up on the PATH of whatever process runs the council, which
is not always enough. The ChatGPT desktop app, for one, installs codex under a
content-hashed directory — `…/OpenAI/Codex/bin/<hash>/codex.exe` — that is on nobody's
PATH and gets a **new hash on every update**. Give that panelist the path instead, as a
glob, so an update does not quietly break it:

```yaml
- name: gpt
  adapter: codex_cli
  binary: C:\Users\you\AppData\Local\OpenAI\Codex\bin\*\codex.exe
```

The newest match wins. This beats a PATH entry, which has to be set before the daemon
starts and is silently absent for whoever forgets. `council models` reads the same
setting, so its listing is what a council here would really run.

To set the panel for a single run:

```bash
council start --task task.md --agents "gpt, glm-5.2, kimi k2.6, claude opus"
```

Each entry resolves on its own:

| You write | You get |
|---|---|
| `gpt` | codex, using the default model in `~/.codex/config.toml` |
| `gpt-5.5` | matched against codex's own catalogue |
| `gpt 5.4 mini` | the same thing — spaces, dots and dashes are interchangeable |
| `claude` | the `claude` CLI default |
| `claude opus` | the family alias, which always means the current Opus |
| `claude opus 5` | `claude --model claude-opus-5` |
| `kimi k2.6`, `glm-5.2` | matched against `opencode models` |
| `gpt@max`, `claude opus@low` | the same, with how hard it should think |

Prefer the bare family alias over a pinned version: `claude opus` keeps meaning the
newest Opus, while `claude opus 5` will still mean Opus 5 a year from now.

### How hard each panelist thinks

`effort` — `low`, `medium`, `high`, `xhigh`, `max` — per panelist, in `council.yaml` or
as `@level` on an `--agents` entry:

```yaml
panel:
  - name: gpt
    adapter: codex_cli
    effort: max
  - name: claude-sonnet
    adapter: claude_cli
    model: sonnet
    effort: low        # a cheap dissenter against three deep thinkers
```

codex and the claude CLI happen to accept exactly the same five words, so this is one
setting rather than two; `opencode` has no such control and setting it there is an error
rather than a line that does nothing. Omit it and each harness keeps its own default —
for codex that is `model_reasoning_effort` in `~/.codex/config.toml`, which applies
globally to every codex panelist at once and is invisible from this repository. Setting
it here makes it a property of the council instead.

It is not a small dial. The same two-panelist consultation on this repository took **28
seconds and 114k tokens** at `low`, and **3m 40s and 1.3M tokens** at the codex config's
`high`. `events.jsonl` records the effort next to the model, so a run's cost can be
explained afterwards.

An ambiguous or unknown name is an error rather than a guess, and it tells you what the
harness really offers — `gpt-5.6` lists the three models that start that way.

Panelists run through a dedicated read-only opencode agent, `council-plan`, defined in
[`opencode/council.opencode.json`](opencode/council.opencode.json) and passed via
`OPENCODE_CONFIG`. It has `write`, `edit`, `patch` and `bash` disabled, which both
enforces read-only access and keeps a headless run from stalling on a permission prompt.
Your own opencode config is merged, not replaced, and never modified.

## Output

```
.council/2026-07-22_14300/
├── task.md          your words, unedited
├── seed.md          the proposal, when one was supplied
├── context.md       the brief, when one was supplied
├── plans/agent-*.md each independent plan
├── transcript.md    the full discussion
├── digest.md        final positions + unresolved points  ← the one to read
├── events.jsonl     the session's semantic log
├── stream.jsonl     live deltas: prompts, streamed text, tool calls
├── calls/*.log      what each harness process printed, unparsed
└── status.json      a derived snapshot, for anything that only wants to poll
```

Everything above `calls/` has been through a parser: `turn_end` holds the envelope's
comment, a delta holds a tool name and one truncated argument. `calls/` holds the raw
stdout and stderr of every harness process, with the exact command line at the top and
the exit code at the bottom — which is the whole record when a panelist times out or
exits non-zero, because then there is no envelope to have parsed. One file per call, so
a resumed turn whose session was refused keeps both the failure and the cold retry.
Capped at 2 MiB each, keeping both ends and dropping the middle; set
`capture_console: false` in `council.yaml` to write none at all.

Deleting a session deletes the directory, `calls/` included — nothing has to be cleaned
up separately.

`events.jsonl` is the contract. Everything any client shows is a fold over it, so a live
view and a reload of a finished session cannot disagree; it also holds every argument in
full, so a run that dies before Phase 3 still has its discussion on disk. `stream.jsonl`
is the verbose half — high volume, and the only source for the *panelists* tab,
which is rebuilt from the prompts and text deltas recorded there.

## Cost and time

A real session takes **15–45 minutes** — the panelists are reading your code, not
answering from memory. Phase 1 runs in parallel; Phase 2 is sequential by design,
because a debate where everyone speaks at once is not a debate.

A `consult --max-rounds 1` takes **3–8 minutes** and costs roughly one Phase 1, because
that is what it is: everyone reads the repository once, in parallel, and answers. That is
the whole trade — you pay for the exploration and skip the argument.

Three safeguards bound a runaway session: `max_rounds`, `token_budget` and
`wall_clock_budget`. Whichever trips first ends the discussion, and the digest says
which one it was. You can also raise any of them, or stop the session outright, while it
runs — a graceful stop still writes the digest from what exists.

**Privacy:** every panelist reads your repository through its own provider. Three
providers see your code in a default session. Choose the panel accordingly for
sensitive work — it is a deliberate choice, not a detail.

## Configuration

```yaml
protocol:
  min_rounds: 2          # a first-round consensus is usually a shallow one
  max_rounds: 5
  token_budget: 8000000
  wall_clock_budget: 1800
  compaction_threshold: 200000  # summarise older rounds past this size
  anonymize: true
  session_continuity: true
  compaction_panelist: claude

timeouts:
  per_call: 300          # a discussion turn
  per_call_phase1: 900   # a plan, including repo exploration

on_failure: skip_with_note   # or 'abort'
capture_console: true        # keep each harness process's raw output in calls/
```

The three limits stop a runaway session, and each measures something different:

- **`wall_clock_budget`** — real seconds since the session began, checked before each
  turn. It never interrupts a turn in progress, so a session can overrun by up to one
  turn.
- **`token_budget`** — *cumulative* real tokens across every call (from each CLI's own
  usage reporting; a char/4 estimate fills in only when a CLI reports none). The number
  is dominated not by the short transcript but by each panelist's **repo-exploration
  agent loop**: every file a panelist reads is re-sent to the model on the next step,
  and providers without prompt caching pay full price each time. Two codex panelists,
  one round, on a **five-line** repository measured 665k tokens. Size this in the
  millions, not thousands.
- **`compaction_threshold`** — the size the transcript must reach before older rounds
  are summarised. Keep it under the smallest panelist's context window: the prompt is
  the transcript *plus* the task, every plan, and the last two rounds verbatim.

`skip_with_note` drops an unresponsive panelist and carries on; the session fails only
if fewer than two remain.

## Troubleshooting

**A panelist is dropped immediately.** Its CLI cannot authenticate. Run the harness
check above by hand — the error in the panelist's own history is the CLI's own.

**A panelist is not dropped, but its CLI was installed after the daemon started.** The
harness is looked up on the PATH of the process running the council, and the daemon
inherited its PATH when it started. `council down && council up` from a fresh shell.

**Every codex `command_execution` fails with `codex-windows-sandbox-setup.exe:
program not found`.** Codex's Windows sandbox helper is missing from that install.
The panelist still works — it falls back to its MCP tools and reads the repo that way
— but every shell command it tries is a wasted round trip, which shows up directly in
the token bill. Reinstall codex, or expect the panelist to be slower and dearer than
the others.

**`opencode` hangs and never returns.** It blocks when it inherits an open stdin. The
adapter always closes stdin; if you are invoking opencode yourself, add `< /dev/null`.

**A panelist stops early with a context-length error.** `compaction_threshold` is
above that model's context window. Lower it, or drop the model.

**Everyone says READY in round one.** `min_rounds` forces a second round precisely
because that usually means a shallow review. If it keeps happening, the task
description is probably too narrow to argue about.

**The browser says the token is wrong.** The tab was opened against an older daemon.
Run `council up` and open the link it prints.

**The daemon will not stay up.** `~/.council/daemon.log` has its output. A daemon
started from inside a terminal that kills its process tree needs the breakaway that
`council up` already asks for; if the log says it could not break away, start it with
`council serve` in its own window instead.

## Development

```bash
pytest -q                                  # unit + end-to-end, no model calls
council start --task t.md --mock           # exercise the whole pipeline for free
council run --task t.md --mock             # the same, without the daemon
```

The UI lives in `web/` and builds into `council/web/`, which ships in the wheel:

```bash
cd web && npm install && npm run build
```

For UI work, run the daemon and Vite side by side — the dev server proxies `/api`, and
the origin has to be allowed explicitly:

```bash
council serve --dev-origin http://localhost:5173
cd web && npm run dev
```

Adding a harness means one file in `council/adapters/` implementing
`ask(prompt, cwd, timeout, session, on_delta) -> Reply` plus a `LineParser` that turns
its output into deltas, and an entry in the registry.
