# Plan Council

Several model agents read your repository independently, write their own plans, argue
about them, and stop when they agree. You get a digest; Claude Code turns it into one
plan.

The point is the disagreement. Each panelist explores the repo itself, so nobody
inherits anyone else's framing, and the plans collide on real differences rather than
on wording. See [docs/plan-council-design.md](docs/plan-council-design.md) for the
design and the reasoning behind it.

```
/council I want to add rate limiting to the public API
```

## How it works

1. **Phase 1 — independent plans.** Every panelist gets the task text byte-for-byte,
   explores the repository read-only through its own CLI harness, and writes a plan.
   In parallel, and nobody sees anyone else's plan.
2. **Phase 2 — the discussion.** Round-robin. Each panelist argues, verifies claims
   against the repo, and ends its turn with `CONTINUE` or `READY`. The debate ends when
   everyone says `READY` (never before round `min_rounds`), or a limit is hit.

   Each panelist speaks from **the same harness conversation in which it explored the
   repo**, so it keeps everything it learned writing its plan; it is sent only what it
   has not yet seen (the others' plans the first time, then each round's new messages).
   If a session is lost or a harness cannot resume, that turn silently falls back to a
   full reassembly of task + plans + transcript. Set `session_continuity: false` to
   always use the reassembly path.
3. **Phase 3 — the digest.** Final positions, unresolved objections, and pointers to
   the full transcript. Claude Code reads the digest and writes the unified plan.

Panelists are anonymous (`Agent-A`, `Agent-B`, …), shuffled per session. The mapping
back to real models lives only in `events.jsonl`, so the argument is judged rather than
the brand.

## Requirements

