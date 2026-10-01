import { useEffect, useRef, useState } from "react";

/** Returns `value` after it has stopped changing for `delayMs`. */
export function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(id);
  }, [value, delayMs]);
  return debounced;
}

/** A counter for discarding responses from superseded requests. */
export function useLatest(): { next: () => number; isCurrent: (id: number) => boolean } {
  const ref = useRef(0);
  return {
    next: () => ++ref.current,
    isCurrent: (id) => id === ref.current,
  };
}
