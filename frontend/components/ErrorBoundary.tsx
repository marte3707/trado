"use client";

import { Component, type ReactNode } from "react";

import { ErrorState } from "@/components/ui/error-state";

interface Props {
  children: ReactNode;
  fallback?: (error: Error, reset: () => void) => ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: { componentStack?: string | null }) {
    if (process.env.NODE_ENV !== "production") {
      console.error("[ErrorBoundary]", error, info);
    }
  }

  reset = () => {
    this.setState({ error: null });
  };

  render() {
    if (this.state.error) {
      if (this.props.fallback) {
        return this.props.fallback(this.state.error, this.reset);
      }
      return (
        <ErrorState
          title="Errore di rendering"
          message="Un componente ha lanciato un'eccezione mentre si visualizzava la pagina."
          detail={this.state.error}
          onRetry={this.reset}
        />
      );
    }
    return this.props.children;
  }
}
