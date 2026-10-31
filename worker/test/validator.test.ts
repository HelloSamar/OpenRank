import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { LIMITS, validateQuestion } from "../src/validator";

const fixture = JSON.parse(
  readFileSync(new URL("../../tests/fixtures/quiz_validation_cases.json", import.meta.url), "utf8"),
) as {
  limits: Record<string, number>;
  cases: { name: string; item: unknown; valid: boolean; problem_contains?: string }[];
};

describe("quiz validator: the cases shared with the Python bot", () => {
  it("has the cases", () => expect(fixture.cases.length).toBeGreaterThanOrEqual(30));

  for (const testCase of fixture.cases) {
    it(testCase.name, () => {
      const problem = validateQuestion(testCase.item);
      if (testCase.valid) {
        expect(problem).toBe("");
      } else {
        expect(problem).not.toBe("");
        expect(problem).toContain(testCase.problem_contains ?? "");
      }
    });
  }

  it("uses the limits written in the fixture", () => {
    expect(LIMITS.question).toBe(fixture.limits.question);
    expect(LIMITS.questionPrefixReserve).toBe(fixture.limits.question_prefix_reserve);
    expect(LIMITS.option).toBe(fixture.limits.option);
    expect(LIMITS.explanation).toBe(fixture.limits.explanation);
    expect(LIMITS.optionsMin).toBe(fixture.limits.options_min);
    expect(LIMITS.optionsMax).toBe(fixture.limits.options_max);
    expect(LIMITS.explanationLineBreaksMax).toBe(fixture.limits.explanation_line_breaks_max);
  });
});

describe("quiz validator: extra cases", () => {
  it("counts an emoji as one character, like Python", () => {
    expect(validateQuestion(["\u{1F600}".repeat(288), ["a", "b"], 0])).toBe("");
    expect(validateQuestion(["\u{1F600}".repeat(289), ["a", "b"], 0])).toContain("question exceeds");
  });

  it("allows 300 characters when question numbers are not shown", () => {
    expect(validateQuestion(["q".repeat(300), ["a", "b"], 0], false)).toBe("");
    expect(validateQuestion(["q".repeat(301), ["a", "b"], 0], false)).toContain("exceeds 300");
  });

  it("refuses true/false/null and NaN as an index", () => {
    for (const bad of [true, false, null, Number.NaN, undefined, Infinity]) {
      expect(validateQuestion(["Q?", ["a", "b"], bad])).toContain("integer");
    }
  });

  it("never throws, whatever it is given", () => {
    for (const junk of [null, undefined, 5, "text", {}, [], [null], [[], [], []], ["Q", {}, 0], ["Q", ["a", "b"], 0, null]]) {
      expect(typeof validateQuestion(junk)).toBe("string");
    }
  });
});
