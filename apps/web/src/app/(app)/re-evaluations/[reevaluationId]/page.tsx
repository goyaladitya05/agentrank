import { notFound } from "next/navigation";

import { InsightFailure } from "@/components/InsightFailure";
import { ReevaluationDetailContent } from "@/components/ReevaluationDetail";
import { ReevaluationRefresh } from "@/components/ReevaluationRefresh";
import { loadInsight } from "@/lib/insights/load";
import { decodeReevaluationDetail } from "@/lib/reevaluation";

export const dynamic = "force-dynamic";
export const metadata = { title: "Re-evaluation | AgentRank" };

const UUID_SHAPE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function ReevaluationPage({
  params,
}: {
  params: Promise<{ reevaluationId: string }>;
}) {
  const { reevaluationId } = await params;
  // A malformed identifier is an address for something that does not exist, not a server error.
  if (!UUID_SHAPE.test(reevaluationId)) {
    return notFound();
  }
  const result = await loadInsight(
    `/api/v1/benchmark/re-evaluations/${encodeURIComponent(reevaluationId)}`,
    decodeReevaluationDetail,
  );
  if (!result.ok) {
    return result.failure.reason === "notFound" ? (
      notFound()
    ) : (
      <InsightFailure failure={result.failure} />
    );
  }
  const launch = result.data;
  // The refresher is mounted beside the content rather than inside it, so the content stays a
  // pure function of its data and every state it can show is renderable in a test.
  return (
    <>
      <ReevaluationDetailContent launch={launch} />
      <ReevaluationRefresh active={launch.status === "QUEUED" || launch.status === "EXECUTING"} />
    </>
  );
}
