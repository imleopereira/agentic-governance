"use client";
/**
 * v4 React error boundary.
 *
 * Wraps the v4 route tree so that a render-time exception in any drill
 * panel or tab does not white-screen the whole console. Defaults to a
 * small centered card with a "Try again" reset. Intended to be imported
 * by the v4 layout as `@/components/v4/ErrorBoundary`.
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

import { sanitizeErrorMessage } from "@/lib/connectionStore";

export interface ErrorBoundaryProps {
  children: ReactNode;
  fallback?: ReactNode | ((error: Error, reset: () => void) => ReactNode);
  onError?: (error: Error, info: ErrorInfo) => void;
}

interface ErrorBoundaryState {
  error: Error | null;
}

export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    if (this.props.onError) {
      this.props.onError(error, info);
    } else {
      // eslint-disable-next-line no-console
      console.error("[v4 ErrorBoundary]", error, info.componentStack);
    }
  }

  reset = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    const { fallback } = this.props;
    if (typeof fallback === "function") {
      return fallback(error, this.reset);
    }
    if (fallback !== undefined) {
      return fallback;
    }

    return (
      <div
        role="alert"
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          minHeight: "60vh",
          padding: "1.5rem",
        }}
      >
        <div
          style={{
            maxWidth: "420px",
            padding: "1.25rem 1.5rem",
            borderRadius: "var(--radius-md)",
            border: "1px solid var(--danger)",
            background: "rgba(239, 68, 68, 0.08)",
            color: "var(--danger)",
            textAlign: "center",
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: "0.5rem" }}>
            Something went wrong rendering this view
          </div>
          <div
            style={{
              fontSize: "0.8125rem",
              opacity: 0.85,
              marginBottom: "1rem",
              wordBreak: "break-word",
            }}
          >
            {sanitizeErrorMessage(error.message)}
          </div>
          <button
            type="button"
            onClick={this.reset}
            style={{
              fontSize: "0.8125rem",
              padding: "0.375rem 0.875rem",
              background: "rgba(239, 68, 68, 0.15)",
              border: "1px solid var(--danger)",
              borderRadius: "var(--radius-sm)",
              color: "var(--danger)",
              cursor: "pointer",
            }}
          >
            Try again
          </button>
        </div>
      </div>
    );
  }
}

export default ErrorBoundary;
