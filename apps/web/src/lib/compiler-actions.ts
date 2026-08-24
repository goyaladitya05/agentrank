"use server";

import { revalidatePath } from "next/cache";

import { requireConsoleApiKey } from "@/lib/auth/credential";
import { apiBaseUrl } from "@/lib/config";
import { decodeCompilerRun } from "@/lib/compiler";

export interface CompilerMutationState {
  readonly ok: boolean;
  readonly message: string | null;
}

async function command(path: string, body?: unknown): Promise<CompilerMutationState> {
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
  if (!response.ok) {
    const detail =
      typeof payload === "object" && payload !== null && "detail" in payload
        ? (payload as { detail?: unknown }).detail
        : null;
    return {
      ok: false,
      message:
        typeof detail === "string"
          ? detail
          : `The compiler command was refused (HTTP ${String(response.status)}). Refresh to see current review state.`,
    };
  }
  try {
    decodeCompilerRun(payload);
  } catch {
    return {
      ok: false,
      message: "The compiler returned an unreadable response. Refresh and try again.",
    };
  }
  return { ok: true, message: null };
}

export async function reviewCandidate(
  runId: string,
  candidateId: string,
  _: CompilerMutationState,
  formData: FormData,
): Promise<CompilerMutationState> {
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
    if (
      (kind === "BOOLEAN" && raw !== "true" && raw !== "false") ||
      (!Number.isFinite(value as number) && (kind === "INTEGER" || kind === "MEASUREMENT"))
    ) {
      return { ok: false, message: "Enter a valid correction value." };
    }
    const result = await command(
      `/api/v1/compiler/candidates/${encodeURIComponent(candidateId)}/correct`,
      {
        value,
        provenance_field: String(formData.get("provenance_field") ?? ""),
        provenance_excerpt: String(formData.get("provenance_excerpt") ?? "") || null,
      },
    );
    if (!result.ok) return result;
  } else if (decision === "accept" || decision === "reject") {
    const result = await command(
      `/api/v1/compiler/candidates/${encodeURIComponent(candidateId)}/${decision}`,
    );
    if (!result.ok) return result;
  } else {
    return { ok: false, message: "Unknown compiler review action." };
  }
  revalidatePath(`/compiler/runs/${runId}`);
  revalidatePath("/compiler");
  return { ok: true, message: null };
}

export async function reviewCandidateForm(
  runId: string,
  candidateId: string,
  formData: FormData,
): Promise<void> {
  const result = await reviewCandidate(runId, candidateId, { ok: false, message: null }, formData);
  if (!result.ok) throw new Error(result.message ?? "The compiler command was refused.");
}

export async function publishRun(
  runId: string,
  _: CompilerMutationState,
): Promise<CompilerMutationState> {
  const result = await command(`/api/v1/compiler/runs/${encodeURIComponent(runId)}/publish`);
  if (!result.ok) return result;
  revalidatePath(`/compiler/runs/${runId}`);
  revalidatePath("/compiler");
  revalidatePath("/overview");
  return { ok: true, message: null };
}
