import "@testing-library/jest-dom/vitest";

// jsdom does not implement layout scrolling; components call these harmlessly.
if (!Element.prototype.scrollTo) {
  Element.prototype.scrollTo = () => undefined;
}
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => undefined;
}

// Node 26 no longer exposes globalThis.localStorage inside worker threads.
// The app reads it for the prompt-version toggle, so supply a tiny stub.
if (!globalThis.localStorage) {
  const store = new Map<string, string>();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, String(value)),
      removeItem: (key: string) => void store.delete(key),
      clear: () => store.clear(),
      key: (index: number) => [...store.keys()][index] ?? null,
      get length() {
        return store.size;
      },
    },
  });
}
