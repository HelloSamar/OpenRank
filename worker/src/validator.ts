/**
 * Quiz question validator: the same rules as `validate_question` in quiz_bot.py.
 * Both are tested against tests/fixtures/quiz_validation_cases.json.
 *
 * Telegram limits: question 1-300 characters, 2-10 options of 1-100 characters, explanation
 * up to 200 characters with at most 2 line breaks. Characters are counted as Unicode code
 * points, like Python, so emoji count as one.
 */
export const LIMITS = {
  question: 300,
  questionPrefixReserve: 12, // room for "Q12/100: " in front of the question
  option: 100,
  explanation: 200,
  optionsMin: 2,
  optionsMax: 10,
  explanationLineBreaksMax: 2,
} as const;

const length = (text: string): number => [...text].length;

/** Returns "" when the question can be posted, otherwise the reason it cannot. */
export function validateQuestion(item: unknown, showNumbers = true): string {
  if (!Array.isArray(item) || (item.length !== 3 && item.length !== 4)) {
    return "entry must be [question, [options], correct_index, optional explanation]";
  }
  const [question, options, correctIndex] = item as unknown[];

  if (typeof question !== "string" || question.trim() === "") {
    return "question must be a non-empty string";
  }
  const maxQuestion = LIMITS.question - (showNumbers ? LIMITS.questionPrefixReserve : 0);
  if (length(question) > maxQuestion) return `question exceeds ${maxQuestion} characters`;

  if (!Array.isArray(options)) return "options must be a list";
  if (options.length < LIMITS.optionsMin || options.length > LIMITS.optionsMax) {
    return `need ${LIMITS.optionsMin}-${LIMITS.optionsMax} options, got ${options.length}`;
  }
  for (const option of options) {
    if (typeof option !== "string" || option.trim() === "") {
      return "every option must be a non-empty string";
    }
    if (length(option) > LIMITS.option) return `an option exceeds ${LIMITS.option} characters`;
  }
  if (new Set(options.map((option: string) => option.trim())).size !== options.length) {
    return "options must all be different (Telegram does not accept identical options)";
  }

  if (typeof correctIndex !== "number" || !Number.isInteger(correctIndex)) {
    return "correct_index must be an integer";
  }
  if (correctIndex < 0 || correctIndex >= options.length) {
    return `correct_index ${correctIndex} is out of range (0-${options.length - 1})`;
  }

  if (item.length === 4) {
    const explanation: unknown = item[3];
    if (typeof explanation !== "string") return "explanation must be text";
    if (length(explanation) > LIMITS.explanation) {
      return `explanation exceeds ${LIMITS.explanation} characters`;
    }
    if (explanation.split("\n").length - 1 > LIMITS.explanationLineBreaksMax) {
      return `explanation can have at most ${LIMITS.explanationLineBreaksMax} line breaks`;
    }
  }
  return "";
}
