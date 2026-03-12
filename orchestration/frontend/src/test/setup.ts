import '@testing-library/jest-dom/vitest'

// Stub ResizeObserver for jsdom (used by DependencyOverlay)
;(globalThis as Record<string, unknown>).ResizeObserver = class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
