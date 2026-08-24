/**
 * Reading mission diagnoses the way the product presents them.
 *
 * The backend decides which code leads a diagnosis; this module resolves that code back
 * to its finding so the console can show the finding's merchant readable summary as the
 * primary line, and never a raw enum value where a sentence belongs.
 */

import { humanize } from "@/lib/labels";
import type { MerchantFinding, MissionDiagnosis, MissionFinding } from "@/lib/insights/types";

export function primaryFinding(diagnosis: MissionDiagnosis): MissionFinding | null {
  if (diagnosis.primary_code === null) {
    return null;
  }
  return diagnosis.findings.find((finding) => finding.code === diagnosis.primary_code) ?? null;
}

/** The text shown in a mission table's primary diagnosis cell. */
export function primaryDiagnosisText(diagnosis: MissionDiagnosis): string {
  const finding = primaryFinding(diagnosis);
  if (finding !== null) {
    return finding.summary;
  }
  return diagnosis.primary_code === null
    ? "No diagnosis recorded"
    : humanize(diagnosis.primary_code);
}

export function ownerOfPrimary(diagnosis: MissionDiagnosis): string | null {
  const finding = primaryFinding(diagnosis);
  return finding === null ? null : finding.owner;
}

/**
 * A provider fault marker for a mission table. Terminating outages lead their
 * diagnoses; recovered throttles are secondary history and are marked as such.
 */
export function providerFaultMark(diagnosis: MissionDiagnosis): string | null {
  if (diagnosis.primary_code === "PROVIDER_OUTAGE_TERMINATED_MISSION") {
    return "Provider outage";
  }
  const throttle = diagnosis.findings.find(
    (finding: MerchantFinding | MissionFinding) => finding.code === "PROVIDER_THROTTLE_RECOVERED",
  );
  return throttle === undefined || throttle === null ? null : "Throttle recovered";
}
