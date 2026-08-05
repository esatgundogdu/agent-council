export type Mode = 'consult' | 'independent' | 'review' | 'hybrid'

/** Paper or lamplight. Stamped on the root element; every colour follows from it. */
export type Theme = 'light' | 'dark'

export interface Activity {
  state: string
  tool: string
  target: string
}

export interface Panelist {
  label: string
  name: string | null
  model: string | null
  adapter: string | null
  /** How hard it was told to think, or null for the harness default. */
  effort: string | null
  dropped: boolean
  drop_reason: string | null
  verdict: string | null
  reason: string
  tokens: number
  has_plan: boolean
  plan: { chars?: number; seconds?: number; tokens?: number; session?: string } | null
  speaking: boolean
  activity: Activity | null
  /**
   * What it has done since its current turn began — tool calls and status lines,
   * oldest first. Reset by `turn_start`, capped at TRAIL_LIMIT.
   */
  trail?: string[]
  /** One per harness process this panelist's turns have started, oldest first. */
  calls?: CallRecord[]
}

/**
 * A console log on disk. The only record of a call that is not a parse of something,
 * and the only one a timed-out turn leaves behind at all.
 */
export interface CallRecord {
  file: string
  round: number
  phase: number | null
  seconds: number | null
  exit_code: number | null
  bytes: number | null
  truncated: boolean
  ok: boolean
}

export interface Turn {
  label: string
  round: number
  phase?: number
  verdict: string | null
  comment: string
  reason: string
  text: string
  streaming: boolean
  failed: boolean
  chair?: boolean
  by?: string
  note?: string
  malformed?: boolean
  resumed?: boolean
  seconds?: number
  tokens?: number
}

export interface Round {
  round: number
  turns: Turn[]
}

export interface HealthItem {
  kind: string
  agent?: string
  round?: number
  detail: string
}

export interface ControlRecord {
  action: string
  by: string
  detail: string
  ts?: string
}

export interface SessionState {
  session: {
    id: string
    name: string
    dir: string
    project_dir: string
    mode: Mode
    /** Who set the task: the main agent in an editor, or a person here. */
    convened_by: 'agent' | 'user'
    protocol: Record<string, unknown>
    timeouts: Record<string, unknown>
    started_at: string | null
    task: string
    seed: string
    context: string
  }
  status: {
    state: string
    live: boolean
    paused: boolean
    phase: number | null
    round: number | null
    elapsed: number | null
    tokens: number | null
    rounds: number | null
    termination: string | null
    error: string | null
    heartbeat_age: number | null
  }
  panel: Panelist[]
  rounds: Round[]
  health: HealthItem[]
  controls: ControlRecord[]
  compactions: { through_round: number; agent: string }[]
  has_digest: boolean
  digest: string
  seq: number
}

export interface SessionRow {
  id: string
  dir: string
  project_dir: string
  project: string
  state: string
  live: boolean
  paused: boolean
  mode: string
  phase: number | null
  round: number | null
  tokens: number | null
  elapsed: number | null
  has_digest: boolean
  task: string
  /** From the session id, which is the only start time every session records. */
  started_at: string | null
  /** Last heartbeat written to status.json — how a stalled run gives itself away. */
  updated_at: string | null
}

export interface HarnessModel {
  id: string
  label: string
  note: string
}

export interface Harness {
  adapter: string
  label: string
  binary: string
  available: boolean
  default_model: string | null
  models: HarnessModel[]
  /** Whether `models` is the harness's real catalogue or only a set of aliases. */
  enumerable: boolean
}

export type Effort = 'low' | 'medium' | 'high' | 'xhigh' | 'max'

export interface PanelMember {
  name: string
  adapter: string
  model: string | null
  /** How hard to think. null leaves the harness on its own default. */
  effort?: Effort | null
  /** An explicit path or glob, when this harness is not on PATH. */
  binary?: string | null
}

/**
 * What a council gets when the form is left alone — council.yaml, verbatim.
 *
 * The form carried its own copy of these numbers and sent them on every request,
 * which silently overrode the file: editing council.yaml changed what the CLI did
 * and nothing about what the UI did. One owner now, and the form transmits only
 * what the user actually changed.
 */
export interface Defaults {
  panel: PanelMember[]
  protocol: {
    min_rounds: number
    max_rounds: number
    token_budget: number
    wall_clock_budget: number
    compaction_threshold: number
    anonymize: boolean
    compaction_panelist: string | null
    session_continuity: boolean
  }
  timeouts: { per_call: number; per_call_phase1: number }
  on_failure: string
}

export interface Catalog {
  harnesses: Harness[]
  modes: Mode[]
  defaults: Defaults
}

export interface Project {
  dir: string
  name: string
  exists: boolean
  sessions: number
}

export interface CouncilEvent {
  seq: number
  ts: string
  event: string
  agent?: string
  [key: string]: unknown
}

export interface AgentEntry {
  role: 'sent' | 'reply' | 'tool' | 'problem' | 'narration'
  seq: number
  ts: string
  round?: number
  phase?: number
  text?: string
  tool?: string
  target?: string
  verdict?: string
  reason?: string
  seconds?: number
  tokens?: number
  session?: string
  kind?: string
  detail?: string
}

export interface AgentThread {
  label: string
  name: string | null
  model: string | null
  tokens: number
  entries: AgentEntry[]
}
