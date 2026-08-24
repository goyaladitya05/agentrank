import { InsightFailure } from "@/components/InsightFailure";
import { ExperimentDetailContent } from "@/components/ExperimentDetail";
import { decodeExperimentComparison } from "@/lib/insights/decode";
import { loadInsight } from "@/lib/insights/load";

export const dynamic = "force-dynamic";

const UUID_SHAPE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function ExperimentPage({
  params,
}: {
  params: Promise<{ experimentId: string }>;
}) {
  const { experimentId } = await params;
  if (!UUID_SHAPE.test(experimentId)) {
    return (
      <InsightFailure
        failure={{
          reason: "notFound",
          message:
            "Nothing at this address belongs to your merchant, or it does not exist. Cross merchant identifiers are indistinguishable from unknown ones.",
        }}
      />
    );
  }

  const outcome = await loadInsight(
    `/api/v1/insights/experiments/${encodeURIComponent(experimentId)}`,
    decodeExperimentComparison,
  );
  if (!outcome.ok) {
    return <InsightFailure failure={outcome.failure} />;
  }
  return <ExperimentDetailContent data={outcome.data} />;
}
