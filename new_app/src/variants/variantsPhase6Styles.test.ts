import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const variantsCss = readFileSync("src/variants/variants.css", "utf8");

describe("Phase 6 Variants responsive CSS contracts", () => {
  it("defines desktop, intermediate, compact, bottom-clearance, and reduced-motion layouts", () => {
    expect(variantsCss).toMatch(
      /grid-template-columns:\s*minmax\(220px,\s*250px\)\s+minmax\(500px,\s*1fr\)\s+minmax\(300px,\s*340px\)/
    );
    expect(variantsCss).toMatch(
      /@media \(min-width:\s*960px\) and \(max-width:\s*1359px\)[\s\S]*?grid-template-columns:\s*minmax\(480px,\s*1fr\)\s+minmax\(280px,\s*320px\)/
    );
    expect(variantsCss).toMatch(
      /@media \(max-width:\s*959px\)[\s\S]*?\.variant-workspace\s*\{[\s\S]*?grid-template-columns:\s*1fr/
    );
    expect(variantsCss).toMatch(
      /@media \(max-width:\s*760px\)[\s\S]*?padding-bottom:\s*calc\(82px \+ env\(safe-area-inset-bottom\)\)/
    );
    expect(variantsCss).toMatch(
      /@media \(prefers-reduced-motion:\s*reduce\)[\s\S]*?animation:\s*none/
    );
  });

  it("keeps tabs sticky and constrained tab and navigator strips horizontally usable", () => {
    expect(variantsCss).toMatch(
      /\.variant-editor-tablist\s*\{[\s\S]*?position:\s*sticky[\s\S]*?overflow-x:\s*auto/
    );
    expect(variantsCss).toMatch(
      /@media \(min-width:\s*960px\) and \(max-width:\s*1359px\)[\s\S]*?\.variant-navigator-list\s*\{[\s\S]*?display:\s*flex[\s\S]*?overflow-x:\s*auto/
    );
    expect(variantsCss).toMatch(
      /@media \(max-width:\s*959px\)[\s\S]*?\.variation-editor\s*\{[\s\S]*?overflow:\s*visible/
    );
    expect(variantsCss).toMatch(/\.variant-jump-preview\s*\{[\s\S]*?display:\s*none/);
    expect(variantsCss).toMatch(
      /@media \(max-width:\s*959px\)[\s\S]*?\.variant-jump-preview\s*\{[\s\S]*?display:\s*inline-flex/
    );
    expect(variantsCss).toMatch(
      /@media \(max-width:\s*760px\)[\s\S]*?\.variant-command-actions\s*\{[\s\S]*?grid-template-columns:\s*repeat\(2/
    );
  });
});
