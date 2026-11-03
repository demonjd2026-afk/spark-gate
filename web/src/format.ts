import type { Check, Decision } from "./types";

/** Round half to even, as Python does, so numbers match the gate's own reports. */
function roundEven(v: number, digits: number): string {
  const f = 10 ** digits;
  const n = v * f;
  const r = Math.round(n);
  const even = Math.abs(n % 1) === 0.5 ? (r % 2 === 0 ? r : r - 1) : r;
  return (even / f).toFixed(digits);
}

export const pct = (x: number | null | undefined, digits = 0) =>
  x == null ? "–" : `${roundEven(x * 100, digits)}%`;

export const ratio = (x: number | null | undefined) => (x == null ? "–" : `${roundEven(x, 2)}×`);

export const usd = (x: number) => (x < 0.01 ? `$${x.toFixed(4)}` : `$${x.toFixed(2)}`);

export const seconds = (ms: number) => `${(ms / 1000).toFixed(1)} s`;

export function checkValue(c: Pick<Check, "name" | "actual">): string {
  if (c.actual == null) return "–";
  if (c.name.includes("ratio")) return ratio(c.actual);
  if (c.name === "run errors") return String(Math.round(c.actual));
  return pct(c.actual);
}

export function thresholdText(c: Check): string {
  const v = checkValue({ name: c.name, actual: c.threshold });
  return c.direction === "min" ? `at least ${v}` : `at most ${v}`;
}

export const DECISION: Record<Decision, { word: string; tone: "go" | "fix" | "stop" }> = {
  ship: { word: "Cleared", tone: "go" },
  ship_with_fix: { word: "With fix", tone: "fix" },
  dont_ship: { word: "Held", tone: "stop" },
};

/** The short reason a candidate was held, built from its failing checks. */
export function heldBecause(checks: Check[]): string {
  const failed = checks.filter((c) => c.passed === false);
  if (!failed.length) return "Every check passed";
  return failed.map((c) => `${c.name} ${checkValue(c)}`).join(", ");
}

export function endpointOf(model: string): string {
  return model.endsWith("-contributor") ? "Contributor endpoint" : "Standard endpoint";
}

export function versionOf(model: string): string {
  return model.replace(/^muse-spark-/, "").replace(/-contributor$/, "");
}

export const prettyDate = (iso: string) =>
  new Date(iso).toLocaleString("en-GB", {
    day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC",
  }) + " UTC";

/** 95% Wilson score interval for k successes out of n. */
export function wilson(k: number, n: number, z = 1.96): [number, number] {
  if (n === 0) return [0, 1];
  const p = k / n;
  const d = 1 + (z * z) / n;
  const c = (p + (z * z) / (2 * n)) / d;
  const h = (z * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n))) / d;
  return [Math.max(0, c - h), Math.min(1, c + h)];
}
