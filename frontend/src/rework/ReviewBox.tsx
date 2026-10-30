import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { api, type ItemPatch, type ReworkAttempt } from "../api";
import { itemsKey } from "../backlog/ImportPanel";
import { REVIEW_TEXT, reviewClass } from "./rework";

interface Props {
  workspaceId: number;
  patch: ItemPatch;
  /** The attempt that made it, for Copilot's explanation. */
  attempt: ReworkAttempt | undefined;
}

/** Review of one rework patch: state, base chain, staleness, findings, Copilot's explanation, Accept / Reject with a note. */
export function ReviewBox({ workspaceId, patch, attempt }: Props) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState("");
  const onReviewed = () => {
    setNote("");
    // Another item's patch may sit on this one: refetch every item's patches.
    void queryClient.invalidateQueries({ queryKey: ["itemPatches", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: itemsKey(workspaceId) });
    void queryClient.invalidateQueries({ queryKey: ["item", workspaceId] });
  };
  const accept = useMutation({ mutationFn: () => api.acceptPatch(workspaceId, patch.id), onSuccess: onReviewed });
  const reject = useMutation({ mutationFn: () => api.rejectPatch(workspaceId, patch.id, note), onSuccess: onReviewed });
  const blocking = patch.bases.filter((b) => b.review !== "accepted");
  const answers = (attempt?.calls ?? []).filter((c) => c.project === patch.project && c.status === "ok" && c.answer.trim());
  const pending = accept.isPending || reject.isPending;

  return (
    <section className="review-box" aria-label="Review">
      <p className="actions">
        <span>
          Review: <span className={reviewClass(patch.review)}>{REVIEW_TEXT[patch.review]}</span>
        </span>
        {patch.stale.length > 0 && <span className="tag tag-warn">stale</span>}
      </p>
      {patch.review_note && <p>Note: {patch.review_note}</p>}
      {patch.stale.length > 0 && (
        <p className="hint" aria-label="Stale reason">
          Out of date: {patch.stale.join("; ")}. Rework the item for a fresh patch; this one still builds.
        </p>
      )}
      <div aria-label="Base chain">
        Made on{" "}
        <span className="mono" title={patch.base_head ?? undefined}>
          {patch.base_head ? `HEAD ${patch.base_head.slice(0, 12)}` : "the tree"}
        </span>
        {patch.bases.map((b) => (
          <span key={b.id}>
            {" "}
            + <Link to={`/workspaces/${workspaceId}/items/${b.key}?tab=migration`}>{b.key}</Link>{" "}
            <span className="mono">({b.project})</span> <span className={reviewClass(b.review)}>{REVIEW_TEXT[b.review]}</span>
          </span>
        ))}
      </div>

      {patch.findings.length > 0 && (
        <section aria-label="Findings">
          <h4>Findings</h4>
          <ul>
            {patch.findings.map((f, i) => (
              <li key={i}>
                <span className={f.severity === "error" ? "tag tag-error" : "tag tag-warn"}>{f.severity}</span> <span className="tag">{f.kind}</span>{" "}
                {f.path && <span className="mono">{f.path}: </span>}
                {f.message}
              </li>
            ))}
          </ul>
        </section>
      )}
      {answers.length > 0 && (
        <section aria-label="Copilot's explanation">
          <h4>Copilot's explanation</h4>
          {answers.map((c, i) => (
            <pre key={i} className="copilot-answer">
              {c.answer}
            </pre>
          ))}
        </section>
      )}

      {(patch.review === "proposed" || patch.review === "accepted") && (
        <div className="form">
          {blocking.length > 0 && patch.review === "proposed" && (
            <p className="hint">
              Accept the base {blocking.length === 1 ? "patch" : "patches"} first: {blocking.map((b) => `${b.key} (${b.project}, ${b.review})`).join(", ")}
            </p>
          )}
          <label>
            Note for the next rework
            <textarea aria-label="Reject note" rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
          </label>
          <div className="actions">
            {patch.review === "proposed" && (
              <button onClick={() => accept.mutate()} disabled={pending || blocking.length > 0}>
                Accept
              </button>
            )}
            <button onClick={() => reject.mutate()} disabled={pending}>
              Reject
            </button>
            {accept.isError && <span className="error">{accept.error.message}</span>}
            {reject.isError && <span className="error">{reject.error.message}</span>}
          </div>
        </div>
      )}
    </section>
  );
}
