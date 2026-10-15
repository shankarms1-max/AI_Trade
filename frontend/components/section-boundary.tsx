"use client";

import { Component, type ReactNode } from "react";

type Props = { label: string; children: ReactNode };
type State = { failed: boolean };

export class SectionBoundary extends Component<Props, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State { return { failed: true }; }

  render() {
    if (this.state.failed) return <section className="terminal-error" role="alert" aria-label={`${this.props.label} unavailable`}>
      <strong>{this.props.label} unavailable</strong>
      <span>This section could not load. Other sections remain available.</span>
      <button type="button" onClick={() => this.setState({ failed: false })}>Retry section</button>
    </section>;
    return this.props.children;
  }
}
