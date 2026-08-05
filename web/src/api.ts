import type {
  AgentThread,
  Catalog,
  CouncilEvent,
  Project,
  SessionRow,
  SessionState,
} from './types'

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
    ...init,
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      const payload = await response.json()
      if (payload && typeof payload.detail === 'string') detail = payload.detail
    } catch {
      /* the body was not JSON; the status line will have to do */
    }
    throw new ApiError(response.status, detail)
  }
  const type = response.headers.get('content-type') || ''
  return (type.includes('json') ? response.json() : response.text()) as Promise<T>
}

export const api = {
  catalog: () => request<Catalog>('/api/catalog'),
  projects: () => request<Project[]>('/api/projects'),
  sessions: (project?: string) =>
    request<SessionRow[]>(`/api/sessions${project ? `?project=${encodeURIComponent(project)}` : ''}`),
  session: (id: string) => request<SessionState>(`/api/sessions/${id}`),
  create: (payload: Record<string, unknown>) =>
    request<{ id: string; dir: string; mode: string }>('/api/sessions', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  remove: (id: string) => request<unknown>(`/api/sessions/${id}`, { method: 'DELETE' }),
  control: (id: string, action: string, payload: Record<string, unknown> = {}) =>
    request<{ action: string; detail: string }>(`/api/sessions/${id}/control`, {
      method: 'POST',
      body: JSON.stringify({ action, ...payload }),
    }),
  agent: (id: string, label: string) =>
    request<AgentThread>(`/api/sessions/${id}/agents/${label}`),
  plan: (id: string, label: string) => request<string>(`/api/sessions/${id}/plans/${label}`),
}

/**
 * Follow one session's event stream.
 *
 * `from_seq` is only the *initial* position — on an automatic reconnect the browser
 * sends Last-Event-ID by itself, so no event is replayed twice and none is skipped.
 * `onResync` fires when the server says a client fell behind; the only honest answer
 * is to refetch the snapshot rather than trust one with holes in it.
 */
export function streamSession(
  id: string,
  fromSeq: number,
  onEvent: (event: CouncilEvent) => void,
  onResync: () => void,
  onEnd: () => void,
): () => void {
  const source = new EventSource(`/api/sessions/${id}/events?from_seq=${fromSeq}`)
  source.addEventListener('council', (raw) => {
    try {
      onEvent(JSON.parse((raw as MessageEvent).data))
    } catch {
      /* a malformed frame is not worth tearing the stream down for */
    }
  })
  source.addEventListener('resync', () => onResync())
  source.addEventListener('end', () => {
    source.close()
    onEnd()
  })
  return () => source.close()
}

/** Session-list changes, so a run started from the CLI shows up here. */
export function streamSessions(onRows: (rows: SessionRow[]) => void): () => void {
  const source = new EventSource('/api/events')
  source.addEventListener('sessions', (raw) => {
    try {
      onRows(JSON.parse((raw as MessageEvent).data).sessions)
    } catch {
      /* ignore */
    }
  })
  return () => source.close()
}
