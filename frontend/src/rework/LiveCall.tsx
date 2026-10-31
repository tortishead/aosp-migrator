import { useEffect, useState } from "react";

import type { CopilotCall } from "../api";
import { ATTEMPT_RUNG_TEXT, RUNG_TEXT } from "./rework";

/** Re-renders every `ms` while `active`, for timers that tick between polls. */
export function useTick(active: boolean, ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(timer);
  }, [active, ms]);
  return now;
}

/** `m:ss`, or `h:mm:ss` from an hour. */
export function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

/** Seconds a call has run: the server's `elapsed`, plus the time since that answer arrived. */
function useElapsed(call: CopilotCall): number {
  const [anchor, setAnchor] = useState(() => ({ at: Date.now(), elapsed: call.elapsed, call: call.call }));
  if (anchor.call !== call.call || Math.abs(anchor.elapsed - call.elapsed) > 0.001) {
    setAnchor({ at: Date.now(), elapsed: call.elapsed, call: call.call });
  }
  const now = useTick(true);
  return anchor.elapsed + Math.max(0, (now - anchor.at) / 1000);
}

/** The Copilot call running now: what it is for, a ticking timer and the CLI's output so far. */
export function LiveCall({ call, title }: { call: CopilotCall; title?: string }) {
  const elapsed = useElapsed(call);
  const rung = RUNG_TEXT[call.rung as keyof typeof RUNG_TEXT]?.name ?? ATTEMPT_RUNG_TEXT[call.rung] ?? call.rung;
  const step = call.build_round > 0 ? `build fix ${call.build_round}` : call.round > 0 ? `fix round ${call.round}` : null;
  return (
    <section className="live-call" aria-label="Copilot call running">
      <div className="live-call-head">
        <span className="spinner" aria-hidden="true" />
        <strong>{title ?? "Copilot"}</strong>
        <span>
          {rung}
          {step && <span className="hint"> · {step}</span>}
        </span>
        {call.key && (
          <span className="mono">
            {call.key}
            {call.project && <span className="hint"> ({call.project})</span>}
          </span>
        )}
        <span className="mono hint">{call.model ?? "default model"}</span>
        <span className="hint">{call.call}</span>
        <span className="live-call-time" aria-label="Copilot call time">
          {clock(elapsed)}
        </span>
      </div>
      {call.log ? (
        <pre aria-label="Copilot output">{call.log}</pre>
      ) : (
        <div className="live-call-empty">Waiting for Copilot's output…</div>
      )}
    </section>
  );
}
