---
description: Put something in front of a multi-model panel — a task to plan, a plan to check, or a decision you are about to take
argument-hint: <what you want the panel to do, in your own words>
allowed-tools: Bash, Read, Write
---

Convene the council on the request below.

## The request

$ARGUMENTS

## What to do

### 1. Open the control plane

```
council up --open
```

Idempotent: if a daemon is already running this reuses it and prints the same URL. It is
detached, so it survives this turn and this session. Tell the user the URL in your first
message — the browser is where they watch, intervene, and read each panelist's own
history.

If this fails (not installed, no browser), say so once and carry on; `council start`
starts the daemon anyway.

### 2. Choose the mode

One question decides it: **what should the panel start from?**

| The user wants | Mode | Extra input |
|---|---|---|
| a plan, designed from nothing | `independent` (default) | — |
| your read on a decision in front of you *now* | `consult` | `--context` |
| a plan or diff they already have, checked | `review` | `--seed` |
| both — the panel plans first, then meets the proposal | `hybrid` | `--seed` |

Every mode then debates for `min_rounds`–`max_rounds` rounds and ends when everyone says
READY. The modes differ only in where the panel starts.

`consult` skips the planning phase and opens with all panelists answering **in parallel,
without seeing each other**; from round 2 it is an ordinary debate. That opening round is
what makes it cheap, and `--max-rounds 1` stops there — fast, but then nobody has heard
anybody and the digest says so.

`review` deliberately anchors the panel on the proposal's framing: right for **verifying**
a plan, wrong for **generating** one. If you just wrote the plan yourself and the user
wants it stress-tested, prefer `hybrid` unless they ask for speed.

Say which mode you chose and why, in one line. If the request is genuinely ambiguous
between two, ask.

### 3. Write the task down, verbatim

`.council-task.md`, containing the request text and **nothing else**.

No summary, no restatement, no list of relevant files, no "context" preamble — those go
in the brief (step 4), where they are labelled as yours. Several models exploring this
repository independently is the whole product; framing folded into the task is
indistinguishable from the task itself. If the request is ambiguous, leave it ambiguous:
where the panelists diverge on an ambiguity is a result, not a defect.

If the user gave you no request text, ask for it and stop.

### 4. Write the brief, if this session has anything worth passing

`.council-context.md`, and `--context .council-context.md`.

This is yours to write and it is a judgement call. **The tool guarantees the part that
matters: a panelist writing its independent plan never sees it.** It arrives when the
discussion starts, where the panel already has its own view and the brief is something to
argue with. So briefing is safe in every mode — what it costs is that the discussion
starts inside your framing rather than theirs.

Include, when they exist:

- **what has been decided, and why** — the reasoning, not just the conclusion;
- **what was tried and rejected**, and what it cost, so nobody proposes it again;
- **what you are still unsure about** — an uncertainty you hide is one the panel cannot
  help you with;
- **the files that matter**, by path;
- **the diff, pasted**, if the work is uncommitted and the panel needs to see it. Do this
  deliberately: Claude panelists run with `Bash` disallowed and cannot run `git diff`
  themselves, so whatever you do not paste, half the panel cannot see.

Leave out: the transcript of this conversation, anything the panel can read for itself,
and your argument for the answer you already prefer. You are briefing reviewers, not
prosecuting a case. Where you are reporting a belief rather than a fact, mark it — the
prompt tells them the brief may be wrong, which is only useful if you have not written it
as though it were settled.

Skip the brief entirely when there is nothing to say: a fresh `/council` on a new task
with no prior conversation does not need one, and an empty one is worse than none.

### 5. Pass the proposal, if there is a concrete artefact to judge

A drafted plan, a proposed diff, a design doc — anything whose job is to be **judged**
rather than **known** — goes in its own file and is passed as `--seed`. Required by
`review` and `hybrid`, optional for `consult`, refused by `independent`.

