import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { mockApi, renderRoute } from "./test-utils";

describe("routes", () => {
  beforeEach(() => {
    mockApi({});
  });

  it("renders workspaces at root", async () => {
    renderRoute("/");
    expect(await screen.findByRole("heading", { name: "Workspaces" })).toBeInTheDocument();
  });

  it("renders item detail from a deep link", async () => {
    renderRoute("/workspaces/3/items/ABC-1");
    expect(await screen.findByRole("heading", { name: "ABC-1" })).toBeInTheDocument();
  });

  it("renders not found for unknown paths", async () => {
    renderRoute("/nope");
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });
});
