/**
 * Compiler review fixtures shaped exactly like the compiler API's JSON responses.
 *
 * Copied from a real `GET /api/v1/compiler/runs/{id}` answer for the VoltEdge source, so a
 * component test exercises the wire shape the console receives rather than an approximation of
 * it. Nothing here is imported by a route or a shipped component.
 */

export const COMPILER_RUN_FIXTURE = {
  run_id: "01a03000-0000-7000-8000-000000000001",
  source_snapshot_id: "01a03000-0000-7000-8000-000000000002",
  source_label: "voltedge-merchant-source@2",
  configuration_digest: `sha256:${"c".repeat(64)}`,
  status: "COMPLETED",
  created_at: "2026-08-24T10:00:00Z",
  completed_at: "2026-08-24T10:00:04Z",
  candidates: [
    {
      candidate_id: "01a03000-0000-7000-8000-00000000000a",
      target: "variant.VE-CHG-100-BLK.attribute.wattage",
      product_or_variant: "variant.VE-CHG-100-BLK",
      attribute: "wattage",
      proposal: {
        target: "variant.VE-CHG-100-BLK.attribute.wattage",
        fact: {
          value: 0,
          authority: "DERIVED",
          confidence: "REVIEW_REQUIRED",
          review_state: "REVIEW_REQUIRED",
          provenance: [{ field: "products[VE-CHG-100].title", excerpt: "100W" }],
        },
        attribute_kind: "MEASUREMENT",
        unit: "W",
        requires_correction: true,
      },
      proposed_value: 0,
      authority: "DERIVED",
      confidence: "REVIEW_REQUIRED",
      attribute_kind: "MEASUREMENT",
      unit: "W",
      state: "REVIEW_REQUIRED",
      requires_correction: true,
      evidence: [{ field: "products[VE-CHG-100].title", excerpt: "100W" }],
      review: null,
    },
    {
      candidate_id: "01a03000-0000-7000-8000-00000000000b",
      target: "variant.VE-CBL-USBC-1M.compatibility.usb-c-pd",
      product_or_variant: "variant.VE-CBL-USBC-1M",
      attribute: "usb-c-pd",
      proposal: {
        target: "variant.VE-CBL-USBC-1M.compatibility.usb-c-pd",
        fact: {
          value: "TRUE",
          authority: "DERIVED",
          confidence: "REVIEW_REQUIRED",
          review_state: "REVIEW_REQUIRED",
          provenance: [{ field: "products[VE-CBL-USBC].description", excerpt: "USB-PD" }],
        },
        attribute_kind: null,
        unit: null,
        requires_correction: false,
      },
      proposed_value: "TRUE",
      authority: "DERIVED",
      confidence: "REVIEW_REQUIRED",
      attribute_kind: null,
      unit: null,
      state: "REVIEW_REQUIRED",
      requires_correction: false,
      evidence: [{ field: "products[VE-CBL-USBC].description", excerpt: "USB-PD" }],
      review: null,
    },
    {
      candidate_id: "01a03000-0000-7000-8000-00000000000c",
      target: "variant.VE-CBL-USBC-1M.attribute.length",
      product_or_variant: "variant.VE-CBL-USBC-1M",
      attribute: "length",
      proposal: {
        target: "variant.VE-CBL-USBC-1M.attribute.length",
        fact: {
          value: 1,
          authority: "DERIVED",
          confidence: "HIGH",
          review_state: "CONFIRMED",
          provenance: [
            { field: "products[VE-CBL-USBC].variants[VE-CBL-USBC-1M].label", excerpt: "1 m" },
          ],
        },
        attribute_kind: "MEASUREMENT",
        unit: "m",
        requires_correction: false,
      },
      proposed_value: 1,
      authority: "DERIVED",
      confidence: "HIGH",
      attribute_kind: "MEASUREMENT",
      unit: "m",
      state: "ACCEPTED",
      requires_correction: false,
      evidence: [{ field: "products[VE-CBL-USBC].variants[VE-CBL-USBC-1M].label", excerpt: "1 m" }],
      review: null,
    },
  ],
  readiness: {
    publishable: false,
    blockers: ["2 fact(s) still require review."],
    published_representation_id: null,
  },
};

export const COMPILER_OVERVIEW_FIXTURE = {
  current_representation_id: null,
  review_required_count: 2,
  runs: [
    {
      run_id: COMPILER_RUN_FIXTURE.run_id,
      source_snapshot_id: COMPILER_RUN_FIXTURE.source_snapshot_id,
      source_label: COMPILER_RUN_FIXTURE.source_label,
      status: "COMPLETED",
      created_at: "2026-08-24T10:00:00Z",
      review_required_count: 2,
      reviewed_count: 0,
      published_representation_id: null,
    },
  ],
};

export const CORRECTION_REVIEW_FIXTURE = {
  review_id: "01a03000-0000-7000-8000-0000000000f1",
  decision: "CORRECT",
  correction: {
    target: "variant.VE-CHG-100-BLK.attribute.wattage",
    fact: {
      value: 65,
      authority: "DERIVED",
      confidence: "HIGH",
      review_state: "CONFIRMED",
      provenance: [{ field: "products[VE-CHG-100].description", excerpt: "65W" }],
    },
    attribute_kind: "MEASUREMENT",
    unit: "W",
    requires_correction: false,
  },
  reviewer: "MERCHANT_CREDENTIAL",
  created_at: "2026-08-24T10:05:00Z",
};

export const REJECT_REVIEW_FIXTURE = {
  review_id: "01a03000-0000-7000-8000-0000000000f2",
  decision: "REJECT",
  correction: null,
  reviewer: "MERCHANT_CREDENTIAL",
  created_at: "2026-08-24T10:06:00Z",
};
