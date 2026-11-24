import { Component, type ReactNode } from "react";

/** Catches what a page throws (e.g. notFound()) so tests can assert it. */
export class ErrorBoundaryProbe extends Component<{ children: ReactNode; onError: (error: unknown) => void }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(error: unknown) { this.props.onError(error); }
  render() { return this.state.failed ? <div>boundary caught</div> : this.props.children; }
}
