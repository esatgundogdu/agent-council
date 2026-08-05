---
description: Turn a finished council session into a unified plan
argument-hint: <session id, or nothing for the newest>
allowed-tools: Bash, Read
---

Turn the council session below into one plan.

## Session

$ARGUMENTS

## What to do

This is the other half of `/council`, for a session that was started somewhere else —
from the browser, from a terminal, or in an earlier Claude Code session. The debate has
already happened; your job is only the synthesis.

### 1. Find it

```
council digest $ARGUMENTS
```

With no argument that is the newest session. If it reports that the digest does not
exist yet, the session has not finished — run `council status $ARGUMENTS` and tell the
user where it has got to, then stop.

### 2. Read the digest — only the digest

Do not open `transcript.md` or the individual plans unless the user asks or the digest
is unusable. The transcript is long, and loading it costs you the context you need to
do the synthesis well.

### 3. Synthesise

Write one plan that a developer could execute:

- the design decisions the panel converged on, each with the reasoning that settled it;
- concrete implementation steps, files to touch, and a test strategy;
- a final section, **"Points the panel could not agree on"**, listing each unresolved
  item with the competing positions, ending with your recommendation and why.

Weigh arguments on their merits, not by how many panelists made them — the panel is
anonymous precisely so that a good argument from one agent outranks a weak consensus.

Two things in the digest change how you should read it, and you must pass them on:

- **the discussion was cut short** (a round, token or time limit, or the user stopped
  it) — the positions in it are not final;
- **the panel started from a supplied proposal** (`review` or `hybrid` mode) — the
  panel was arguing inside that proposal's framing, not from a blank page.

If you are in plan mode, present this through the normal plan-approval flow rather than
starting to implement.
