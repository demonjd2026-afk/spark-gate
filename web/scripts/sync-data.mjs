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

  // A small per-run summary, so the site can compare verdicts across runs without
  // downloading every full report.
  const candidates = {};
  for (const v of report.verdicts) {
    const m = report.metrics.find((x) => x.model === v.candidate_model && x.variant === "original") ?? {};
    const diffs = report.diffs.filter((d) => d.candidate_model === v.candidate_model && d.variant === "original");
    // Counts use the same denominators as the gate (src/sparkgate/gate.py): every replayed conversation.
    candidates[v.candidate_model] = {
      decision: v.decision,
      quality_loss_rate: m.quality_loss_rate ?? null,
      tool_fidelity_rate: m.tool_fidelity_rate ?? null,
      policy_pass_rate: m.policy_pass_rate ?? null,
      cost_ratio: m.cost_ratio ?? null,
      losses: diffs.filter((d) => d.quality === "baseline_better").length,
      fidelity_ok: diffs.filter((d) => d.candidate_status === "ok" && d.tool_fidelity !== "sequence_changed").length,
      traces: diffs.length,
    };
  }
  runs.push({
    run_id: report.run_id,
    created_at: report.created_at,
    baseline: report.baseline_model,
    repeats: report.repeats ?? 1,
    total_cost_usd: report.total_cost_usd,
    candidates,
  });
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
