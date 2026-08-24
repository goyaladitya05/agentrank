import { DecodeError } from "@/lib/insights/decode";

export interface CompilerEvidence {
  readonly field: string;
  readonly excerpt: string | null;
}

export interface CompilerReview {
  readonly review_id: string;
  readonly decision: "ACCEPT" | "CORRECT" | "REJECT";
  readonly correction: Record<string, unknown> | null;
  readonly reviewer: string;
  readonly created_at: string;
}

export interface CompilerCandidate {
  readonly candidate_id: string;
  readonly target: string;
  readonly product_or_variant: string;
  readonly attribute: string;
  readonly proposal: Record<string, unknown>;
  readonly attribute_kind: string | null;
  readonly unit: string | null;
  readonly state: string;
  readonly requires_correction: boolean;
  readonly evidence: readonly CompilerEvidence[];
  readonly review: CompilerReview | null;
}

export interface CompilerRun {
  readonly run_id: string;
  readonly source_snapshot_id: string;
  readonly source_label: string;
  readonly configuration_digest: string;
  readonly status: string;
  readonly created_at: string;
  readonly completed_at: string | null;
  readonly candidates: readonly CompilerCandidate[];
  readonly readiness: {
    readonly publishable: boolean;
    readonly blockers: readonly string[];
    readonly published_representation_id: string | null;
  };
}

export interface CompilerOverview {
  readonly current_representation_id: string | null;
  readonly review_required_count: number;
  readonly runs: readonly {
    readonly run_id: string;
    readonly source_snapshot_id: string;
    readonly source_label: string;
    readonly status: string;
    readonly created_at: string;
    readonly review_required_count: number;
    readonly reviewed_count: number;
    readonly published_representation_id: string | null;
  }[];
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

function bool(value: unknown, where: string): boolean {
  if (typeof value !== "boolean") throw new DecodeError(`${where}: expected a boolean`);
  return value;
}

function integer(value: unknown, where: string): number {
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new DecodeError(`${where}: expected an integer`);
  }
  return value;
}

function candidate(value: unknown): CompilerCandidate {
  const source = object(value, "compiler candidate");
  const evidence = source.evidence;
  if (!Array.isArray(evidence)) throw new DecodeError("candidate evidence: expected an array");
  const reviewValue = source.review;
  const review = reviewValue === null ? null : decodeReview(reviewValue);
  return {
    candidate_id: string(source.candidate_id, "candidate_id"),
    target: string(source.target, "target"),
    product_or_variant: string(source.product_or_variant, "product_or_variant"),
    attribute: string(source.attribute, "attribute"),
    proposal: object(source.proposal, "proposal"),
    attribute_kind: nullableString(source.attribute_kind, "attribute_kind"),
    unit: nullableString(source.unit, "unit"),
    state: string(source.state, "state"),
    requires_correction: bool(source.requires_correction, "requires_correction"),
    evidence: evidence.map((item) => {
      const entry = object(item, "evidence");
      return {
        field: string(entry.field, "evidence field"),
        excerpt: nullableString(entry.excerpt, "excerpt"),
      };
    }),
    review,
  };
}

function decodeReview(value: unknown): CompilerReview {
  const source = object(value, "compiler review");
  const decision = string(source.decision, "decision");
  if (decision !== "ACCEPT" && decision !== "CORRECT" && decision !== "REJECT") {
    throw new DecodeError("decision: unexpected value");
  }
  return {
    review_id: string(source.review_id, "review_id"),
    decision,
    correction: source.correction === null ? null : object(source.correction, "correction"),
    reviewer: string(source.reviewer, "reviewer"),
    created_at: string(source.created_at, "created_at"),
  };
}

export function decodeCompilerOverview(value: unknown): CompilerOverview {
  const source = object(value, "compiler overview");
  if (!Array.isArray(source.runs)) throw new DecodeError("runs: expected an array");
  return {
    current_representation_id: nullableString(
      source.current_representation_id,
      "current_representation_id",
    ),
    review_required_count: integer(source.review_required_count, "review_required_count"),
    runs: source.runs.map((item) => {
      const run = object(item, "compiler run summary");
      return {
        run_id: string(run.run_id, "run_id"),
        source_snapshot_id: string(run.source_snapshot_id, "source_snapshot_id"),
        source_label: string(run.source_label, "source_label"),
        status: string(run.status, "status"),
        created_at: string(run.created_at, "created_at"),
        review_required_count: integer(run.review_required_count, "review_required_count"),
        reviewed_count: integer(run.reviewed_count, "reviewed_count"),
        published_representation_id: nullableString(
          run.published_representation_id,
          "published_representation_id",
        ),
      };
    }),
  };
}

export function decodeCompilerRun(value: unknown): CompilerRun {
  const source = object(value, "compiler run");
  const readiness = object(source.readiness, "publish readiness");
  if (!Array.isArray(source.candidates) || !Array.isArray(readiness.blockers)) {
    throw new DecodeError("compiler run: expected candidate and blocker arrays");
  }
  return {
    run_id: string(source.run_id, "run_id"),
    source_snapshot_id: string(source.source_snapshot_id, "source_snapshot_id"),
    source_label: string(source.source_label, "source_label"),
    configuration_digest: string(source.configuration_digest, "configuration_digest"),
    status: string(source.status, "status"),
    created_at: string(source.created_at, "created_at"),
    completed_at: nullableString(source.completed_at, "completed_at"),
    candidates: source.candidates.map(candidate),
    readiness: {
      publishable: bool(readiness.publishable, "publishable"),
      blockers: readiness.blockers.map((item) => string(item, "blocker")),
      published_representation_id: nullableString(
        readiness.published_representation_id,
        "published_representation_id",
      ),
    },
  };
}
