import { useEffect, useState } from 'react'

import { api } from '../api'
import { duration, tokens as fmtTokens } from '../format'
import type { Catalog, Defaults, Harness, Mode, PanelMember, Project } from '../types'

interface Member {
  name: string
  adapter: string
  model: string
  /** '' means "leave the harness on its own default". */
  effort: string
}

//: Both codex and the claude CLI accept exactly these five and reject anything else,
//: so one list covers the panel. opencode has no such control.
const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max']
const EFFORT_ADAPTERS = new Set(['codex_cli', 'claude_cli', 'mock'])

const MODE_COPY: Record<Mode, { title: string; blurb: string; warn?: string }> = {
  consult: {
    title: 'Start from the brief',
    blurb:
      'No planning phase. Every panelist reads the repository and gives its own answer to the question, in parallel, and then they argue it out as usual. The cheapest way in, because nobody writes a plan first.',
    warn:
      'Set the round ceiling to 1 and this becomes a quick opinion poll — fast, but nobody hears anyone else, so agreement is coincidence and disagreement stays unresolved. Two rounds or more and it is an ordinary debate that simply skipped the plans.',
  },
  independent: {
    title: 'Plan from scratch',
    blurb:
      'Every panelist explores the repository and writes its own plan before seeing anyone else’s. Nobody is anchored.',
  },
  review: {
    title: 'Review a proposal',
    blurb:
      'Skip the planning phase. The panel goes straight to critiquing the plan below — faster, and pointed at one target.',
    warn:
      'This deliberately anchors the panel on the proposal’s framing. It is the right mode for verifying a plan, and the wrong one for generating one.',
  },
  hybrid: {
    title: 'Both',
    blurb:
      'The panel writes independent plans first, and only then meets the proposal. Slower, but it cannot be anchored before it has its own view.',
  },
}

/**
 * Convening a council.
 *
 * Two questions are always on screen — which repository, and what the task is — and
 * everything else is a summary line you can open. The form used to ask for eight
 * protocol numbers, three checkboxes and a panel editor before you could type a word,
 * which is a configuration screen wearing the clothes of a first step. The numbers
 * still exist; they now come from council.yaml, are shown as the sentence they add up
 * to, and are only transmitted when you actually change one.
 */
