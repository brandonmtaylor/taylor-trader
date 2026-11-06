import { Component, type ErrorInfo, type ReactNode } from "react";

/** One bad render shouldn't blank the app: the header, and with it the mode and pause controls, stay. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(error, info.componentStack);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <main className="page">
        <p className="empty" role="alert">
          This page couldn't be shown. That's a problem with the dashboard: the trading service isn't affected.
          Reload to try again.
        </p>
      </main>
    );
  }
}
