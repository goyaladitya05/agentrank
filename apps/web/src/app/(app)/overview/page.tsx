import { InsightFailure } from "@/components/InsightFailure";
import { OverviewContent } from "@/components/OverviewContent";
import { decodeMerchantOverview } from "@/lib/insights/decode";
import { loadInsight } from "@/lib/insights/load";
import { decodePreflight } from "@/lib/evaluation";
import { loadLatestRunDiagnostics } from "@/lib/insights/latest-run";

export const dynamic = "force-dynamic";

export const metadata = { title: "Overview | AgentRank" };

export default async function OverviewPage() {
  const outcome = await loadInsight("/api/v1/insights/overview", decodeMerchantOverview);
  if (!outcome.ok) {
    return <InsightFailure failure={outcome.failure} />;
  }
  // The preflight decides the overview's next action: whether an evaluation is pending and
  // whether one can be launched. Its failure is not the overview's failure; the page still
  // renders every measured fact, with the action falling back to what the insight alone knows.
  const preflight = await loadInsight("/api/v1/benchmark/evaluations/preflight", decodePreflight);
  // The journey board comes from the latest finished run's own missions. Failing to read them
  // is not the overview's failure: every measured fact still renders, without the board.
  const latest = await loadLatestRunDiagnostics();
  return (
    <OverviewContent
      data={outcome.data}
      preflight={preflight.ok ? preflight.data : null}
      run={latest.state === "ok" ? latest.data : null}
    />
  );
}