export function NewCouncil({ onStarted }: { onStarted: (id: string) => void }) {
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [projects, setProjects] = useState<Project[]>([])
  const [projectDir, setProjectDir] = useState('')
  const [mode, setMode] = useState<Mode>('independent')
  const [task, setTask] = useState('')
  const [seed, setSeed] = useState('')
  const [context, setContext] = useState('')
  const [contextOpen, setContextOpen] = useState(false)
  const [panelOpen, setPanelOpen] = useState(false)
  const [limitsOpen, setLimitsOpen] = useState(false)
  const [members, setMembers] = useState<Member[] | null>(null)
  const [limits, setLimits] = useState<Record<string, number> | null>(null)
  const [mockPanel, setMockPanel] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    void api.catalog().then(setCatalog).catch(() => undefined)
    void api
      .projects()
      .then((rows) => {
        setProjects(rows)
        if (rows.length) setProjectDir((current) => current || rows[0].dir)
      })
      .catch(() => undefined)
  }, [])

  // In a consultation the brief is what the panel opens on — the only thing it is told
  // beyond the question, and there are no plans to fall back on. Opening the field is a
  // nudge, not a lock: shutting it again stays shut.
  useEffect(() => {
    if (mode === 'consult') setContextOpen(true)
  }, [mode])

  const defaults: Defaults | undefined = catalog?.defaults
  const seeded = mode === 'review' || mode === 'hybrid'
  //: `consult` reads a proposal if there is one and does something sensible without.
  const takesSeed = seeded || mode === 'consult'
  //: null means "as council.yaml has it" — the form only owns what you have edited.
  const panel: PanelMember[] = mockPanel
    ? []
    : members
      ? members.map((m) => ({
          name: m.name,
          adapter: m.adapter,
          model: m.model || null,
          effort: (m.effort || null) as PanelMember['effort'],
        }))
      : (defaults?.panel ?? [])

  //: Harnesses this council must find on PATH. One that names its own binary is
  //: excluded — the catalogue already checked that file.
  const needed = new Set(panel.filter((m) => !m.binary).map((m) => m.adapter))
  const missing = (catalog?.harnesses ?? []).filter(
    (h) => !h.available && needed.has(h.adapter),
  )


  const effectiveLimits = limits ?? (defaults && flatten(defaults))

  async function submit() {
    setError(null)
    if (!projectDir.trim()) return setError('Choose the repository the panel will read.')
    if (!task.trim()) return setError('The task cannot be empty — it is all the panel gets.')
    if (seeded && !seed.trim()) return setError(`“${MODE_COPY[mode].title}” needs a proposal.`)

    const payload: Record<string, unknown> = {
      project_dir: projectDir.trim(),
      register_project: true,
      mode,
      task,
    }
    if (takesSeed && seed.trim()) payload.seed = seed
    if (context.trim()) payload.context = context
    if (mockPanel) {
      payload.panel = [
        { name: 'alpha', adapter: 'mock' },
        { name: 'beta', adapter: 'mock' },
        { name: 'gamma', adapter: 'mock' },
      ]
    } else if (members) {
      payload.panel = members.map((m) => ({
        name: m.name.trim(),
        adapter: m.adapter,
        model: m.model.trim() || null,
        effort: m.effort || null,
      }))
    }
    // Only when edited: otherwise council.yaml stays the single owner of these, and
    // changing the file changes what the UI does too.
    if (limits) {
      payload.protocol = {
        min_rounds: limits.min_rounds,
        max_rounds: limits.max_rounds,
        token_budget: limits.token_budget,
        wall_clock_budget: limits.wall_clock_budget,
      }
      payload.timeouts = {
        per_call: limits.per_call,
        per_call_phase1: limits.per_call_phase1,
      }
    }

    setBusy(true)
    try {
      const created = await api.create(payload)
      onStarted(created.id)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page form">
      <div className="page-head">
        <h2>New council</h2>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="field">
        <div className="label">Repository the panel will read</div>
        <div className="row">
          <input
            value={projectDir}
            onChange={(e) => setProjectDir(e.target.value)}
            placeholder="C:\path\to\your\repo"
            spellCheck={false}
          />
          {projects.length > 0 && (
            <select
              style={{ width: '13rem', flex: 'none' }}
              value=""
              onChange={(e) => e.target.value && setProjectDir(e.target.value)}
            >
              <option value="">recent…</option>
              {projects.map((p) => (
                <option key={p.dir} value={p.dir}>
                  {p.name} ({p.sessions})
                </option>
              ))}
            </select>
          )}
        </div>
        <div className="hint">
          Every panelist reads this directory through its own provider, read-only.
        </div>
      </div>

      <div className="field">
        <div className="label">The task, in your own words</div>
        <textarea
          value={task}
          onChange={(e) => setTask(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !busy) void submit()
          }}
          rows={8}
          autoFocus
          placeholder="I want to add rate limiting to the public API…"
        />
        <div className="hint">
          This reaches every panelist byte-for-byte. If it is ambiguous, leave it
          ambiguous: where the panelists diverge on an ambiguity is a result, not a defect.
        </div>
      </div>

      <div className="field">
        <div className="label">How the panel starts</div>
        <div className="grid-4">
          {(Object.keys(MODE_COPY) as Mode[]).map((key) => (
            <button
              key={key}
              className={`choice${mode === key ? ' active' : ''}`}
              aria-pressed={mode === key}
              onClick={() => setMode(key)}
            >
              <b>{MODE_COPY[key].title}</b>
              <span>{key}</span>
            </button>
          ))}
        </div>
        <div className="hint">{MODE_COPY[mode].blurb}</div>
        {MODE_COPY[mode].warn && (
          <div className="note warn" style={{ marginTop: '0.6rem' }}>
            {MODE_COPY[mode].warn}
          </div>
        )}
      </div>

      {takesSeed && (
        <div className="field">
          <div className="label">
            The proposal to put in front of the panel
            {seeded ? '' : ' (optional)'}
          </div>
          <textarea
            value={seed}
            onChange={(e) => setSeed(e.target.value)}
            rows={10}
            placeholder="Paste the plan your main agent produced…"
          />
          <div className="hint">
            {seeded
              ? 'Typically the plan your main agent just wrote. Markdown is fine.'
              : 'Leave it empty and the panel answers the question from the repository ' +
                'instead. Fill it in and each panelist critiques it, once.'}
          </div>
        </div>
      )}

      {/* Optional in every mode, and deliberately not free of consequence — which is
          why the summary line says how big it is even when the editor is shut. */}
      <Disclosure
        label="Context"
        summary={describeContext(context, mode)}
        open={contextOpen}
        onOpen={() => setContextOpen(true)}
        onClose={() => setContextOpen(false)}
        onReset={context ? () => setContext('') : undefined}
      >
        <textarea
          value={context}
          onChange={(e) => setContext(e.target.value)}
          rows={8}
          placeholder="What has already been decided, what was tried and rejected, what is still uncertain…"
        />
        <div className="hint">
          Where the work already stands, in your words or your agent's. Panelists writing
          an independent plan never see this — it arrives when the discussion starts, so
          it can be argued with rather than assumed.
        </div>
      </Disclosure>

      {/* `members`/`limits` are null until edited, so council.yaml stays the owner.
          The editors are shown the effective values either way — a reset while one is
          open must put the defaults back on screen, not blank it. */}
      <Disclosure
        label="Panel"
        summary={describePanel(panel, mockPanel, catalog)}
        open={panelOpen}
        onOpen={() => setPanelOpen(true)}
        onClose={() => setPanelOpen(false)}
        onReset={members ? () => setMembers(null) : undefined}
      >
        <PanelEditor
          members={members ?? toMembers(defaults?.panel ?? [])}
          catalog={catalog}
          onChange={setMembers}
        />
      </Disclosure>

      <Disclosure
        label="Limits"
        summary={describeLimits(effectiveLimits)}
        open={limitsOpen}
        onOpen={() => setLimitsOpen(true)}
        onClose={() => setLimitsOpen(false)}
        onReset={limits ? () => setLimits(null) : undefined}
      >
        <LimitsEditor values={effectiveLimits} onChange={setLimits} />
      </Disclosure>

      {missing.length > 0 && (
        <div className="note warn">
          {missing.map((h) => (
            <div key={h.adapter}>
              <code>{h.binary}</code> is not on this PATH — the panelists using it will be
              dropped on their first turn.
            </div>
          ))}
        </div>
      )}

      <div className="submit">
        <button className="primary" onClick={submit} disabled={busy}>
          {busy ? 'convening…' : 'Convene the council'}
        </button>
        <label className="mock-toggle">
          <input
            type="checkbox"
            checked={mockPanel}
            onChange={(e) => setMockPanel(e.target.checked)}
          />
          mock panel — scripted replies, no model is called and nothing is spent
        </label>
      </div>
    </div>
  )
}

