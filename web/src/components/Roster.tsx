import { tokens as fmtTokens } from '../format'
import type { Panelist } from '../types'

/**
 * Who is on the panel, and — while a session runs — who is speaking right now and
 * what they are doing. The activity line is the panelist's own harness reporting its
 * tool calls, so "exploring · read council/panel.py" is literally what it just did.
 *
 * The whole card opens that panelist's history; skip and drop are separate buttons
 * because they change the session, and a click meant for "show me" must never be one.
 */
export function Roster({
  panel,
  running,
  onDrop,
  onSkip,
  onRestore,
  onInspect,
}: {
  panel: Panelist[]
  running: boolean
  onDrop: (label: string) => void
  onSkip: (label: string) => void
  onRestore: (label: string) => void
  onInspect: (label: string) => void
}) {
  if (!panel.length) return null
  return (
    <div className="roster">
      {panel.map((member) => (
        <div
          key={member.label}
          className={[
            'panelist',
            `letter-${member.label.slice(-1)}`,
            member.speaking ? 'speaking' : '',
            member.dropped ? 'dropped' : '',
          ]
            .filter(Boolean)
            .join(' ')}
        >
          <button
            className="face"
            onClick={() => onInspect(member.label)}
            title={`Everything ${member.label} was sent and replied`}
          >
            <div className="who">
              <span className="label">{member.label}</span>
              {member.name && (
                <span className="real">
                  {member.name}
                  {member.model ? ` · ${member.model}` : ''}
                  {/* Only when it was set: "effort: default" on every row would be
                      noise, and the absence already means the harness default. */}
                  {member.effort ? ` · ${member.effort} effort` : ''}
                </span>
              )}
            </div>

            <div className={`state verdict-${member.verdict ?? 'none'}`}>
              {member.dropped
                ? 'dropped'
                : member.speaking
                  ? 'speaking…'
                  : (member.verdict ?? 'not spoken yet')}
            </div>

            <div className="activity">
              {member.dropped && member.drop_reason
                ? member.drop_reason
                : member.activity
                  ? [
                      member.activity.state,
                      member.activity.tool,
                      member.activity.target && trim(member.activity.target),
                    ]
                      .filter(Boolean)
                      .join(' · ')
                  : member.reason || ' '}
            </div>

            <div className="foot">
              <span>{member.has_plan ? 'plan ✓' : 'no plan'}</span>
              <span>{fmtTokens(member.tokens)} tok</span>
              <span className="spacer" />
              <span className="go">history →</span>
            </div>
          </button>

          {running && (
            <div className="acts">
              {member.dropped ? (
                <button className="ghost" onClick={() => onRestore(member.label)}>
                  restore
                </button>
              ) : (
                <>
                  <button
                    className="ghost"
                    title="Sit out the next round, then rejoin"
                    onClick={() => onSkip(member.label)}
                  >
                    skip
                  </button>
                  <button
                    className="ghost"
                    title="Remove from the panel for the rest of the session"
                    onClick={() => onDrop(member.label)}
                  >
                    drop
                  </button>
                </>
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

function trim(target: string): string {
  return target.length > 46 ? `…${target.slice(-45)}` : target
}
