/**
 * The translation layer, and the ratchet that keeps it the only one.
 *
 * Two things are tested here, and the second is the one that matters over time:
 *
 *  1. `translate()` behaves when a string is missing — the case that decides
 *     whether a half-migrated UI looks broken or looks English.
 *  2. The number of inline bilingual ternaries in `src/` does not grow. A
 *     codebase that is 510/510 today and 512 tomorrow has not been migrated, it
 *     has been *re*-forked, and nothing else in the suite would notice.
 */

import { describe, it, expect, vi } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { directionFor, STRINGS, translate } from "./strings";

// ---------------------------------------------------------------------------
// The table
// ---------------------------------------------------------------------------
describe("the translation table", () => {
  it("has the same keys in both languages", () => {
    // A key present in one language and missing in the other is the defect the
    // fallback hides: it renders English inside an Arabic page and nobody
    // notices until a user does.
    const en = Object.keys(STRINGS.en).sort();
    const ar = Object.keys(STRINGS.ar).sort();
    expect(ar).toEqual(en);
  });

  it("has no empty strings", () => {
    for (const [language, table] of Object.entries(STRINGS)) {
      for (const [key, value] of Object.entries(table)) {
        expect(value.trim(), `${language}:${key} is empty`).not.toBe("");
      }
    }
  });

  it("really is Arabic for the Arabic entries", () => {
    // Guards against a copy-paste migration that left English on both sides.
    const arabic = Object.entries(STRINGS.ar).filter(
      ([key]) => !key.startsWith("common.language_"),
    );
    expect(arabic.length).toBeGreaterThan(20);
    const withArabicScript = arabic.filter(([, value]) => /[\u0600-\u06FF]/.test(value));
    expect(withArabicScript.length).toBeGreaterThanOrEqual(arabic.length - 2);
  });
});

// ---------------------------------------------------------------------------
// Lookup behaviour
// ---------------------------------------------------------------------------
describe("translate", () => {
  it("returns the string for the requested language", () => {
    expect(translate("en", "security.active")).toBe("ACTIVE");
    expect(translate("ar", "security.active")).toBe("نشط");
  });

  it("falls back to English and says so when a translation is missing", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const table = { ...STRINGS.ar };
    delete STRINGS.ar["security.active"];

    try {
      // Visible and in the wrong language, rather than empty or a raw key: an
      // empty label in a HUD panel reads as a rendering bug.
      expect(translate("ar", "security.active")).toBe("ACTIVE");
      expect(warn).toHaveBeenCalledWith(expect.stringContaining("security.active"));
    } finally {
      STRINGS.ar["security.active"] = table["security.active"];
      warn.mockRestore();
    }
  });

  it("returns the key rather than nothing when no language has it", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      expect(translate("en", "nope.missing")).toBe("nope.missing");
    } finally {
      warn.mockRestore();
    }
  });

  it("fills {placeholders} and leaves unknown ones alone", () => {
    STRINGS.en["test.greeting"] = "Hello {name}";
    STRINGS.ar["test.greeting"] = "أهلاً {name}";
    try {
      expect(translate("en", "test.greeting", { name: "Zaytoon" })).toBe("Hello Zaytoon");
      expect(translate("ar", "test.greeting", { name: "زيتون" })).toBe("أهلاً زيتون");
      // An unfilled placeholder stays visible: it is a bug in the call site and
      // should look like one, not silently become an empty string.
      expect(translate("en", "test.greeting")).toBe("Hello {name}");
    } finally {
      delete STRINGS.en["test.greeting"];
      delete STRINGS.ar["test.greeting"];
    }
  });

  it("treats an unknown language as English instead of throwing", () => {
    expect(translate("fr", "security.active")).toBe("ACTIVE");
  });
});

describe("directionFor", () => {
  it("is RTL for Arabic and LTR for everything else", () => {
    expect(directionFor("ar")).toBe("rtl");
    expect(directionFor("en")).toBe("ltr");
    expect(directionFor(undefined)).toBe("ltr");
  });
});

// ---------------------------------------------------------------------------
// The ratchet
// ---------------------------------------------------------------------------
//: How many `ar ? "…" : "…"` expressions exist outside `src/i18n/`.
//:
//: Measured, not estimated. This number may only go DOWN: every migration
//: lowers it, and the test is what stops a new component from re-introducing
//: the pattern that this module exists to replace. Raise it only with the
//: migration that justifies the raise — never to make a red test green.
//: Counted with `/\bar\s*\?\s*"/`, which catches the multi-line form
//: (`ar\n  ? "…"`) as well as the single-line one.
const INLINE_BILINGUAL_TERNARIES = 597;

function sourceFiles(dir, acc = []) {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      if (entry === "i18n" || entry === "node_modules") continue;
      sourceFiles(path, acc);
    } else if (/\.(jsx?|tsx?)$/.test(entry)) {
      acc.push(path);
    }
  }
  return acc;
}

describe("the ratchet", () => {
  it("does not gain inline bilingual ternaries", () => {
    const files = sourceFiles(join(process.cwd(), "src"));
    expect(files.length).toBeGreaterThan(30);

    let count = 0;
    const offenders = [];
    for (const file of files) {
      const hits = (readFileSync(file, "utf8").match(/\bar\s*\?\s*"/g) || []).length;
      if (hits) {
        count += hits;
        offenders.push(`${file.replace(`${process.cwd()}/`, "")}: ${hits}`);
      }
    }

    // The message matters as much as the assertion: it is the migration list.
    expect(
      count,
      `inline bilingual ternaries grew to ${count} (limit ${INLINE_BILINGUAL_TERNARIES}).\n` +
        `Move the new strings into src/i18n/strings.js and call t(...).\n` +
        `Largest remaining files:\n${offenders.slice(0, 8).join("\n")}`,
    ).toBeLessThanOrEqual(INLINE_BILINGUAL_TERNARIES);
  });

  it("keeps the migrated surface migrated", () => {
    // SecurityView is the proof that the mechanism works end to end; if a
    // future edit puts a sentence back into it, the demonstration is gone.
    //
    // Locale *selectors* are exempt and there are exactly two — `[ar ? "ar" :
    // "en"]` chooses a key in a lookup table, and translating it would turn a
    // key lookup into a sentence. They are named in the comment above the `t`
    // definition so the next reader does not "fix" them.
    const source = readFileSync(join(process.cwd(), "src/components/SecurityView.jsx"), "utf8");
    const ternaries = (source.match(/\bar\s*\?\s*"(?!ar"|en"|ltr"|rtl")/g) || []).length;
    expect(ternaries, "SecurityView went back to inline strings").toBe(0);
    expect(source).toContain('from "../i18n/strings"');
    expect((source.match(/t\("security\./g) || []).length).toBeGreaterThan(20);
  });
});
