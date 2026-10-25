import DOMPurify from "dompurify";

// Own instance, so the link hook below does not leak into other DOMPurify users.
const purifier = DOMPurify(window);
let jiraBase: string | null = null;

purifier.addHook("afterSanitizeAttributes", (node) => {
  for (const name of ["href", "src"]) {
    const value = node.getAttribute(name);
    // Downloaded attachments are served by this app; any other site-relative URL is a Jira page.
    if (value?.startsWith("/") && !value.startsWith("//") && !value.startsWith("/api/")) {
      if (jiraBase) node.setAttribute(name, jiraBase.replace(/\/$/, "") + value);
      else if (name === "href") node.removeAttribute(name);
    }
  }
  if (node.tagName === "A" && node.hasAttribute("href") && !node.getAttribute("href")!.startsWith("#")) {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

/**
 * Jira's rendered HTML, made safe to inject: no scripts, event handlers, forms or frames.
 * Site-relative links are pointed at Jira (`baseUrl`), except the local attachment URLs.
 */
export function sanitizeJiraHtml(html: string, baseUrl: string | null): string {
  jiraBase = baseUrl;
  try {
    return purifier.sanitize(html, { FORBID_TAGS: ["style", "form", "input", "button", "textarea", "select"] });
  } finally {
    jiraBase = null;
  }
}

export function JiraHtml({ html, baseUrl }: { html: string; baseUrl: string | null }) {
  if (!html.trim()) return <p className="hint">No description.</p>;
  return <div className="jira-html" dangerouslySetInnerHTML={{ __html: sanitizeJiraHtml(html, baseUrl) }} />;
}