The brief is the situation; the seed is the artefact. Keeping them apart is what lets the
digest say which one the panel actually objected to. `--seed -` reads stdin, so you can
pipe your own output.

### 6. Choose the panel, if the user wants to

If the request begins with an `--agents "..."` flag, strip it out before writing the task
file — it is an instruction to you, not part of the task — and pass it through.

If the user asks *who* is on the panel, or asks to change it without naming models, run
`council models` and show them the options grouped by provider, then ask. Do not guess a
panel on their behalf. With no flag, the panel in `council.yaml` is used.

### 7. Convene

```
council start --task .council-task.md --project-dir . [--mode consult] [--context .council-context.md] [--seed plan.md] [--agents "..."] [--max-rounds N] --json
```

Timing follows the round count, not the mode:

- `--max-rounds 1` — **3–8 minutes.** One parallel round. Add `--follow` and report when
  it lands; do not poll as well.
- the default 2–5 rounds — **15–45 minutes.** Return immediately and poll:

```
council status <id> --json
```

Report each phase and round change as it happens; do not sit silent for 40 minutes, and
do not poll faster than every ~30s. `council watch <id>` streams the same lines if you
would rather block.

Add `--mock` to rehearse the whole pipeline with scripted panelists — no model is called
and nothing is spent.

### 8. Steer it, if the user asks

You have exactly what the browser has, and it works from any session, not only the one
that started the council:

```
council sessions                              # what exists, newest first
council control pause <id>
council control resume <id>
council control chair <id> --text "..."       # a message to the whole panel
council control skip <id> --agent Agent-C     # sit this round out
council control drop <id> --agent Agent-C     # leave the panel
council control restore <id> --agent Agent-C  # bring it back
council control extend <id> --max-rounds 8
council control digest <id>                   # stop early, write the digest now
council control stop <id>                     # graceful; still writes the digest
council down                                  # stop the daemon itself
```

A chair message is an intervention from outside the panel: it reaches everyone and is
marked as a direction, not an opinion. Use it when the user wants the discussion pointed
somewhere, and say you are doing it.

If the run fails, `council status <id>` says why. Exit code 2 means too few panelists
survived (usually a CLI auth problem); exit code 3 means a configuration error. Every
argument is in `events.jsonl` in full, so even a run that died before Phase 3 has its
discussion on disk.

### 9. Read the digest — only the digest

```
council digest <id>
```

Do not open the transcript or the individual plans unless the user asks or the digest is
unusable: the transcript is long, and loading it costs you the context you need to do the
synthesis well.

### 10. Report, and synthesise

From the digest, write one plan a developer could execute:

- the design decisions the panel converged on, each with the reasoning that settled it;
- concrete implementation steps, files to touch, and a test strategy;
- a final section, **"Points the panel could not agree on"**, listing each unresolved item
  with the competing positions, ending with your recommendation and why.

For a one-round consultation there is no unified plan to write — report each panelist's
answer in a sentence or two, then the same unresolved section.

Rules that hold either way:

- **Every `CONTINUE` is a decision for the user.** A panelist that found a blocker found
  it against this repository. Do not resolve it yourself and do not average it away
  against three READYs. Say what it is, say whether you think it holds, and let the user
  choose.
- **Weigh arguments on their merits, not by how many panelists made them.** The panel is
  anonymous precisely so a good argument from one agent outranks a weak consensus.
- **Pass on what the digest says about itself.** If the discussion was cut short (round,
  token or time limit, or the user stopped it), the positions are not final. If the panel
  started from a supplied proposal or a brief, it was working inside that framing. If it
  held one round, nobody heard anybody — agreement there is coincidence, not consensus.

If you are in plan mode, present this through the normal plan-approval flow rather than
starting to implement.

---

To synthesise a session that finished somewhere else — the browser, a terminal, an
earlier Claude Code session — use `/council-apply` instead; the debate has already
happened and only the synthesis is left.
