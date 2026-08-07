import type {
  AgentThread,
  Catalog,
  CouncilEvent,
  Project,
  SessionRow,
  SessionState,
} from './types'

export class ApiError extends Error {
  /** `detail` is whatever the server put in the body. Most errors say what went wrong
      in a sentence; a few — the refusal to shut down over a running council — answer
      with the facts the UI has to show, and those would be lost as a status line. */
  constructor(public status: number, message: string, public detail?: unknown) {
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
    let body: unknown
    try {
      const payload = await response.json()
      body = payload?.detail
      if (typeof body === 'string') detail = body
    } catch {
      /* the body was not JSON; the status line will have to do */
    }
    throw new ApiError(response.status, detail, body)
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
  /** Stop the daemon. Refuses with 409 and the list while a council is running, unless
      `force` — so the first click can never be the one that throws work away. */
  shutdown: (force = false) =>
    request<{ stopping: boolean; cancelled: { id: string; task: string }[] }>(
      '/api/shutdown',
      { method: 'POST', body: JSON.stringify({ force }) },
    ),
  control: (id: string, action: string, payload: Record<string, unknown> = {}) =>
    request<{ action: string; detail: string }>(`/api/sessions/${id}/control`, {
      method: 'POST',
      body: JSON.stringify({ action, ...payload }),
    }),
  agent: (id: string, label: string) =>
    request<AgentThread>(`/api/sessions/${id}/agents/${label}`),
  plan: (id: string, label: string) => request<string>(`/api/sessions/${id}/plans/${label}`),
  /**
   * One harness process's raw output, from `offset` bytes in.
   *
   * `panel[].calls` supplies the name. The offset is what makes tailing a live log
   * cheap: without it every poll re-reads the whole file, which for a capped 2 MiB
   * log is megabytes a second for nothing.
   */
  call: (id: string, name: string, offset = 0) =>
    request<{ offset: number; text: string }>(
      `/api/sessions/${id}/calls/${name}?offset=${offset}`,
    ),
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
