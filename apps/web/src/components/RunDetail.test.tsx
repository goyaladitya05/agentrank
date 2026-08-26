import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { RunDetailContent, isOutcomeFilter } from "./RunDetail";
import { decodeRunDiagnostics } from "@/lib/insights/decode";
import { RUN_DIAGNOSTICS_FIXTURE } from "@/lib/insights/fixtures";

function render(filter: Parameters<typeof RunDetailContent>[0]["filter"] = "ALL"): string {
  return renderToStaticMarkup(
    <RunDetailContent data={decodeRunDiagnostics(RUN_DIAGNOSTICS_FIXTURE)} filter={filter} />,
  );
}

describe("<RunDetailContent>", () => {
  it("renders run identity, designation and honest pins", () => {
    const html = render();
    expect(html).toContain("voltedge-core@2");
    expect(html).toContain("Development benchmark");
    expect(html).toContain("Not independent evaluation evidence.");
    expect(html).toContain("matches the pinned catalog");
    expect(html).toContain("sha256:engine0000");
  });

  it("renders summary metrics with their denominators", () => {
    const html = render();
    expect(html).toContain("(8 of 8 purchase missions)");
    expect(html).toContain("(4 of 6 controls)");
    expect(html).toContain("2 failed missions");
  });

  it("separates provider health from merchant findings and asks no merchant action", () => {
    const html = render();
    expect(html).toContain(
      "1 mission(s) ended on a provider outage. No merchant action is required.",
    );
    expect(html).toContain("Operational history only.");
  });

  it("lists missions with primary diagnosis, owner and provider marks", () => {
    const html = render();
    expect(html).toContain("mission.usb-c.charger");
    expect(html).toContain(
      "The buyer could not establish the required wattage attribute for the selected variant.",
    );
    expect(html).toContain("Your catalog");
    // The outage mission leads with its real cause, never the stored agent error label.
    expect(html).not.toContain("Agent Execution Error");
    expect(html).toContain("Provider outage");
    expect(html).toContain("Throttle recovered");
  });

  it("filters missions by outcome through links that keep the address shareable", () => {
    const html = render("FAILED");
    expect(html).toContain("/runs/01992222-2222-7222-8222-222222222222?outcome=FAILED");
    expect(html).toContain("2 of 3 mission(s)");
    const filtered = renderToStaticMarkup(
      <RunDetailContent
        data={decodeRunDiagnostics({
          ...RUN_DIAGNOSTICS_FIXTURE,
          missions: RUN_DIAGNOSTICS_FIXTURE.missions.filter(
            (mission) => mission.status === "ABSTAINED",
          ),
        })}
        filter="ERRORED"
      />,
    );
    expect(filtered).toContain("No Errored missions");
  });

  it("rejects unknown filters so an arbitrary query string cannot change behavior", () => {
    expect(isOutcomeFilter("ALL")).toBe(true);
    expect(isOutcomeFilter("SUCCEEDED")).toBe(true);
    expect(isOutcomeFilter("DROP")).toBe(false);
    expect(isOutcomeFilter(undefined)).toBe(false);
  });

  it("links a finding to the exact compiler fact behind the representation it tested", () => {
    const html = render();
    expect(html).toContain(
      "/fixes/01a0aaaa-aaaa-7aaa-8aaa-aaaaaaaaaaa1#01a0bbbb-bbbb-7bbb-8bbb-bbbbbbbbbbb1",
    );
    expect(html).toContain("variant.VE-CHG-100-BLK.attribute.wattage");
  });

  it("offers no compiler action on findings the API could not address", () => {
    const stripped = {
      ...RUN_DIAGNOSTICS_FIXTURE,
      findings: RUN_DIAGNOSTICS_FIXTURE.findings.map((finding) => ({
        ...finding,
        compiler_references: [],
      })),
    };
    const html = renderToStaticMarkup(
      <RunDetailContent data={decodeRunDiagnostics(stripped)} filter="ALL" />,
    );
    expect(html).not.toContain("/fixes/");
    expect(html).not.toContain("Compiler facts behind the representation this run tested");
    // The finding itself is unchanged: no link is not no problem.
    expect(html).toContain("wattage");
  });

  it("renders simulated demand per currency without summing", () => {
    const html = render();
    expect(html).toContain("INR\u00a021,000.00");
    expect(html).toContain("one row per currency");
  });
});
