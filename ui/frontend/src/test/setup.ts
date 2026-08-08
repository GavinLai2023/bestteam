import '@testing-library/jest-dom/vitest'

// jsdom doesn't implement scrollTo (Layout.tsx calls it on every route change).
window.scrollTo = () => {}
