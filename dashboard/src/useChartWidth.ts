import { useEffect, useRef, useState } from "react";

/** A ref for a chart's wrapper and the wrapper's width, kept current as it resizes. */
export function useChartWidth(initial = 640, min = 280) {
  const wrap = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(initial);
  useEffect(() => {
    if (!wrap.current) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(min, e.contentRect.width)));
    ro.observe(wrap.current);
    return () => ro.disconnect();
  }, [min]);
  return { wrap, w };
}
