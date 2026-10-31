export type StatusCategory = "todo" | "active" | "done";

// Jira only hands us the status name, so categorise by the usual workflow words.
export function statusCategory(status: string): StatusCategory {
  const name = status.toLowerCase();
  if (/done|closed|resolved|fixed|merged/.test(name)) return "done";
  if (/progress|review|testing|verify/.test(name)) return "active";
  return "todo";
}
