import { useEffect, useState } from 'react'

import { api } from '../api'
import { tokens as fmtTokens } from '../format'
import { tintAt } from '../seats'
import { Markdown } from './Markdown'
import type { Mode, Panelist } from '../types'

/**
 * Phase 1: what each panelist wrote before anyone had spoken.
 *
 * Its own tab, and one panelist at a time. These are the independent plans — the whole
 * reason the tool exists — and they were reachable only by opening a seat and finding
 * a third-level tab inside a drawer.
 */
export function Plans({
  sessionId,
  panel,
  mode,
}: {
  sessionId: string
  panel: Panelist[]
  mode: Mode
}) {
  const withPlans = panel.filter((p) => p.has_plan)
  const [label, setLabel] = useState<string | null>(null)
  const current = label && withPlans.some((p) => p.label === label) ? label : withPlans[0]?.label

  if (mode === 'consult' || mode === 'review') {
    return (
      <div className="note">
        This council had no planning phase. <code>{mode}</code> starts the panel from
        something it was given rather than from plans of its own — only{' '}
        <code>independent</code> and <code>hybrid</code> open with one.
      </div>
    )
  }

  if (!withPlans.length) {
    return (
      <div className="note">
        No plans yet. Every panelist is still reading the repository; each one appears
        here the moment it lands, and the discussion does not begin until they all have.
      </div>
    )
  }

  return (
    <>
      <div className="plan-picker">
        {withPlans.map((member) => {
          const tint = tintAt(panel.findIndex((p) => p.label === member.label))
          const active = member.label === current
          return (
            <button
              key={member.label}
              className={`plan-tab${active ? ' active' : ''}`}
              style={active ? { borderBottomColor: tint } : undefined}
              onClick={() => setLabel(member.label)}
            >
              <span className="avatar" style={{ color: tint, borderColor: tint }}>
                {member.label.slice(-1)}
              </span>
              <span className="who">{member.name ?? member.label}</span>
              <span className="sub">
                {member.plan?.chars ? `${Math.round(member.plan.chars / 100) / 10}k chars` : ''}
                {member.plan?.tokens ? ` · ${fmtTokens(member.plan.tokens)} tok` : ''}
              </span>
            </button>
          )
        })}
      </div>
      {current && <PlanBody sessionId={sessionId} label={current} />}
    </>
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

  if (error) return <div className="error-banner">{error}</div>
  if (!text) return <div className="loading">loading…</div>
  return (
    <div className="plan-body">
      <Markdown text={text} />
    </div>
  )
}
