"use client";
/**
 * Lightweight virtual scroll - renders only items in the visible window.
 * Max ~200 DOM nodes regardless of list length.
 * Uses spacer divs for correct total scroll height.
 */

import {
  useRef,
  useState,
  useCallback,
  useLayoutEffect,
  type ReactNode,
} from "react";

const DEFAULT_ITEM_HEIGHT = 72; // px, estimated for a collapsed event row
const OVERSCAN = 8; // items to render beyond visible boundary on each side

interface VirtualListProps<T> {
  items: T[];
  itemHeight?: number;
  className?: string;
  renderItem: (item: T, index: number) => ReactNode;
  onScrolledToTop?: () => void;
  onScrolledToBottom?: () => void;
  /** Increment to programmatically scroll to top */
  scrollToTopTrigger?: number;
}

export function VirtualList<T>({
  items,
  itemHeight = DEFAULT_ITEM_HEIGHT,
  className = "",
  renderItem,
  onScrolledToTop,
  onScrolledToBottom,
  scrollToTopTrigger,
}: VirtualListProps<T>) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [containerHeight, setContainerHeight] = useState(600);
  const prevTrigger = useRef(scrollToTopTrigger);

  // Scroll to top when trigger increments
  useLayoutEffect(() => {
    if (scrollToTopTrigger !== prevTrigger.current) {
      prevTrigger.current = scrollToTopTrigger;
      containerRef.current?.scrollTo({ top: 0, behavior: "smooth" });
    }
  }, [scrollToTopTrigger]);

  // Track container height for correct windowing
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setContainerHeight(el.clientHeight));
    ro.observe(el);
    setContainerHeight(el.clientHeight);
    return () => ro.disconnect();
  }, []);

  const handleScroll = useCallback(
    (e: React.UIEvent<HTMLDivElement>) => {
      const el = e.currentTarget;
      setScrollTop(el.scrollTop);
      if (onScrolledToTop && el.scrollTop <= 0) onScrolledToTop();
      if (
        onScrolledToBottom &&
        el.scrollTop + el.clientHeight >= el.scrollHeight - 80
      ) {
        onScrolledToBottom();
      }
    },
    [onScrolledToTop, onScrolledToBottom]
  );

  const startIndex = Math.max(
    0,
    Math.floor(scrollTop / itemHeight) - OVERSCAN
  );
  const visibleCount = Math.ceil(containerHeight / itemHeight) + OVERSCAN * 2;
  const endIndex = Math.min(items.length - 1, startIndex + visibleCount);

  const paddingTop = startIndex * itemHeight;
  const paddingBottom = Math.max(0, (items.length - endIndex - 1) * itemHeight);

  return (
    <div
      ref={containerRef}
      onScroll={handleScroll}
      className={className}
      style={{ overflowY: "auto", height: "100%" }}
    >
      {/* Top spacer maintains scroll position */}
      {paddingTop > 0 && (
        <div style={{ height: paddingTop }} aria-hidden="true" />
      )}

      {items.slice(startIndex, endIndex + 1).map((item, i) =>
        renderItem(item, startIndex + i)
      )}

      {/* Bottom spacer for correct total scroll height */}
      {paddingBottom > 0 && (
        <div style={{ height: paddingBottom }} aria-hidden="true" />
      )}
    </div>
  );
}
