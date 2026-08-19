import { Component, type ErrorInfo, type ReactNode } from 'react'
import './ErrorBoundary.css'

interface Props {
  children: ReactNode
}

interface State {
  crashed: boolean
}

/**
 * Last line of defence against a render-time exception: without a boundary
 * React unmounts the whole tree and the customer sees a blank page with no
 * way forward. This shows a plain-language recovery message and a Reload
 * button instead. The raw error is deliberately not rendered -- it is
 * meaningless to a non-technical user and can echo backend text; React
 * already reports it on the console for the operator.
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { crashed: false }

  static getDerivedStateFromError(): State {
    return { crashed: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('Unhandled render error', error, info.componentStack)
  }

  render(): ReactNode {
    if (!this.state.crashed) return this.props.children
    return (
      <div className="error-boundary" role="alert">
        <h1>Something went wrong</h1>
        <p>
          This page hit an unexpected error. Reload to continue; if it keeps happening, tell
          the operator the time and the page you were on.
        </p>
        <button type="button" onClick={() => window.location.reload()}>
          Reload the page
        </button>
      </div>
    )
  }
}
