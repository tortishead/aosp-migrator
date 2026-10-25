import type { HistoryEvent } from "../api";
import { STATE_LABELS } from "../backlog/params";

const FIELD_LABELS: Record<string, string> = {
  summary: "summary",
  description_raw: "description",
  status: "Jira status",
  labels: "labels",
  components: "components",
  fix_versions: "fix versions",
  issue_links: "issue links",
};

function stateLabel(value: unknown): string {
  return STATE_LABELS[value as keyof typeof STATE_LABELS] ?? String(value);
}

// Values short enough to show inline as "old → new"; other fields only get named.
const INLINE_FIELDS = ["summary", "status", "labels", "components", "fix_versions"];

const COUNTS: [string, string, string][] = [
  ["comments_added", "comment added", "comments added"],
  ["comments_edited", "comment edited", "comments edited"],
  ["comments_removed", "comment removed", "comments removed"],
];

function show(value: unknown): string {
  return Array.isArray(value) ? value.join(", ") || "none" : String(value);
}

function describeUpdate(payload: Record<string, unknown>): string[] {
  const parts: string[] = [];
  const fields = (payload.fields ?? {}) as Record<string, { old: unknown; new: unknown }>;
  for (const [name, change] of Object.entries(fields)) {
    const label = FIELD_LABELS[name] ?? name;
    parts.push(INLINE_FIELDS.includes(name) ? `${label}: ${show(change.old)} → ${show(change.new)}` : `${label} changed`);
  }
  for (const [name, one, many] of COUNTS) {
    const count = payload[name];
    if (typeof count === "number") parts.push(`${count} ${count === 1 ? one : many}`);
  }
  if (Array.isArray(payload.attachments_added)) parts.push(`attachments added: ${payload.attachments_added.join(", ")}`);
  if (Array.isArray(payload.attachments_removed)) parts.push(`attachments removed: ${payload.attachments_removed.join(", ")}`);
  return parts;
}

function describe(event: HistoryEvent): { title: string; details: string[] } {
  const p = event.payload;
  switch (event.type) {
    case "imported":
      return { title: "Imported from Jira", details: [] };
    case "updated":
      return { title: "Updated from Jira", details: describeUpdate(p) };
    case "dropped":
      return { title: "Dropped: no longer matched by the workspace JQL", details: [] };
    case "restored":
      return { title: "Matched by the workspace JQL again", details: [] };
    case "import_failed":
      return { title: "Import failed", details: [String(p.error ?? "")] };
    case "state_changed":
      return { title: `State: ${stateLabel(p.old)} → ${stateLabel(p.new)}`, details: [] };
    case "note_added":
      return { title: "Note", details: [String(p.text ?? "")] };
    default:
      return { title: event.type, details: [] };
  }
}

/** Newest first. */
export function HistoryTimeline({ events }: { events: HistoryEvent[] }) {
  if (events.length === 0) return <p className="hint">No history yet.</p>;
  return (
    <ol className="timeline" aria-label="History">
      {[...events].reverse().map((event) => {
        const { title, details } = describe(event);
        return (
          <li key={event.id} className={`timeline-${event.type}`}>
            <time dateTime={event.created_at}>{new Date(event.created_at).toLocaleString()}</time>
            <div>
              <strong>{title}</strong>
              {details.length > 0 && (
                <ul className={event.type === "note_added" ? "note-text" : undefined}>
                  {details.map((d, i) => (
                    <li key={i}>{d}</li>
                  ))}
                </ul>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
