import { InsightFailure } from "@/components/InsightFailure";
import { RunDetailContent, isOutcomeFilter } from "@/components/RunDetail";
import { decodeRunDiagnostics } from "@/lib/insights/decode";
import { loadInsight } from "@/lib/insights/load";

export const dynamic = "force-dynamic";

const UUID_SHAPE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function RunPage({
  params,
  searchParams,
}: {
  params: Promise<{ runId: string }>;
  searchParams: Promise<{ outcome?: string }>;
}) {
  const { runId } = await params;
  const { outcome } = await searchParams;

  // A malformed identifier is not a server error; it is an address for something that
  // does not exist, and the API's structured 404 shape is what it should read as.
  if (!UUID_SHAPE.test(runId)) {
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

  const result = await loadInsight(
    `/api/v1/insights/runs/${encodeURIComponent(runId)}`,
    decodeRunDiagnostics,
  );
  if (!result.ok) {
    return <InsightFailure failure={result.failure} />;
  }

  return (
    <RunDetailContent data={result.data} filter={isOutcomeFilter(outcome) ? outcome : "ALL"} />
  );
}
