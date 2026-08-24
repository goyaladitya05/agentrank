import { ErrorState } from "@/components/Primitives";
import type { InsightsFailure } from "@/lib/insights/load";

/** Renders any insights failure with the honest next step for its kind. */
export function InsightFailure({ failure }: { failure: InsightsFailure }) {
  switch (failure.reason) {
    case "unauthenticated":
    case "forbidden":
      return <ErrorState title="Sign in required" explanation={failure.message} kind="auth" />;
    case "notFound":
      return <ErrorState title="Not found" explanation={failure.message} kind="missing" />;
    case "apiError":
      return (
        <ErrorState
          title="The AgentRank API reported a problem"
          explanation={`${failure.message} The request reached the API and it answered with HTTP ${String(
            failure.status,
          )}.`}
          kind="retry"
        />
      );
    case "networkError":
      return (
        <ErrorState
          title="Could not reach the AgentRank API"
          explanation={failure.message}
          kind="retry"
        />
      );
    case "invalidResponse":
      return (
        <ErrorState title="Unreadable API response" explanation={failure.message} kind="retry" />
      );
  }
}
