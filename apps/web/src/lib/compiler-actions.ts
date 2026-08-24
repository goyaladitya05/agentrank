"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import { requireConsoleApiKey } from "@/lib/auth/credential";
import { apiBaseUrl } from "@/lib/config";
import { decodeCompilerRun } from "@/lib/compiler";

async function command(path: string, body?: unknown): Promise<void> {
  const response = await fetch(`${apiBaseUrl().replace(/\/+$/, "")}${path}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${await requireConsoleApiKey()}`,
      "Content-Type": "application/json",
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    cache: "no-store",
  });
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok)
    throw new Error(`Compiler command was refused (HTTP ${String(response.status)}).`);
  decodeCompilerRun(payload);
}

export async function reviewCandidate(
  runId: string,
  candidateId: string,
  formData: FormData,
): Promise<void> {
  const decision = String(formData.get("decision") ?? "");
  if (decision === "correct") {
    const raw = String(formData.get("value") ?? "");
    const kind = String(formData.get("kind") ?? "");
    const value =
      kind === "INTEGER" || kind === "MEASUREMENT"
        ? Number(raw)
        : kind === "BOOLEAN"
          ? raw === "true"
          : raw;
    await command(`/api/v1/compiler/candidates/${encodeURIComponent(candidateId)}/correct`, {
      value,
      provenance_field: String(formData.get("provenance_field") ?? ""),
      provenance_excerpt: String(formData.get("provenance_excerpt") ?? "") || null,
    });
  } else if (decision === "accept" || decision === "reject") {
    await command(`/api/v1/compiler/candidates/${encodeURIComponent(candidateId)}/${decision}`);
  } else {
    throw new Error("Unknown compiler review action.");
  }
  revalidatePath(`/compiler/runs/${runId}`);
  revalidatePath("/compiler");
  redirect(`/compiler/runs/${runId}`);
}

export async function publishRun(runId: string): Promise<void> {
  await command(`/api/v1/compiler/runs/${encodeURIComponent(runId)}/publish`);
  revalidatePath(`/compiler/runs/${runId}`);
  revalidatePath("/compiler");
  revalidatePath("/overview");
  redirect(`/compiler/runs/${runId}`);
}