/** A setting that is a sentence until you want to change it. */
function Disclosure({
  label,
  summary,
  open,
  onOpen,
  onClose,
  onReset,
  children,
}: {
  label: string
  summary: string
  open: boolean
  onOpen: () => void
  onClose: () => void
  onReset?: () => void
  children: React.ReactNode
}) {
  return (
    <div className={`disclosure${open ? ' open' : ''}`}>
      <div className="summary">
        <span className="k">{label}</span>
        <span className="v">{summary}</span>
        {onReset && (
          <button className="ghost tiny" onClick={onReset} title="Back to council.yaml">
            reset
          </button>
        )}
        <button className="ghost tiny" onClick={open ? onClose : onOpen}>
          {open ? 'done' : 'change'}
        </button>
      </div>
      {open && <div className="body">{children}</div>}
    </div>
  )
}

function PanelEditor({
  members,
  catalog,
  onChange,
}: {
  members: Member[]
  catalog: Catalog | null
  onChange: (members: Member[]) => void
}) {
  const set = (i: number, change: Partial<Member>) =>
    onChange(members.map((m, n) => (n === i ? { ...m, ...change } : m)))

  return (
    <>
      {members.map((member, i) => (
        <div className="row" key={i}>
          <select
            style={{ width: '12rem', flex: 'none' }}
            value={member.adapter}
            onChange={(e) => set(i, { adapter: e.target.value })}
          >
            {(catalog?.harnesses ?? []).map((h) => (
              <option key={h.adapter} value={h.adapter}>
                {h.label}
                {h.available ? '' : ' — not installed'}
              </option>
            ))}
          </select>
          <input
            value={member.name}
            onChange={(e) => set(i, { name: e.target.value })}
            placeholder="name"
            style={{ width: '8rem', flex: 'none' }}
          />
          <ModelField
            harness={catalog?.harnesses.find((h) => h.adapter === member.adapter)}
            value={member.model}
            onChange={(model) => set(i, { model })}
          />
          {/* Disabled rather than hidden for a harness that cannot think harder: a
              control that vanishes reads as a bug, one that is greyed out explains
              itself. */}
          <select
            style={{ width: '7.5rem', flex: 'none' }}
            value={member.effort}
            disabled={!EFFORT_ADAPTERS.has(member.adapter)}
            title={
              EFFORT_ADAPTERS.has(member.adapter)
                ? 'How hard this panelist is told to think'
                : 'This harness has no reasoning-effort control'
            }
            onChange={(e) => set(i, { effort: e.target.value })}
          >
            <option value="">effort: default</option>
            {EFFORTS.map((level) => (
              <option key={level} value={level}>
                {level}
              </option>
            ))}
          </select>
          <button
            className="ghost"
            disabled={members.length <= 2}
            title={members.length <= 2 ? 'A council needs at least two' : 'Remove'}
            onClick={() => onChange(members.filter((_, n) => n !== i))}
          >
            ✕
          </button>
        </div>
      ))}
      <button
        className="ghost"
        onClick={() =>
          onChange([
            ...members,
            {
              name: `panelist-${members.length + 1}`,
              adapter: 'codex_cli',
              model: '',
              effort: '',
            },
          ])
        }
      >
        + panelist
      </button>
      <div className="hint">
        Each provider on this list sees your code. Two panelists is the minimum; three or
        four is where the disagreement gets useful.
      </div>
    </>
  )
}

