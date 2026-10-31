import type { ReworkStatus } from "../api";
import { REWORK_TEXT, reworkClass } from "./rework";

/** Backlog rework column: the last rework or review outcome, its reason in the tooltip; a proposed patch says whether it built. */
export function ReworkTag({ status, reason, built = false }: { status: ReworkStatus | null; reason: string | null; built?: boolean }) {
  if (!status) return null;
  const title = reason || (status === "proposed" ? (built ? "Built by the rework" : "Not built by the rework") : undefined);
  return (
    <span className={reworkClass(status, built)} title={title}>
      {REWORK_TEXT[status]}
    </span>
  );
}