| Panelist | Harness | Auth |
|---|---|---|
| GPT | [`codex`](https://developers.openai.com/codex) | ChatGPT subscription (`codex login`) |
| Kimi / GLM / DeepSeek | [`opencode`](https://opencode.ai) | Ollama Cloud API key (`opencode auth login`) |
| Claude | `claude` | Claude subscription (shares your Claude Code quota) |

Python ≥ 3.10.

## Setup

```bash
pip install -e .
```

Check the harnesses are working before your first session:

```bash
codex exec - --sandbox read-only --skip-git-repo-check <<< "Reply with OK"
opencode run --agent build -m ollama-cloud/kimi-k2.6 "Reply with OK" < /dev/null
```

Install the slash command:

```bash
cp claude/commands/council.md ~/.claude/commands/council.md
```

## Choosing the panel

The default panel lives in [`council.yaml`](council.yaml). To see what you can pick:

```bash
council models
```

To set the panel for a single run, without editing anything:

```bash
council run --task task.md --agents "gpt, glm-5.2, kimi k2.6, claude opus 4.8"
```

Each entry resolves on its own:

| You write | You get |
|---|---|
| `gpt` | codex, using the default model in `~/.codex/config.toml` |
| `gpt-5.2` | codex with that model — any id is passed straight to `codex -m` |
| `claude` | the `claude` CLI default |
| `claude opus 4.8` | `claude --model claude-opus-4-8` |
| `claude opus` | the family alias, which tracks the latest Opus |
| `kimi k2.6`, `glm-5.2` | matched against `opencode models`; spaces, dots and dashes are interchangeable |

An ambiguous name is an error rather than a guess: `kimi` alone matches several
models and tells you which. In Claude Code, `/council --agents "..." <task>` does the
same thing, and asking "who is on the panel?" makes Claude list the options.

Panelists run through a dedicated read-only opencode agent, `council-plan`, defined in
[`opencode/council.opencode.json`](opencode/council.opencode.json) and passed via
`OPENCODE_CONFIG`. It has `write`, `edit`, `patch` and `bash` disabled, which both
enforces read-only access and keeps a headless run from stalling on a permission
prompt. Your own opencode config is merged, not replaced, and never modified.

## Usage

Normally through `/council` in Claude Code. Directly:

```bash
python -m council run --task path/to/task.md --project-dir .
```

| Flag | Meaning |
|---|---|
| `--agents` | panel for this run, overriding `council.yaml` (see below) |
| `--project-dir` | repo the panel explores, and where `.council/` is written (default `.`) |
| `--config` | alternative `council.yaml` |
| `--max-rounds N` | override the round limit; `1` gives a quick single-pass review |
| `--mock` | scripted panel, no model calls — for trying the pipeline out |
| `--session-dir` | write to an explicit directory instead of a timestamped one |
| `--quiet` | no progress output |

Exit codes: `0` success, `2` too few panelists survived, `3` configuration error.

## Watching a run

A session takes 15–45 minutes, so there is a dashboard:

```bash
council serve
```

It opens a browser on `127.0.0.1:8787` and follows the newest session — start it once
and it picks up each new run as it begins. It shows phase and round, each panelist's
current stance (exploring → thinking → CONTINUE/READY), every argument in full as it
arrives, and a health panel for drops, failed turns and session fallbacks. `Blind`
hides which model is which.

| Flag | Meaning |
|---|---|
| `--session <dir\|name>` | pin one session instead of following the newest |
| `--project-dir` | repo whose `.council/` to watch (default `.`) |
| `--port` | default 8787; a busy port falls back to a free one |
| `--no-browser` | do not open a window |

It only reads `status.json` and `events.jsonl`, so watching can never disturb a run —
and it can be started against a session that has already finished. A run whose status
still says `running` but whose process is gone (dead pid, or a heartbeat older than
15 minutes) is reported as **interrupted** rather than live.

### Output

```
.council/2026-07-22_1430/
├── task.md          your words, unedited
├── plans/agent-*.md each independent plan
├── transcript.md    the full discussion
├── digest.md        final positions + unresolved points  ← the one to read
├── events.jsonl     machine-readable log, incl. the real identities
└── status.json      live progress while running
```

`events.jsonl` holds every argument in full, so a run that dies before Phase 3 still
has its discussion on disk even though `transcript.md` was never written.

## Cost and time

A real session takes **15–45 minutes** — the panelists are reading your code, not
answering from memory. Phase 1 runs in parallel; Phase 2 is sequential by design,
because a debate where everyone speaks at once is not a debate.

Three safeguards bound a runaway session: `max_rounds`, `token_budget` and
`wall_clock_budget`. Whichever trips first ends the discussion, and the digest says
which one it was. For a quick pass, `--max-rounds 1`.

**Privacy:** every panelist reads your repository through its own provider. Three
providers see your code in a default session. Choose the panel accordingly for
sensitive work — it is a deliberate choice, not a detail.

## Configuration

```yaml
protocol:
  min_rounds: 2          # a first-round consensus is usually a shallow one
  max_rounds: 5
  token_budget: 4000000
  wall_clock_budget: 1800
  compaction_threshold: 200000  # summarise older rounds past this size
  anonymize: true
  compaction_panelist: claude

timeouts:
  per_call: 300          # a discussion turn
  per_call_phase1: 900   # a plan, including repo exploration

on_failure: skip_with_note   # or 'abort'
```

The three limits stop a runaway session, and each measures something different:

- **`wall_clock_budget`** — real seconds since the session began, checked before each
  turn. It never interrupts a turn in progress, so a session can overrun by up to one
  turn.
- **`token_budget`** — *cumulative* real tokens across every call (from each CLI's own
  usage reporting; a char/4 estimate fills in only when a CLI reports none). The number
  is dominated not by the short transcript but by each panelist's **repo-exploration
  agent loop**: every file a panelist reads is re-sent to the model on the next step,
  and providers without prompt caching (e.g. Ollama Cloud) pay full price each time. A
  single thorough panelist can spend hundreds of thousands of tokens exploring, so size
  this in the millions, not thousands.
- **`compaction_threshold`** — the size the transcript must reach before older rounds
  are summarised. Keep it under the smallest panelist's context window: the prompt is
  the transcript *plus* the task, every plan, and the last two rounds verbatim.

Whichever trips first ends the discussion, and the digest names it.

`skip_with_note` drops an unresponsive panelist and carries on; the session fails only
if fewer than two remain.

## Troubleshooting

**A panelist is dropped immediately.** Its CLI cannot authenticate. Run the harness
check above by hand — the error in `events.jsonl` is the CLI's own.

**`opencode` hangs and never returns.** It blocks when it inherits an open stdin. The
adapter always closes stdin; if you are invoking opencode yourself, add `< /dev/null`.

**A panelist stops early with a context-length error.** `compaction_threshold` is
above that model's context window. Lower it, or drop the model.

**Everyone says READY in round one.** `min_rounds` forces a second round precisely
because that usually means a shallow review. If it keeps happening, the task
description is probably too narrow to argue about.

## Development

```bash
pytest -q                                    # unit + end-to-end (no model calls)
python -m council run --task t.md --mock     # exercise the pipeline for free
```

Adding a harness means one file in `council/adapters/` implementing
`ask(prompt, cwd, timeout) -> Reply`, plus an entry in the registry.
