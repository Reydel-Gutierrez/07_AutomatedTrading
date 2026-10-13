// A draggable, zoomable viewport over an SVG world. Drag (mouse or one finger) to look
// around, scroll or pinch to zoom, double-click to zoom in, keyboard +/-/0/arrows. The
// camera lives in a ref and is written straight to the DOM, so moving it never re-renders
// the world. Clicks still reach the world unless the pointer was dragged.

import { type ReactNode, forwardRef, useCallback, useEffect, useImperativeHandle, useRef } from "react";
import type { Rect } from "./departments";

export type Camera = {
  fit: (animate?: boolean) => void;
  flyTo: (r: Rect, pad?: number) => void;
  zoomBy: (f: number) => void;
};

type Cam = { x: number; y: number; k: number };
const MAX_K = 3;
const FAR_BELOW = 0.62; // under this zoom only names are shown, not details
const DRAG_PX = 5;

const ease = (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2);

export const PanZoom = forwardRef<Camera, { world: { w: number; h: number }; start?: Rect; insetRight?: number; insetBottom?: number; label: string; children: ReactNode; onBackground?: () => void }>(function PanZoom(
  { world, start, insetRight = 0, insetBottom = 0, label, children, onBackground },
  ref,
) {
  const box = useRef<HTMLDivElement>(null);
  const svg = useRef<SVGSVGElement>(null);
  const layer = useRef<SVGGElement>(null);
  const cam = useRef<Cam>({ x: 0, y: 0, k: 0.4 });
  const size = useRef({ w: 0, h: 0 });
  const insets = useRef({ r: insetRight, b: insetBottom });
  insets.current = { r: insetRight, b: insetBottom };
  const anim = useRef<number>();
  const fitted = useRef(false);
  const first = useRef(start);

  const fitK = () => {
    const w = Math.max(200, size.current.w - insets.current.r), h = Math.max(200, size.current.h - insets.current.b);
    return Math.min(w / world.w, h / world.h);
  };
  const minK = () => Math.min(fitK() * 0.7, 0.25);

  const apply = useCallback(() => {
    const c = cam.current;
    const { w, h } = size.current;
    // Keep at least part of the world on screen.
    const m = 120;
    c.x = Math.min(w - m, Math.max(m - world.w * c.k, c.x));
    c.y = Math.min(h - m, Math.max(m - world.h * c.k, c.y));
    layer.current?.setAttribute("transform", `translate(${c.x.toFixed(2)} ${c.y.toFixed(2)}) scale(${c.k.toFixed(4)})`);
    const el = svg.current;
    if (el) {
      el.style.setProperty("--lbl", String(Math.min(1.6, Math.max(1, 0.72 / c.k))));
      el.classList.toggle("fm-far", c.k < FAR_BELOW);
    }
  }, [world.w, world.h]);

  const stop = () => {
    if (anim.current) cancelAnimationFrame(anim.current);
  };

  const animateTo = useCallback(
    (to: Cam, ms = 450) => {
      stop();
      const from = { ...cam.current };
      if (ms <= 0 || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        cam.current = to;
        return apply();
      }
      const t0 = performance.now();
      const step = (t: number) => {
        const p = ease(Math.min(1, (t - t0) / ms));
        const k = Math.exp(Math.log(from.k) + (Math.log(to.k) - Math.log(from.k)) * p);
        cam.current = { x: from.x + (to.x - from.x) * p, y: from.y + (to.y - from.y) * p, k };
        apply();
        if (p < 1) anim.current = requestAnimationFrame(step);
      };
      anim.current = requestAnimationFrame(step);
    },
    [apply],
  );

  const target = (r: Rect, pad: number): Cam => {
    const vw = size.current.w - insets.current.r, vh = size.current.h - insets.current.b;
    const k = Math.min(MAX_K, Math.max(minK(), Math.min((vw - pad * 2) / r[2], (vh - pad * 2) / r[3])));
    return { k, x: vw / 2 - (r[0] + r[2] / 2) * k, y: vh / 2 - (r[1] + r[3] / 2) * k };
  };

  const fit = useCallback((animate = true) => animateTo(target([-70, 70, world.w + 140, world.h - 70], 8), animate ? 450 : 0), [animateTo, world.w, world.h]);

  const zoomAt = useCallback(
    (px: number, py: number, f: number) => {
      stop();
      const c = cam.current;
      const k = Math.min(MAX_K, Math.max(minK(), c.k * f));
      cam.current = { k, x: px - ((px - c.x) * k) / c.k, y: py - ((py - c.y) * k) / c.k };
      apply();
    },
    [apply],
  );

  useImperativeHandle(ref, () => ({
    fit,
    flyTo: (r, pad = 40) => animateTo(target(r, pad)),
    zoomBy: (f) => zoomAt((size.current.w - insets.current.r) / 2, (size.current.h - insets.current.b) / 2, f),
  }));

  // size: fit once on first layout, then keep the world centred where it was
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const prev = size.current;
      const r = el.getBoundingClientRect();
      size.current = { w: r.width, h: r.height };
      if (!fitted.current && r.width > 0) {
        fitted.current = true;
        if (first.current) animateTo(target(first.current, 40), 0);
        else fit(false);
      } else {
        cam.current.x += (r.width - prev.w) / 2;
        cam.current.y += (r.height - prev.h) / 2;
        apply();
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [apply, fit, animateTo]);

  // wheel zoom (non-passive so the page does not scroll)
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
      zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-dy * (e.ctrlKey ? 0.01 : 0.0016)));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [zoomAt]);

  useEffect(() => stop, []);

  // drag and pinch
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const drag = useRef({ moved: false, sx: 0, sy: 0, pinch: 0 });
  const suppress = useRef(false);

  const local = (e: { clientX: number; clientY: number }) => {
    const r = box.current!.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0 && e.pointerType === "mouse") return;
    stop();
    const p = local(e);
    pointers.current.set(e.pointerId, p);
    if (pointers.current.size === 1) drag.current = { moved: false, sx: p.x, sy: p.y, pinch: 0 };
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      drag.current.pinch = Math.hypot(a.x - b.x, a.y - b.y);
      drag.current.moved = true;
      box.current?.setPointerCapture(e.pointerId);
    }
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const prev = pointers.current.get(e.pointerId);
    if (!prev) return;
    const p = local(e);
    if (pointers.current.size === 1) {
      if (!drag.current.moved && Math.hypot(p.x - drag.current.sx, p.y - drag.current.sy) > DRAG_PX) {
        drag.current.moved = true;
        box.current?.setPointerCapture(e.pointerId);
        box.current?.classList.add("fm-dragging");
      }
      if (drag.current.moved) {
        cam.current.x += p.x - prev.x;
        cam.current.y += p.y - prev.y;
        apply();
      }
      pointers.current.set(e.pointerId, p);
      return;
    }
    // two fingers: zoom about the midpoint and pan with it
    const others = [...pointers.current.entries()].filter(([id]) => id !== e.pointerId);
    const o = others[0]?.[1];
    pointers.current.set(e.pointerId, p);
    if (!o) return;
    const dist = Math.hypot(p.x - o.x, p.y - o.y);
    const midPrev = { x: (prev.x + o.x) / 2, y: (prev.y + o.y) / 2 };
    const mid = { x: (p.x + o.x) / 2, y: (p.y + o.y) / 2 };
    if (drag.current.pinch > 0) zoomAt(midPrev.x, midPrev.y, dist / drag.current.pinch);
    drag.current.pinch = dist;
    cam.current.x += mid.x - midPrev.x;
    cam.current.y += mid.y - midPrev.y;
    apply();
  };

  const onPointerEnd = (e: React.PointerEvent) => {
    pointers.current.delete(e.pointerId);
    if (pointers.current.size === 0) {
      box.current?.classList.remove("fm-dragging");
      if (drag.current.moved) {
        // the click that ends a drag must not select anything
        suppress.current = true;
        window.setTimeout(() => (suppress.current = false), 0);
      }
    } else if (pointers.current.size === 1) {
      drag.current.pinch = 0;
    }
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.target !== box.current) return;
    const step = 80;
    const keys: Record<string, () => void> = {
      "+": () => zoomAt(size.current.w / 2, size.current.h / 2, 1.25),
      "=": () => zoomAt(size.current.w / 2, size.current.h / 2, 1.25),
      "-": () => zoomAt(size.current.w / 2, size.current.h / 2, 0.8),
      "0": () => fit(),
      ArrowLeft: () => ((cam.current.x += step), apply()),
      ArrowRight: () => ((cam.current.x -= step), apply()),
      ArrowUp: () => ((cam.current.y += step), apply()),
      ArrowDown: () => ((cam.current.y -= step), apply()),
    };
    const run = keys[e.key];
    if (run) {
      e.preventDefault();
      stop();
      run();
    }
  };

  return (
    <div
      ref={box}
      tabIndex={0}
      role="application"
      aria-label={`${label}. Drag to look around, scroll or pinch to zoom, plus and minus keys to zoom, arrow keys to move, 0 to see everything.`}
      className="fm-viewport absolute inset-0 touch-none select-none overflow-hidden outline-none"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerEnd}
      onPointerCancel={onPointerEnd}
      onClickCapture={(e) => {
        if (suppress.current) {
          e.stopPropagation();
          e.preventDefault();
        }
      }}
      onDoubleClick={(e) => {
        if ((e.target as Element).closest("[data-fm-hit]")) return;
        const p = local(e);
        zoomAt(p.x, p.y, 1.6);
      }}
      onKeyDown={onKeyDown}
    >
      <svg ref={svg} className="fs-scene block h-full w-full" onClick={(e) => e.target === svg.current && onBackground?.()}>
        <g ref={layer}>{children}</g>
      </svg>
    </div>
  );
});
