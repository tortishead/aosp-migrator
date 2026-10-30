import type { ReworkStatus } from "../api";
import { REWORK_TEXT, reworkClass } from "./rework";

/** Backlog rework column: the last rework or review outcome, its reason in the tooltip. */
export function ReworkTag({ status, reason }: { status: ReworkStatus | null; reason: string | null }) {
  if (!status) return null;
  return (
    <span className={reworkClass(status)} title={reason || undefined}>
      {REWORK_TEXT[status]}
    </span>
  );
}
