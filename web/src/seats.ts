import type { Panelist } from './types'

/**
 * A colour per seat, by position on the panel.
 *
 * Shared rather than local to the table, because the point of an identity colour is
 * that it is the *same* everywhere: the ring on the table and the rule down the side
 * of that panelist's turn in the transcript have to agree, or the code never pays off
 * and the table is just decoration.
 *
 * An array rather than the `.letter-A`…`.letter-F` classes the roster used: those ran
 * out at the sixth panelist and the seventh was silently colourless.
 *
 * None of these is the interaction accent. It used to be, and a panelist's ring in the
 * accent colour is ambiguous between "this is A" and "this is selected".
 */
export const SEAT_TINTS = [
  'var(--a)',
  'var(--b)',
  'var(--c)',
  'var(--d)',
  'var(--e)',
  'var(--f)',
]

export function tintAt(index: number): string {
  return SEAT_TINTS[index % SEAT_TINTS.length]
}

/** The tint for a panelist by label, or a neutral rule for the chair and for strangers. */
export function tintOf(panel: Panelist[], label: string): string {
  const index = panel.findIndex((p) => p.label === label)
  return index < 0 ? 'var(--rule-strong)' : tintAt(index)
}
