import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import './index.css'
// Imported for its side effect: initialises i18next before the first render,
// so no component ever sees an uninitialised `t()` (which returns the raw key).
import './lib/i18n'
import App from './App.tsx'
import ErrorBoundary from './components/ErrorBoundary'

const rootElement = document.getElementById('root')
if (!rootElement) throw new Error('#root element not found')

createRoot(rootElement).render(
  <StrictMode>
    <ErrorBoundary>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </ErrorBoundary>
  </StrictMode>,
)
