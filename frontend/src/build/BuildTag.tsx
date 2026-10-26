import type { BuildStatus } from "../api";
import { BUILD_TEXT, buildClass } from "./build";

/** Backlog build column: the last build's result, its reason in the tooltip. */
export function BuildTag({ status, reason }: { status: BuildStatus | null; reason: string | null }) {
  if (!status) return null;
  return (
    <span className={buildClass(status)} title={reason || undefined}>
      {BUILD_TEXT[status]}
    </span>
  );
}
