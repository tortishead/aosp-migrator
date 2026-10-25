import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { JiraHtml, sanitizeJiraHtml } from "./JiraHtml";

const JIRA = "https://jira.example.com/jira";

function parse(html: string): HTMLElement {
  const div = document.createElement("div");
  div.innerHTML = sanitizeJiraHtml(html, JIRA);
  return div;
}

describe("sanitizeJiraHtml", () => {
  it("strips scripts, handlers and javascript: links", () => {
    const out = parse(
      '<p onclick="alert(1)">Hi<script>alert(2)</script></p><img src="x" onerror="alert(3)">' +
        '<a href="javascript:alert(4)">x</a><iframe src="https://evil.example"></iframe><style>body{display:none}</style>' +
        '<form action="https://evil.example"><input name="pat"></form>',
    );
    expect(out.querySelector("script, iframe, style, form, input")).toBeNull();
    expect(out.innerHTML).not.toMatch(/onclick|onerror|javascript:|alert/);
    expect(out.querySelector("p")!.textContent).toBe("Hi");
  });

  it("keeps tables, code blocks and panels", () => {
    const out = parse(
      "<div class='table-wrap'><table class='confluenceTable'><tbody><tr><th class='confluenceTh'>Device</th></tr>" +
        "<tr><td class='confluenceTd' colspan='2'>Pixel 8</td></tr></tbody></table></div>" +
        '<div class="code panel"><div class="codeContent panelContent"><pre class="code-java">client-&gt;notifyResume();</pre></div></div>',
    );
    expect(out.querySelector("table.confluenceTable th")!.textContent).toBe("Device");
    expect(out.querySelector("td")).toHaveAttribute("colspan", "2");
    expect(out.querySelector(".code.panel pre.code-java")!.textContent).toBe("client->notifyResume();");
  });

  it("points relative links at Jira but keeps local attachment URLs", () => {
    const out = parse(
      '<a href="/browse/PROJ-7">PROJ-7</a><img src="/api/workspaces/1/attachments/10/shot.png">' +
        '<img src="/secure/thumbnail/11/big.png"><a href="#anchor">top</a>',
    );
    const [jira, anchor] = out.querySelectorAll("a");
    expect(jira).toHaveAttribute("href", `${JIRA}/browse/PROJ-7`);
    expect(jira).toHaveAttribute("target", "_blank");
    expect(jira).toHaveAttribute("rel", "noopener noreferrer");
    expect(anchor).not.toHaveAttribute("target");
    const [local, thumb] = out.querySelectorAll("img");
    expect(local).toHaveAttribute("src", "/api/workspaces/1/attachments/10/shot.png");
    expect(thumb).toHaveAttribute("src", `${JIRA}/secure/thumbnail/11/big.png`);
  });

  it("drops relative links when no Jira URL is known", () => {
    const div = document.createElement("div");
    div.innerHTML = sanitizeJiraHtml('<a href="/browse/PROJ-7">PROJ-7</a>', null);
    expect(div.querySelector("a")).not.toHaveAttribute("href");
  });
});

describe("JiraHtml", () => {
  it("renders sanitized HTML", () => {
    const { container } = render(<JiraHtml html="<p>ok<script>bad()</script></p>" baseUrl={JIRA} />);
    expect(container.querySelector(".jira-html")!.innerHTML).toBe("<p>ok</p>");
  });
});
