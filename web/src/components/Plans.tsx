import { useEffect, useState } from 'react'

import { api } from '../api'
import { duration, tokens as fmtTokens } from '../format'
import { Markdown } from './Markdown'
import type { Mode, Panelist } from '../types'

/**
 * The independent plans, which is what phase 1 produces and the whole argument is
 * then about.
 *
 * They are written before any panelist has seen another's, so reading two side by
 * side is the clearest picture of where the models actually disagree — and during
 * phase 1, which can run for minutes, watching them land one at a time is the only
 * thing there is to watch.
 */
export function Plans({
  sessionId,
  panel,
  mode,
  running,
  defaultOpen,
}: {
  sessionId: string
  panel: Panelist[]
  mode: Mode
  running: boolean
  defaultOpen: boolean
}) {
  const withPlans = panel.filter((member) => member.has_plan)
  const [open, setOpen] = useState<string | null>(null)

  // While phase 1 runs, follow the newest plan in. Once the debate starts the user is
  // driving, so their choice of what is open must not be overwritten.
  useEffect(() => {
    if (!defaultOpen) return
    const last = withPlans[withPlans.length - 1]
    if (last) setOpen(last.label)
  }, [defaultOpen, withPlans.length])

  // `review` and `consult` skip phase 1 entirely — that is the whole point of them.
  // Showing the section there left three panelists "still writing…" a plan nobody ever
  // asked for. A finished session that produced none is the same lie for another reason.
  if (!panel.length || mode === 'review' || mode === 'consult') return null
  if (!running && !withPlans.length) return null

  return (
    <section className="plans">
      <div className="round-head" style={{ marginTop: 0 }}>
        Independent plans
        <span className="count">
          {withPlans.length} of {panel.length}
        </span>
      </div>

      <div className="plan-tabs">
        {panel.map((member) => (
          <button
            key={member.label}
            className={`plan-tab${open === member.label ? ' active' : ''}`}
            disabled={!member.has_plan}
            onClick={() => setOpen(open === member.label ? null : member.label)}
          >
            <span className="label">{member.label}</span>
            <span className="sub">
              {member.has_plan
                ? [
                    member.plan?.seconds ? duration(member.plan.seconds) : '',
                    member.plan?.tokens ? `${fmtTokens(member.plan.tokens)} tok` : '',
                  ]
                    .filter(Boolean)
                    .join(' · ')
                : member.dropped
                  ? 'dropped'
                  : running
                    ? 'still writing…'
                    : 'never wrote one'}
            </span>
          </button>
        ))}
      </div>

      {open && <PlanBody sessionId={sessionId} label={open} />}
    </section>
  )
}

function PlanBody({ sessionId, label }: { sessionId: string; label: string }) {
  const [text, setText] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setText(null)
    setError(null)
    api
      .plan(sessionId, label)
      .then((body) => !cancelled && setText(body))
      .catch((exc) => !cancelled && setError(exc instanceof Error ? exc.message : String(exc)))
    return () => {
      cancelled = true
    }
  }, [sessionId, label])

  if (error) return <div className="note warn">{error}</div>
  if (text === null) return <div className="loading">reading the plan…</div>
  return (
    <div className="plan-body">
      <Markdown text={text} />
    </div>
  )
}
