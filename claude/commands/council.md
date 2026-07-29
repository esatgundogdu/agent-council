---
description: Convene a multi-model panel to mature a plan before implementing it
argument-hint: <what you want to build, in your own words>
allowed-tools: Bash, Read, Write
---

Convene the plan council on the task below.

## The task

$ARGUMENTS

## What to do

### 1. Write the task down, verbatim

Create `.council/<YYYY-MM-DD_HHMMSS>/task.md` (timestamp = now) in the current
project, containing the text under "The task" above and **nothing else**.

This is the one rule that matters. Do not add a summary, a restatement, your reading
of what the user meant, a list of relevant files, the repo's structure, your opinion,
or a "context" preamble. The panel's value comes from several models exploring this
repository independently and disagreeing about it; anything you add ahead of them
collapses that into your framing. If the request is ambiguous, leave it ambiguous —
watching where the panelists diverge on an ambiguity is a result, not a defect.

If the user gave you no task text, ask for it and stop.

### 2. Choose the panel, if the user wants to

If the task text begins with an `--agents "..."` flag, strip it out before writing
`task.md` — it is an instruction to you, not part of the task — and pass it through
to the run command in step 4.

If the user instead asks *who* is on the panel, or asks to change it without naming
models, run `council models` and show them the options grouped by provider, then ask
which they want. Do not guess a panel on their behalf.

With no flag, the panel in `council.yaml` is used.

### 3. Open the dashboard

A session runs for tens of minutes, so give the user something to watch before you
start it. If nothing already answers on the port, start one:

```
curl -sf -o /dev/null http://127.0.0.1:8787/api/state || council serve --port 8787
```

Start it with `run_in_background: true` — it is a server and does not exit. It opens a
browser and follows the newest session, so it will pick this run up on its own. If the
port was already answering, a dashboard is already running; just point the user at
`http://127.0.0.1:8787/` rather than starting a second one.

Tell the user the URL in your first message about the run. If `council serve` fails
(missing install, no display), say so once and carry on — it is not required.

### 4. Run the council

```
python -m council run --task .council/<ts>/task.md --project-dir . [--agents "..."]
```

Run it with `run_in_background: true`. Expect **15–45 minutes**: the panelists are
reading the repository, not just answering.

The dashboard is for the user, not for you — you still report progress yourself, since
they may not be looking at the browser. Poll `.council/<ts>/status.json` (`state`,
`phase`, `round`, `elapsed`, `tokens`) and report each phase and round change as it
happens; do not sit silent for 40 minutes. Do not poll faster than every ~30s.

If the run exits non-zero, read `status.json` and the tail of `events.jsonl`, tell the
user what failed, and stop. Exit code 2 means too few panelists survived (usually a
CLI auth problem); exit code 3 means a configuration error. `events.jsonl` holds every
argument in full, so even a run that died before Phase 3 has its discussion on disk.

### 5. Read the digest — only the digest

Read `.council/<ts>/digest.md`. Do not open `transcript.md` or the individual plans
unless the user asks for them or the digest is unusable: the transcript is long, and
loading it costs you the context you need to do the synthesis well.

### 6. Synthesise the unified plan

From the digest, write one plan that a developer could execute:

- the design decisions the panel converged on, each with the reasoning that settled it;
- concrete implementation steps, files to touch, and a test strategy;
- a final section, **"Points the panel could not agree on"**, listing each unresolved
  item with the competing positions, ending with your recommendation and why.

Weigh arguments on their merits, not by how many panelists made them — the panel is
deliberately anonymous so that a good argument from one agent outranks a weak consensus.
Where the digest notes the discussion was cut short (round or budget limit), say so.

If you are in plan mode, present this through the normal plan-approval flow rather
than starting to implement.
