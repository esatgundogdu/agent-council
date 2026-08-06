import { tintAt } from '../seats'
import type { SessionState } from '../types'

/**
 * Who is in this conversation, along the top of it.
 *
 * The round table by another name: the same four facts — who is here, who is speaking,
 * where each one stands, and who convened it — laid out the way a group chat puts its
 * members in the header, in about ninety pixels instead of two hundred and ninety.
 *
 * A chat needs its vertical space for the chat. The drawing was answering "who has the
 * floor", which matters for the twenty minutes a council runs, on a screen that is
 * mostly read afterwards.
 */
export function Participants({
  state,
  onInspect,
  onChair,
}: {
  state: SessionState
  onInspect: (label: string) => void
  onChair: () => void
}) {
  const { panel, session } = state

  return (
    <div className="participants">
      <button className="who chair" onClick={onChair} title="The task and the brief this council was given">
        <span className="avatar">{session.convened_by === 'agent' ? 'MA' : 'You'}</span>
        <span className="name">{session.convened_by === 'agent' ? 'main agent' : 'you'}</span>
        <span className="state">set the task</span>
      </button>

      <span className="rule" />

      {panel.map((member, i) => {
        const tint = tintAt(i)
        return (
          <button
            key={member.label}
            className={[
              'who',
              member.speaking ? 'speaking' : '',
              member.dropped ? 'dropped' : '',
            ]
              .filter(Boolean)
              .join(' ')}
            style={{ color: tint }}
            onClick={() => onInspect(member.label)}
            title={`${member.label} — everything it was sent, said and printed`}
            aria-label={`${member.label}, ${member.name ?? 'identity withheld'}. ${status(member)}.`}
          >
            <span className="avatar" style={{ borderColor: tint }}>
              {member.label.slice(-1)}
            </span>
            <span className="name">{member.name ?? member.label}</span>
            <span className={`state ${tone(member)}`}>{status(member)}</span>
          </button>
        )
      })}
    </div>
  )
}

function status(member: SessionState['panel'][number]): string {
  if (member.dropped) return 'dropped'
  if (member.speaking) return 'typing…'
  if (member.verdict) return member.verdict === 'READY' ? 'ready' : 'still open'
  if (member.has_plan) return 'plan written'
  return 'waiting'
}

function tone(member: SessionState['panel'][number]): string {
  if (member.dropped) return 'gone'
  if (member.speaking) return 'now'
  if (member.verdict === 'READY') return 'ready'
  if (member.verdict) return 'open'
  return ''
}
