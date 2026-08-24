import { InsightFailure } from "@/components/InsightFailure";
import { MissionDetailContent, clampTracePage } from "@/components/MissionDetail";
import { decodeMissionDiagnosis, decodeTraceProjection } from "@/lib/insights/decode";
import { loadInsight } from "@/lib/insights/load";

export const dynamic = "force-dynamic";

const UUID_SHAPE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const NOT_FOUND = {
  reason: "notFound",
  message:
    "Nothing at this address belongs to your merchant, or it does not exist. Cross merchant identifiers are indistinguishable from unknown ones.",
} as const;

export default async function MissionPage({
  params,
  searchParams,
}: {
  params: Promise<{ runId: string; missionRunId: string }>;
  searchParams: Promise<{ limit?: string; offset?: string }>;
}) {
  const { runId, missionRunId } = await params;
  const { limit, offset } = await searchParams;

  if (!UUID_SHAPE.test(runId) || !UUID_SHAPE.test(missionRunId)) {
    return <InsightFailure failure={NOT_FOUND} />;
  }

  const diagnosisOutcome = await loadInsight(
    `/api/v1/insights/runs/${encodeURIComponent(runId)}/missions/${encodeURIComponent(missionRunId)}`,
    decodeMissionDiagnosis,
  );
  if (!diagnosisOutcome.ok) {
    return <InsightFailure failure={diagnosisOutcome.failure} />;
  }

  // The trace is its own bounded read with its own address, so paging never refetches
  // the diagnosis and a long trace never inflates the first paint.
  const tracePage = clampTracePage(limit, offset);
  const traceOutcome = await loadInsight(
    `/api/v1/insights/runs/${encodeURIComponent(runId)}/missions/${encodeURIComponent(
      missionRunId,
    )}/trace?limit=${String(tracePage.limit)}&offset=${String(tracePage.offset)}`,
    decodeTraceProjection,
  );

  return (
    <MissionDetailContent
      diagnosis={diagnosisOutcome.data}
      trace={traceOutcome.ok ? traceOutcome.data : null}
      tracePage={tracePage}
    />
  );
}
