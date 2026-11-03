// Copies the gate reports committed in ../results and the recorded customer messages
// in ../traces into public/data, so the static site shows exactly what the gate produced.
import { readFileSync, writeFileSync, mkdirSync, readdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const out = join(root, "web", "public", "data");
mkdirSync(out, { recursive: true });

const resultsDir = join(root, "results");
const runs = [];
for (const name of readdirSync(resultsDir).sort()) {
  if (!name.endsWith(".json") || name === "latest.json" || name.startsWith("calibration-")) continue;
  const report = JSON.parse(readFileSync(join(resultsDir, name), "utf8"));
  if (!report.run_id || !report.verdicts) continue;
  writeFileSync(join(out, `run-${report.run_id}.json`), JSON.stringify(report));
  runs.push({ run_id: report.run_id, created_at: report.created_at, baseline: report.baseline_model });
}
runs.sort((a, b) => b.created_at.localeCompare(a.created_at));

let calibration = null;
try {
  calibration = JSON.parse(readFileSync(join(resultsDir, "calibration-latest.json"), "utf8"));
} catch { /* optional */ }

const conversations = {};
try {
  for (const line of readFileSync(join(root, "traces", "support_agent.jsonl"), "utf8").split("\n")) {
    if (!line.trim()) continue;
    const t = JSON.parse(line);
    conversations[t.id] = { user: t.user, tags: t.tags ?? [], expectations: t.expectations ?? null };
  }
} catch { /* optional */ }

writeFileSync(join(out, "index.json"), JSON.stringify({ runs, calibration, conversations }));
console.log(`synced ${runs.length} run(s), calibration=${!!calibration}, ${Object.keys(conversations).length} conversations`);
