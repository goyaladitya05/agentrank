import { notFound } from "next/navigation";

import { InsightFailure } from "@/components/InsightFailure";
import { EvaluationLaunchDetailContent } from "@/components/EvaluationLaunchDetail";
import { EvaluationRefresh } from "@/components/EvaluationRefresh";
import { loadInsight } from "@/lib/insights/load";
import { decodeEvaluationLaunchDetail } from "@/lib/evaluation";

export const dynamic = "force-dynamic";
export const metadata = { title: "Re-evaluation | AgentRank" };

const UUID_SHAPE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function EvaluationLaunchPage({
  params,
}: {
  params: Promise<{ launchId: string }>;
}) {
  const { launchId } = await params;
  // A malformed identifier is an address for something that does not exist, not a server error.
  if (!UUID_SHAPE.test(launchId)) {
    return notFound();
  }
  const result = await loadInsight(
    `/api/v1/benchmark/evaluations/${encodeURIComponent(launchId)}`,
    decodeEvaluationLaunchDetail,
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
      <EvaluationLaunchDetailContent launch={launch} />
      <EvaluationRefresh active={launch.status === "QUEUED" || launch.status === "EXECUTING"} />
    </>
  );
}
