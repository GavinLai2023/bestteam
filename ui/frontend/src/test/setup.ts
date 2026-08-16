import '@testing-library/jest-dom/vitest'
import { configure } from '@testing-library/dom'

// testing-library's 1s default for waitFor/findBy* is tuned for a developer's
// idle laptop. CI runs 24 test files in parallel on a 2-core runner, where a
// fetch mock resolving, React re-rendering and an effect firing can genuinely
// take longer than that -- AdvancedPage.test.tsx failed at exactly 1020ms.
//
// This costs nothing when tests pass: these helpers poll and return the moment
// their condition holds, so the timeout only bounds how long a genuinely
// failing assertion takes to report. It is a budget for an asynchronous
// condition, not a sleep, and it must never be used to paper over a race --
// a test that needs it to pass reliably has a real ordering bug to fix.
configure({ asyncUtilTimeout: 5000 })

// jsdom doesn't implement scrollTo (Layout.tsx calls it on every route change).
window.scrollTo = () => {}

// jsdom doesn't implement scrollIntoView either (used to bring an
// off-screen banner/panel into view); tests that assert it was called
// override this with their own vi.fn().
Element.prototype.scrollIntoView = Element.prototype.scrollIntoView || (() => {})
