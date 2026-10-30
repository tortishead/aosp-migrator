import { useState } from "react";

import { ReworkDialog } from "./ReworkDialog";

/** A finished build or pre-scan's items worth a rework, with the Rework dialog for them. */
export function ReworkOffer({ workspaceId, keys }: { workspaceId: number; keys: string[] | undefined }) {
  const [open, setOpen] = useState(false);
  if (!keys || keys.length === 0) return null;
  return (
    <p className="actions" aria-label="Rework offer">
      <span>
        {keys.length === 1 ? "1 item" : `${keys.length} items`} worth a rework: {keys.join(", ")}
      </span>
      <button onClick={() => setOpen(true)}>Rework these?</button>
      {open && <ReworkDialog workspaceId={workspaceId} keys={keys} onClose={() => setOpen(false)} />}
    </p>
  );
}
