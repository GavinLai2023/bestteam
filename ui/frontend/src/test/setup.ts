import '@testing-library/jest-dom/vitest'

// jsdom doesn't implement scrollTo (Layout.tsx calls it on every route change).
window.scrollTo = () => {}

// jsdom doesn't implement scrollIntoView either (used to bring an
// off-screen banner/panel into view); tests that assert it was called
// override this with their own vi.fn().
Element.prototype.scrollIntoView = Element.prototype.scrollIntoView || (() => {})
