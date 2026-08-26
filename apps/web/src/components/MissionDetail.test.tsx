import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { clampTracePage, MissionDetailContent } from "./MissionDetail";
import { TraceExplorer } from "./Trace";
import { decodeMissionDiagnosis, decodeTraceProjection } from "@/lib/insights/decode";
import { RUN_DIAGNOSTICS_FIXTURE, TRACE_FIXTURE } from "@/lib/insights/fixtures";
import type { TraceProjection } from "@/lib/insights/types";

const OUTAGE_MISSION = RUN_DIAGNOSTICS_FIXTURE.missions[1] as Record<string, unknown>;
const CATALOG_MISSION = RUN_DIAGNOSTICS_FIXTURE.missions[0] as Record<string, unknown>;

function renderMission(mission: Record<string, unknown>, trace: TraceProjection | null): string {
  return renderToStaticMarkup(
    <MissionDetailContent
      diagnosis={decodeMissionDiagnosis(mission)}
      trace={trace}
      tracePage={{ limit: 100, offset: 0 }}
    />,
  );
}

describe("<MissionDetailContent>", () => {
  it("leads with the merchant readable diagnosis, never a raw code", () => {
    const html = renderMission(CATALOG_MISSION, null);
    expect(html).toContain(
      "The buyer could not establish the required wattage attribute for the selected variant.",
    );
    expect(html).not.toContain(">ATTRIBUTE_NOT_PUBLISHED<");
    expect(html).toContain("You can fix this");
    expect(html).toContain("Your catalog");
  });

  it("addresses the exact compiler fact behind an attribute finding", () => {
    const html = renderMission(CATALOG_MISSION, null);
    expect(html).toContain(
      "/fixes/01a0aaaa-aaaa-7aaa-8aaa-aaaaaaaaaaa1#01a0bbbb-bbbb-7bbb-8bbb-bbbbbbbbbbb1",
    );
  });

  it("offers no compiler action on a provider outage", () => {
    const html = renderMission(OUTAGE_MISSION, null);
    expect(html).not.toContain("/fixes/");
  });

  it("presents a provider outage as external with no merchant action", () => {
    const html = renderMission(OUTAGE_MISSION, null);
    expect(html).toContain("The model provider never produced a usable response");
    expect(html).toContain("No action required from you");
    expect(html).not.toContain("You can fix this");
  });

  it("shows secondary observations beside the primary diagnosis", () => {
    const html = renderMission(CATALOG_MISSION, null);
    expect(html).toContain(
      "One throttled model provider invocation recovered within the mission. Operational history only.",
    );
  });

  it("reports unreported interaction counts as not reported rather than zero", () => {
    const noInteractions = { ...CATALOG_MISSION, model_invocations: null, tool_calls: null };
    const html = renderMission(noInteractions, null);
    expect(html).toContain("Not reported (no model trace)");
    expect(html).not.toContain("0 provider round trips");
  });

  it("renders reported interaction counts exactly as recorded", () => {
    const html = renderMission(CATALOG_MISSION, null);
    expect(html).toContain("5 provider round trips · 9 tool calls · 1 tool error");
  });

  it("attributes simulated demand to the mission, always labelled simulated", () => {
    const html = renderMission(CATALOG_MISSION, null);
    expect(html).toContain("Simulated lost demand: INR\u00a02,599.00");
  });

  it("lists commerce artifacts from evidence references", () => {
    const html = renderMission(CATALOG_MISSION, null);
    expect(html).toContain("Variant");
    expect(html).toContain("01997777-7777-7777-8777-777777777777");
  });
});

describe("<TraceExplorer>", () => {
  it("renders events in delivered order with type labels and verbatim redacted payloads", () => {
    const html = renderToStaticMarkup(
      <TraceExplorer trace={decodeTraceProjection(TRACE_FIXTURE)} />,
    );
    const request = html.indexOf("Model request");
    const response = html.indexOf("Model response");
    const toolCall = html.indexOf("Tool call");
    const providerError = html.indexOf("Provider error");
    expect(request).toBeGreaterThan(-1);
    expect(request).toBeLessThan(response);
    expect(response).toBeLessThan(toolCall);
    expect(toolCall).toBeLessThan(providerError);
    // The redaction marker survives untouched; nothing re-renders payloads as rich text.
    expect(html).toContain("[redacted]");
    expect(html.toLowerCase()).not.toContain("<markdown");
    expect(html).toContain("&quot;truncated&quot;: true");
    expect(html).toContain("search_products");
  });

  it("marks provider errors visually and textually apart", () => {
    const html = renderToStaticMarkup(
      <TraceExplorer trace={decodeTraceProjection(TRACE_FIXTURE)} />,
    );
    expect(html).toMatch(/data-tone="fail"[^>]*>Provider error|Provider error/);
    expect(html).toContain("429 rate limited, retrying");
  });

  it("renders an honest empty state for missions without traces", () => {
    const empty: TraceProjection = { total_events: 0, events: [] };
    const html = renderToStaticMarkup(<TraceExplorer trace={empty} />);
    expect(html).toContain("No trace events");
    expect(html).toContain("reference executor");
  });

  it("says so when the API returns an out of order page instead of silently trusting it", () => {
    const unordered = decodeTraceProjection(TRACE_FIXTURE);
    const swapped = [...unordered.events];
    const first = swapped[0] as NonNullable<(typeof swapped)[number]>;
    swapped[0] = swapped[1] as NonNullable<(typeof swapped)[number]>;
    swapped[1] = first;
    const html = renderToStaticMarkup(
      <TraceExplorer trace={{ total_events: unordered.total_events, events: swapped }} />,
    );
    expect(html).toContain("out of sequence order");
  });

  it("states how much of the total trace is shown", () => {
    const html = renderToStaticMarkup(
      <TraceExplorer trace={decodeTraceProjection(TRACE_FIXTURE)} />,
    );
    expect(html).toContain("6 of 6 event(s) shown");
  });
});

describe("clampTracePage", () => {
  it("defaults sensibly on absent or garbage parameters", () => {
    expect(clampTracePage(undefined, undefined)).toEqual({ limit: 100, offset: 0 });
    expect(clampTracePage("nine", "start")).toEqual({ limit: 100, offset: 0 });
  });

  it("clamps into the backend's documented bounds instead of triggering a 422", () => {
    expect(clampTracePage("10000", "-5")).toEqual({ limit: 500, offset: 0 });
    expect(clampTracePage("25", "40")).toEqual({ limit: 25, offset: 40 });
  });
});
