import { InsightFailure } from "@/components/InsightFailure";
import { OverviewContent } from "@/components/OverviewContent";
import { decodeMerchantOverview } from "@/lib/insights/decode";
import { loadInsight } from "@/lib/insights/load";

export const dynamic = "force-dynamic";

export const metadata = { title: "Overview | AgentRank" };

export default async function OverviewPage() {
  const outcome = await loadInsight("/api/v1/insights/overview", decodeMerchantOverview);
  if (!outcome.ok) {
    return <InsightFailure failure={outcome.failure} />;
  }
  return <OverviewContent data={outcome.data} />;
}
