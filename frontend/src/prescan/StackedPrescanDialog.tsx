import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

import type { Item } from "../api";
import { memberWarning } from "../clusters/shared";
import { useStartPrescan } from "./PrescanPanel";

/** PROJ-9 before PROJ-10. */
export function compareKeys(a: string, b: string): number {
  return a.localeCompare(b, undefined, { numeric: true });
}

type StackItem = Pick<Item, "key" | "summary" | "local_state" | "dropped">;

interface Props {
  workspaceId: number;
  items: StackItem[];
  onClose: () => void;
}

/** Order the selected items by drag and drop (default: by key), then stack each item's diffs on the ones before it. */
export function StackedPrescanDialog({ workspaceId, items, onClose }: Props) {
  const [order, setOrder] = useState(() => [...items].sort((a, b) => compareKeys(a.key, b.key)));
  const [dragged, setDragged] = useState<string | null>(null);
  const start = useStartPrescan(workspaceId, true);

  useEffect(() => {
    const close = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", close);
    return () => document.removeEventListener("keydown", close);
  }, [onClose]);

  /** Move the dragged item to where `over` is. */
  function moveTo(over: string) {
    if (dragged === null || dragged === over) return;
    setOrder((current) => {
      const from = current.findIndex((i) => i.key === dragged);
      const to = current.findIndex((i) => i.key === over);
      const next = [...current];
      next.splice(to, 0, ...next.splice(from, 1));
      return next;
    });
  }

  return createPortal(
    <div className="popup-backdrop" onClick={onClose}>
      <div className="popup" role="dialog" aria-modal="true" aria-label="Stacked pre-scan" onClick={(e) => e.stopPropagation()}>
        <div className="popup-header">
          <h2>Stacked pre-scan</h2>
          <button type="button" className="link-button" onClick={onClose}>
            Close
          </button>
        </div>
        <p className="hint">
          Each item's diffs are applied on top of the items above it. Drag to reorder. An item that does not apply is left out and the
          rest stack without it. Results go into each item's history.
        </p>
        <ol className="stack-order" aria-label="Stack order">
          {order.map((item) => {
            const warning = memberWarning(item);
            return (
              <li
                key={item.key}
                draggable
                className={item.key === dragged ? "dragging" : undefined}
                onDragStart={(e) => {
                  e.dataTransfer.effectAllowed = "move";
                  // Firefox starts no drag without data.
                  e.dataTransfer.setData("text/plain", item.key);
                  setDragged(item.key);
                }}
                onDragEnter={() => moveTo(item.key)}
                onDragOver={(e) => e.preventDefault()}
                onDragEnd={() => setDragged(null)}
                onDrop={(e) => e.preventDefault()}
              >
                <span className="drag-handle" aria-hidden="true">
                  ⠿
                </span>
                <strong>{item.key}</strong> <span>{item.summary}</span>
                {warning && <span className="tag tag-warn">{warning}</span>}
              </li>
            );
          })}
        </ol>
        <div className="actions">
          <button
            onClick={() => start.mutate(order.map((i) => i.key), { onSuccess: onClose })}
            disabled={order.length < 2 || start.isPending}
            title={order.length < 2 ? "A stacked pre-scan needs at least two items" : undefined}
          >
            Start stacked pre-scan
          </button>
          {start.isError && <span className="error">{start.error.message}</span>}
        </div>
      </div>
    </div>,
    document.body,
  );
}
