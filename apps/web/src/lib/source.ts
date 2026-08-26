/**
 * The merchant source contracts the console reads, checked field by field.
 *
 * The same rule the rest of this console follows: these values decide what a merchant is told
 * about their own evidence, so nothing is cast and everything is checked. A decoder either
 * returns a fully typed value or throws a sentence naming the field that disagreed.
 *
 * `document` is the one field that stays unknown-shaped on purpose. It is the merchant's own
 * source document, the API already validated it against a strict schema before storing it, and
 * a second copy of that schema here would be a second thing to keep in step for no gain. It is
 * rendered as text and never as markup.
 */

import { DecodeError } from "@/lib/insights/decode";

export interface SourceField {
  readonly field: string;
  readonly excerpt: string;
  readonly truncated: boolean;
}

export interface SourceCompilerRun {
  readonly run_id: string;
  readonly status: string;
  readonly configuration_digest: string;
  readonly created_at: string;
  readonly completed_at: string | null;
  readonly error_code: string | null;
  readonly review_required_count: number;
  readonly reviewed_count: number;
  readonly published_representation_id: string | null;
}

export interface SourceSnapshotSummary {
  readonly source_snapshot_id: string;
  readonly source_label: string;
  readonly source_key: string;
  readonly source_version: number;
  readonly content_hash: string;
  readonly created_at: string;
  readonly origin: string;
  readonly product_count: number;
  readonly variant_count: number;
  readonly policy_count: number;
  readonly compiler_run_count: number;
  readonly published_representation_count: number;
  readonly is_current: boolean;
}

export interface SourceOverview {
  readonly current_source_snapshot_id: string | null;
  readonly snapshots: readonly SourceSnapshotSummary[];
}

export interface SourceSnapshot {
  readonly summary: SourceSnapshotSummary;
  readonly document: Record<string, unknown>;
  readonly fields: readonly SourceField[];
  readonly compiler_runs: readonly SourceCompilerRun[];
  readonly compilable: boolean;
  readonly existing_run_id: string | null;
}

function object(value: unknown, where: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new DecodeError(`${where}: expected an object`);
  }
  return value as Record<string, unknown>;
}

function string(value: unknown, where: string): string {
  if (typeof value !== "string") throw new DecodeError(`${where}: expected a string`);
  return value;
}

function nullableString(value: unknown, where: string): string | null {
  return value === null ? null : string(value, where);
}

function integer(value: unknown, where: string): number {
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new DecodeError(`${where}: expected an integer`);
  }
  return value;
}

function bool(value: unknown, where: string): boolean {
  if (typeof value !== "boolean") throw new DecodeError(`${where}: expected a boolean`);
  return value;
}

function list(value: unknown, where: string): unknown[] {
  if (!Array.isArray(value)) throw new DecodeError(`${where}: expected an array`);
  return value;
}

export function decodeSourceSummary(value: unknown): SourceSnapshotSummary {
  const source = object(value, "source snapshot summary");
  return {
    source_snapshot_id: string(source.source_snapshot_id, "source_snapshot_id"),
    source_label: string(source.source_label, "source_label"),
    source_key: string(source.source_key, "source_key"),
    source_version: integer(source.source_version, "source_version"),
    content_hash: string(source.content_hash, "content_hash"),
    created_at: string(source.created_at, "created_at"),
    origin: string(source.origin, "origin"),
    product_count: integer(source.product_count, "product_count"),
    variant_count: integer(source.variant_count, "variant_count"),
    policy_count: integer(source.policy_count, "policy_count"),
    compiler_run_count: integer(source.compiler_run_count, "compiler_run_count"),
    published_representation_count: integer(
      source.published_representation_count,
      "published_representation_count",
    ),
    is_current: bool(source.is_current, "is_current"),
  };
}

export function decodeSourceOverview(value: unknown): SourceOverview {
  const source = object(value, "source overview");
  return {
    current_source_snapshot_id: nullableString(
      source.current_source_snapshot_id,
      "current_source_snapshot_id",
    ),
    snapshots: list(source.snapshots, "snapshots").map(decodeSourceSummary),
  };
}

function decodeCompilerRun(value: unknown): SourceCompilerRun {
  const source = object(value, "compiler run");
  return {
    run_id: string(source.run_id, "run_id"),
    status: string(source.status, "status"),
    configuration_digest: string(source.configuration_digest, "configuration_digest"),
    created_at: string(source.created_at, "created_at"),
    completed_at: nullableString(source.completed_at, "completed_at"),
    error_code: nullableString(source.error_code, "error_code"),
    review_required_count: integer(source.review_required_count, "review_required_count"),
    reviewed_count: integer(source.reviewed_count, "reviewed_count"),
    published_representation_id: nullableString(
      source.published_representation_id,
      "published_representation_id",
    ),
  };
}

export function decodeSourceSnapshot(value: unknown): SourceSnapshot {
  const source = object(value, "source snapshot");
  return {
    summary: decodeSourceSummary(source.summary),
    document: object(source.document, "document"),
    fields: list(source.fields, "fields").map((item) => {
      const entry = object(item, "source field");
      return {
        field: string(entry.field, "field"),
        excerpt: string(entry.excerpt, "excerpt"),
        truncated: bool(entry.truncated, "truncated"),
      };
    }),
    compiler_runs: list(source.compiler_runs, "compiler_runs").map(decodeCompilerRun),
    compilable: bool(source.compilable, "compilable"),
    existing_run_id: nullableString(source.existing_run_id, "existing_run_id"),
  };
}

/** What one submission command did, as the API answered it. */
export interface SourceSubmission {
  readonly submission_id: string;
  readonly request_key: string;
  readonly created_snapshot: boolean;
  readonly snapshot: SourceSnapshotSummary;
}

export function decodeSourceSubmission(value: unknown): SourceSubmission {
  const source = object(value, "source submission");
  return {
    submission_id: string(source.submission_id, "submission_id"),
    request_key: string(source.request_key, "request_key"),
    created_snapshot: bool(source.created_snapshot, "created_snapshot"),
    snapshot: decodeSourceSummary(source.snapshot),
  };
}

/** Which mechanism supplied one snapshot, as a sentence rather than as a stored value. */
export function originLabel(origin: string): string {
  switch (origin) {
    case "MERCHANT_CONSOLE":
      return "Submitted in the console";
    case "MERCHANT_IMPORT":
      return "Imported from your own pages";
    case "OPERATOR_FIXTURE":
      return "Published by an operator";
    default:
      return origin;
  }
}

/** The document a merchant edits, as the text the form is prefilled with. */
export function documentText(document: Record<string, unknown>): string {
  return JSON.stringify(document, null, 2);
}