const LIMIT_FIELDS: { key: string; label: string; hint: string; step: number }[] = [
  { key: 'min_rounds', label: 'fewest rounds', hint: 'a first-round consensus is usually a shallow one', step: 1 },
  { key: 'max_rounds', label: 'most rounds', hint: 'stop arguing after this many — set it to 1 and nobody hears anybody', step: 1 },
  { key: 'token_budget', label: 'token budget', hint: 'dominated by repo exploration, not the transcript — size it in millions', step: 500_000 },
  { key: 'wall_clock_budget', label: 'time budget (s)', hint: 'the whole session', step: 300 },
  { key: 'per_call_phase1', label: 'read timeout (s)', hint: 'one panelist reading the repository — its plan, or its one consult answer', step: 60 },
  { key: 'per_call', label: 'turn timeout (s)', hint: 'one discussion turn', step: 30 },
]

function LimitsEditor({
  values,
  onChange,
}: {
  values: Record<string, number> | undefined
  onChange: (values: Record<string, number>) => void
}) {
  if (!values) return <div className="loading">reading council.yaml…</div>
  return (
    <div className="grid-3">
      {LIMIT_FIELDS.map((field) => (
        <div className="field" key={field.key}>
          <div className="label">{field.label}</div>
          <input
            type="number"
            min={1}
            step={field.step}
            value={values[field.key] ?? 0}
            onChange={(e) =>
              onChange({
                ...values,
                [field.key]: Math.max(1, parseInt(e.target.value, 10) || 1),
              })
            }
          />
          <div className="hint">{field.hint}</div>
        </div>
      ))}
    </div>
  )
}

/**
 * The model box, backed by whatever the harness can actually tell us.
 *
 * codex and opencode enumerate, so their real catalogue drops down. The claude CLI
 * cannot, so its list is the family aliases — each meaning "the current one" — and
 * typing a pinned id like `claude-opus-5` stays possible.
 */
