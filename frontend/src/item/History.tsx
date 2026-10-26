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

interface Description {
  title: string;
  details: string[];
}

function describe(event: HistoryEvent): Description {
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
    case "project_picked":
      return { title: `Target project for ${p.filename}: ${p.new ?? "mapped automatically"}`, details: [] };
    case "cluster_joined":
      return { title: `Added to cluster ${p.name}`, details: [] };
    case "cluster_left":
      if (p.moved_to) return { title: `Moved from cluster ${p.name} to ${p.moved_to}`, details: [] };
      if (p.dissolved) return { title: `Cluster ${p.name} dissolved`, details: [] };
      return { title: `Removed from cluster ${p.name}`, details: [] };
    default:
      return { title: event.type, details: [] };
  }
}

function keyList(value: unknown): string {
  return Array.isArray(value) ? value.join(", ") : "";
}

export function describeClusterEvent(event: HistoryEvent): Description {
  const p = event.payload;
  switch (event.type) {
    case "created":
      return { title: `Created with ${keyList(p.keys)}`, details: [] };
    case "renamed":
      return { title: `Renamed: ${p.old} → ${p.new}`, details: [] };
    case "description_changed":
      return { title: "Description changed", details: [] };
    case "members_added":
      return { title: `Added ${keyList(p.keys)}`, details: [] };
    case "member_removed":
      return { title: p.moved_to ? `${p.key} moved to cluster ${p.moved_to}` : `Removed ${p.key}`, details: [] };
    case "reordered":
      return { title: `Reordered: ${keyList(p.keys)}`, details: [] };
    case "note_added":
      return { title: "Note", details: [String(p.text ?? "")] };
    default:
      return { title: event.type, details: [] };
  }
}

interface TimelineProps {
  events: HistoryEvent[];
  // Defaults to item history events.
  describe?: (event: HistoryEvent) => Description;
}

/** Newest first. */
export function HistoryTimeline({ events, describe: describeEvent = describe }: TimelineProps) {
  if (events.length === 0) return <p className="hint">No history yet.</p>;
  return (
    <ol className="timeline" aria-label="History">
      {[...events].reverse().map((event) => {
        const { title, details } = describeEvent(event);
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
