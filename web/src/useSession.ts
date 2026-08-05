import { useCallback, useEffect, useRef, useState } from 'react'

import { api, streamSession } from './api'
import { fold } from './reduce'
import type { SessionState } from './types'

/**
 * One session, kept current.
 *
 * The snapshot is fetched once and then folded forward from the event stream, so a
 * running debate updates without polling. Anything the fold cannot do confidently —
 * a finished session's digest, a control that changes the run's shape — asks for a
 * fresh snapshot instead of guessing.
 */
export function useSession(id: string | null) {
  const [state, setState] = useState<SessionState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const seq = useRef(0)
  const pending = useRef<number | null>(null)

  const load = useCallback(async () => {
    if (!id) return
    try {
      const fresh = await api.session(id)
      seq.current = fresh.seq
      setState(fresh)
      setError(null)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [id])

  // Refetches are coalesced: a burst of events that each want one should cost a
  // single request, not one per event.
  const refetchSoon = useCallback(() => {
    if (pending.current !== null) return
    pending.current = window.setTimeout(() => {
      pending.current = null
      void load()
    }, 250)
  }, [load])

  useEffect(() => {
    setState(null)
    seq.current = 0
    if (id) void load()
  }, [id, load])

  useEffect(() => {
    if (!id || !state) return undefined
    const close = streamSession(
      id,
      seq.current,
      (event) => {
        if (event.seq <= seq.current) return
        seq.current = event.seq
        setState((current) => {
          if (!current) return current
          const result = fold(current, event)
          if (result.refetch) refetchSoon()
          return result.state
        })
      },
      refetchSoon,
      refetchSoon,
    )
    return close
    // Re-subscribing on every state change would tear the stream down constantly;
    // the stream only needs to be re-established when the session itself changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, state !== null, refetchSoon])

  return { state, error, reload: load }
}