function ModelField({
  harness,
  value,
  onChange,
}: {
  harness: Harness | undefined
  value: string
  onChange: (value: string) => void
}) {
  const listId = `models-${harness?.adapter ?? 'none'}`
  const placeholder = harness?.default_model
    ? `${harness.default_model} (CLI default)`
    : harness?.enumerable
      ? 'CLI default — or pick one'
      : 'CLI default — or an alias'

  return (
    <>
      <input
        value={value}
        list={listId}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        spellCheck={false}
        title={
          harness && !harness.enumerable
            ? 'The claude CLI cannot list its models. These aliases always mean the ' +
              'newest model in that family; a full id such as claude-opus-5 also works.'
            : undefined
        }
      />
      <datalist id={listId}>
        {(harness?.models ?? []).map((model) => (
          <option key={model.id} value={model.id}>
            {model.note ? `${model.label} — ${model.note}` : model.label}
          </option>
        ))}
      </datalist>
    </>
  )
}

// ---- summaries ------------------------------------------------------------

function describePanel(
  panel: PanelMember[],
  mockPanel: boolean,
  catalog: Catalog | null,
): string {
  if (mockPanel) return 'three scripted panelists — a rehearsal, nothing is spent'
  if (!panel.length) return 'reading council.yaml…'
  const byHarness = new Map<string, number>()
  for (const member of panel) {
    byHarness.set(member.adapter, (byHarness.get(member.adapter) ?? 0) + 1)
  }
  const parts = [...byHarness].map(([adapter, count]) => {
    const harness = catalog?.harnesses.find((h) => h.adapter === adapter)
    // "ChatGPT — codex" carries its own dash; inside a summary that already uses one
    // it reads as a list that lost its commas. The provider name alone is the point.
    const label = harness ? harness.label.split('—')[0].trim() : adapter
    return count > 1 ? `${count} × ${label}` : label
  })
  // Worth surfacing without opening the editor: a panelist dialled to `max` costs
  // noticeably more than the same panelist on its default, and nothing else on screen
  // would say so.
  const tuned = panel.filter((m) => m.effort)
  const effort = tuned.length
    ? ` · effort set on ${tuned.length} of ${panel.length}`
    : ''
  return `${panel.length} panelists — ${parts.join(', ')}${effort}`
}

function describeContext(context: string, mode: Mode): string {
  const text = context.trim()
  if (!text) {
    return mode === 'consult'
      ? 'nothing yet — a consultation with no brief is four cold opinions'
      : 'none — the panel starts from the repository alone'
  }
  const words = text.split(/\s+/).length
  return mode === 'independent' || mode === 'hybrid'
    ? `${words} words — withheld until the discussion starts`
    : `${words} words — the panel has this from the first word`
}

function describeLimits(values: Record<string, number> | undefined): string {
  if (!values) return 'reading council.yaml…'
  return [
    values.max_rounds === 1
      ? 'one round — no cross-review'
      : `${values.min_rounds}–${values.max_rounds} rounds`,
    `${fmtTokens(values.token_budget)} tokens`,
    round(values.wall_clock_budget),
  ].join(' · ')
}

/** A budget is a round number; "30m 00s" spends precision nobody asked for. */
function round(seconds: number): string {
  if (seconds % 3600 === 0) return `${seconds / 3600}h`
  if (seconds % 60 === 0) return `${seconds / 60}m`
  return duration(seconds)
}

function flatten(defaults: Defaults): Record<string, number> {
  return {
    min_rounds: defaults.protocol.min_rounds,
    max_rounds: defaults.protocol.max_rounds,
    token_budget: defaults.protocol.token_budget,
    wall_clock_budget: defaults.protocol.wall_clock_budget,
    per_call: defaults.timeouts.per_call,
    per_call_phase1: defaults.timeouts.per_call_phase1,
  }
}

function toMembers(panel: PanelMember[]): Member[] {
  const members = panel.map((p) => ({
    name: p.name,
    adapter: p.adapter,
    model: p.model ?? '',
    effort: p.effort ?? '',
  }))
  return members.length >= 2
    ? members
    : [
        { name: 'gpt', adapter: 'codex_cli', model: '', effort: '' },
        { name: 'claude', adapter: 'claude_cli', model: 'opus', effort: '' },
      ]
}
