import { useMemo, useState } from "react";

import { ago, tokens as fmtTokens } from "../format";
import type { SessionRow, Theme } from "../types";

function SunIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      width="15"
      height="15"
      aria-hidden="true"
      focusable="false"
    >
      <circle
        cx="8"
        cy="8"
        r="3.1"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
      />
      <g stroke="currentColor" strokeWidth="1.3" strokeLinecap="round">
        <path d="M8 1v2M8 13v2M1 8h2M13 8h2M3.1 3.1l1.4 1.4M11.5 11.5l1.4 1.4M12.9 3.1l-1.4 1.4M4.5 11.5l-1.4 1.4" />
      </g>
    </svg>
  );
}

/* A power symbol: the arc with a break at the top, and the stroke through it. Drawn
   for the same reason the sun and moon are — ⏻ is a glyph most system fonts lack. */
function PowerIcon() {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true" focusable="false">
      <path
        d="M4.6 4.1a5 5 0 1 0 6.8 0"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
      <path d="M8 2.2v5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  )
}

function MoonIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      width="15"
      height="15"
      aria-hidden="true"
      focusable="false"
    >
      <path
        d="M13.2 10.1A5.6 5.6 0 0 1 6 2.8a5.6 5.6 0 1 0 7.2 7.3z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * Every council on this machine, newest first.
 *
 * Councils are cheap to start and their tasks rhyme, so a list of them is a list of
 * near-identical lines: the time each one ran is usually the only thing that tells
 * two apart. That is why it is on every row, and why the filter and the delete both
 * live here rather than being one more thing to go and find.
 */
export function Sidebar({
  sessions,
  active,
  onNew,
  onOpen,
  onDelete,
  theme,
  onTheme,
  onClose,
}: {
  sessions: SessionRow[];
  active: string | null;
  onNew: () => void;
  onOpen: (id: string) => void;
  onDelete: (id: string) => void;
  theme: Theme;
  onTheme: (theme: Theme) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return sessions;
    return sessions.filter((row) =>
      `${row.task} ${row.project} ${row.id} ${row.mode} ${row.state}`
        .toLowerCase()
        .includes(needle),
    );
  }, [sessions, query]);

  const running = sessions.filter((row) => row.live).length;

  return (
    <aside className="sidebar">
      <div className="brand">
        <h1>
          Coun<span>c</span>il
        </h1>
        {running > 0 && (
          <span className="brand-live">
            <span className="dot" />
            {running} running
          </span>
        )}
        <span className="spacer" />
        {/* Drawn, not a dingbat. ☀/☾ depend on a glyph the system font may not have,
            and where it is missing the button renders as a stray letter. */}
        <button
          className="ghost icon"
          title={theme === "dark" ? "Read on paper" : "Read by lamplight"}
          aria-label={
            theme === "dark"
              ? "Switch to the light theme"
              : "Switch to the dark theme"
          }
          onClick={() => onTheme(theme === "dark" ? "light" : "dark")}
        >
          {theme === "dark" ? <MoonIcon /> : <SunIcon />}
        </button>
        {/* The way out. A control plane you can only close by remembering which
            terminal started it, and what the command was called, is one you leave
            running — so it lives next to the thing you look at, not in your shell. */}
        <button
          className="ghost icon"
          title="Close Council and stop the server"
          aria-label="Close Council and stop the server"
          onClick={onClose}
        >
          <PowerIcon />
        </button>
      </div>

      <div className="sidebar-actions">
        <button className="primary wide" onClick={onNew}>
          New council
        </button>
        {sessions.length > 6 && (
          <input
            className="filter"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="filter…"
            spellCheck={false}
          />
        )}
      </div>

      <div className="session-list">
        {sessions.length === 0 && <div className="empty">No councils yet.</div>}
        {sessions.length > 0 && rows.length === 0 && (
          <div className="empty">Nothing matches “{query}”.</div>
        )}
        {rows.map((row) => (
          <div
            key={row.id}
            className={`session-row${row.id === active ? " active" : ""}`}
          >
            <button className="open" onClick={() => onOpen(row.id)}>
              <div className="top">
                <span className={`badge ${badgeClass(row)}`}>
                  {row.live && <span className="dot" />}
                  {row.paused ? "paused" : row.state}
                </span>
                <span className="when">{ago(row.started_at)}</span>
              </div>
              <div className="task">{row.task || "(no task)"}</div>
              <div className="meta">
                {row.project} · {row.mode}
                {row.tokens ? ` · ${fmtTokens(row.tokens)}` : ""}
                {row.live && row.round ? ` · round ${row.round}` : ""}
              </div>
            </button>
            <button
              className="remove"
              title={
                row.live ? "Stop it before deleting" : "Delete this council"
              }
              aria-label={`Delete the council: ${row.task || row.id}`}
              disabled={row.live}
              onClick={() => onDelete(row.id)}
            >
              ✕
            </button>
          </div>
        ))}
      </div>
    </aside>
  );
}

function badgeClass(row: SessionRow): string {
  if (row.paused) return "paused";
  if (row.live) return "running";
  return row.state;
}
