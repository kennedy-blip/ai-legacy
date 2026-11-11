"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";

interface Edges {
  left: boolean;
  right: boolean;
}

const FADE = "pointer-events-none absolute inset-y-0 w-10";

/**
 * A box that scrolls sideways and says so. While part of the content is cut off, the
 * hidden side fades out and a caption names the direction; when everything fits there
 * is nothing to see.
 *
 * `contentKey` must change whenever the content is replaced by something of a
 * different size (the box itself keeps its size then, so nothing else would notice).
 */
export function ScrollArea({ children, contentKey }: { children: ReactNode; contentKey?: string | number }) {
  const box = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState<Edges>({ left: false, right: false });

  useEffect(() => {
    const element = box.current;
    if (!element) return;
    const measure = () => {
      const left = element.scrollLeft > 1;
      const right = element.scrollLeft + element.clientWidth < element.scrollWidth - 1;
      setEdges((current) => (current.left === left && current.right === right ? current : { left, right }));
    };
    // Observing reports the current size once, so no separate first measurement is needed.
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    if (element.firstElementChild) observer.observe(element.firstElementChild);
    element.addEventListener("scroll", measure, { passive: true });
    return () => {
      observer.disconnect();
      element.removeEventListener("scroll", measure);
    };
  }, [contentKey]);

  const cut = edges.left || edges.right;
  return (
    <div className="relative min-w-0">
      {cut ? (
        <p className="mb-1 text-right text-[11px] text-faint">
          {edges.left ? "← " : ""}wider than this panel: scroll sideways{edges.right ? " →" : ""}
        </p>
      ) : null}
      <div className="relative">
        <div ref={box} className="overflow-auto">
          {children}
        </div>
        {edges.right ? (
          <div
            aria-hidden="true"
            className={`${FADE} right-0`}
            style={{ background: "linear-gradient(to left, var(--color-panel), transparent)" }}
          />
        ) : null}
        {edges.left ? (
          <div
            aria-hidden="true"
            className={`${FADE} left-0`}
            style={{ background: "linear-gradient(to right, var(--color-panel), transparent)" }}
          />
        ) : null}
      </div>
    </div>
  );
}
