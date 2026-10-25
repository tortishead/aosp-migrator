import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import { routes } from "./routes";

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
}

describe("routes", () => {
  it("renders workspaces at root", () => {
    renderAt("/");
    expect(screen.getByRole("heading", { name: "Workspaces" })).toBeInTheDocument();
  });

  it("renders settings", () => {
    renderAt("/settings");
    expect(screen.getByRole("heading", { name: "Settings" })).toBeInTheDocument();
  });

  it("renders item detail from a deep link", () => {
    renderAt("/workspaces/3/items/ABC-1");
    expect(screen.getByRole("heading", { name: "ABC-1" })).toBeInTheDocument();
  });

  it("renders not found for unknown paths", () => {
    renderAt("/nope");
    expect(screen.getByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });
});
