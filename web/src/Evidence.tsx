import type { Check, RunReport } from "./types";
import { checkValue, pct, ratio, thresholdText, versionOf, endpointOf } from "./format";

const CHECK_HELP: Record<string, string> = {
  "run errors": "Conversations that failed to finish",
  "tool-call fidelity": "Same tools, in the same order, as today",
  "policy pass rate": "Conversations that follow every written rule",
  "schema validity": "Final answers that match the required JSON",
  "quality loss rate": "Judged worse than today, in both orderings",
  "p95 latency ratio": "Slowest-5% response time against today",
  "cost ratio": "Spend per conversation against today",
};

function Cell({ c }: { c: Check | undefined }) {
  if (!c) return <td className="cell-na">–</td>;
  const state = c.passed == null ? "skip" : c.passed ? "pass" : "fail";
  return (
    <td className={`cell cell-${state}`}>
      <span className="cell-value">{checkValue(c)}</span>
      <span className="cell-mark" aria-label={state === "pass" ? "passed" : state === "fail" ? "failed" : "skipped"}>
        {state === "pass" ? "✓" : state === "fail" ? "✕" : "–"}
      </span>
    </td>
  );
}

function Matrix({ report, selected }: { report: RunReport; selected: string }) {
  const names = report.verdicts[0]?.checks.map((c) => c.name) ?? [];
  return (
    <div className="matrix-wrap" tabIndex={0} aria-label="Check results, scrollable">
      <table className="matrix">
        <caption className="sr-only">Each gate check for each candidate</caption>
        <thead>
          <tr>
            <th scope="col" className="matrix-corner">Check</th>
            {report.verdicts.map((v) => (
              <th
                scope="col"
                key={v.candidate_model}
                className={v.candidate_model === selected ? "is-selected" : undefined}
              >
                <span className="col-version">Spark {versionOf(v.candidate_model)}</span>
                <span className="col-endpoint">{endpointOf(v.candidate_model)}</span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {names.map((name) => {
            const any = report.verdicts[0].checks.find((c) => c.name === name)!;
            return (
              <tr key={name}>
                <th scope="row">
                  <span className="row-name">{name[0].toUpperCase() + name.slice(1)}</span>
                  <span className="row-help">{CHECK_HELP[name] ?? ""}</span>
                  <span className="row-threshold">Must be {thresholdText(any)}</span>
                </th>
                {report.verdicts.map((v) => (
                  <Cell key={v.candidate_model} c={v.checks.find((c) => c.name === name)} />
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Cost against quality: the trade-off a team actually has to make. */
function TradeoffChart({ report, selected }: { report: RunReport; selected: string }) {
  const W = 520, H = 360, L = 58, R = 18, T = 20, B = 52;
  const pts = report.metrics
    .filter((m) => m.variant === "original" && m.cost_ratio != null && m.quality_loss_rate != null)
    .map((m) => ({ model: m.model, x: m.cost_ratio!, y: m.quality_loss_rate! }));
  const xmin = 0.02, xmax = 3;
  const ymax = Math.max(0.3, ...pts.map((p) => p.y + 0.05));
  const sx = (x: number) => L + ((Math.log10(x) - Math.log10(xmin)) / (Math.log10(xmax) - Math.log10(xmin))) * (W - L - R);
  const sy = (y: number) => T + (1 - y / ymax) * (H - T - B);
  const gate = report.verdicts[0]?.checks;
  const maxLoss = gate?.find((c) => c.name === "quality loss rate")?.threshold ?? 0.1;
  const maxCost = gate?.find((c) => c.name === "cost ratio")?.threshold ?? 1.5;
  const decision = (m: string) => report.verdicts.find((v) => v.candidate_model === m)?.decision;
  const xticks = [0.05, 0.1, 0.25, 0.5, 1, 2];
  const yticks = [0, 0.1, 0.2, 0.3].filter((y) => y <= ymax);

  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-labelledby="chart-title chart-desc">
        <title id="chart-title">Cost against quality loss for each candidate</title>
        <desc id="chart-desc">
          {pts.map((p) => `${p.model}: cost ${ratio(p.x)}, judged worse on ${pct(p.y)}`).join("; ")}
        </desc>
        <rect
          className="chart-safe"
          x={L} y={sy(maxLoss)} width={sx(maxCost) - L} height={sy(0) - sy(maxLoss)}
        />
        <text className="chart-safe-label" x={L + 8} y={sy(maxLoss) + 16}>Within the gate</text>
        {yticks.map((y) => (
          <g key={y}>
            <line className="chart-grid" x1={L} x2={W - R} y1={sy(y)} y2={sy(y)} />
            <text className="chart-tick" x={L - 8} y={sy(y) + 4} textAnchor="end">{pct(y)}</text>
          </g>
        ))}
        {xticks.map((x) => (
          <g key={x}>
            <line className="chart-grid" x1={sx(x)} x2={sx(x)} y1={T} y2={H - B} />
            <text className="chart-tick" x={sx(x)} y={H - B + 18} textAnchor="middle">{x}×</text>
          </g>
        ))}
        <text className="chart-axis" x={(L + W - R) / 2} y={H - 10} textAnchor="middle">
          Cost per conversation against today (log scale)
        </text>
        <text className="chart-axis" transform={`translate(16 ${(T + H - B) / 2}) rotate(-90)`} textAnchor="middle">
          Judged worse than today
        </text>

        <g className="chart-point chart-baseline">
          <circle cx={sx(1)} cy={sy(0)} r={6} />
          <text x={sx(1) - 10} y={sy(0) - 10} textAnchor="end">Today</text>
        </g>
        {pts.map((p) => {
          const d = decision(p.model);
          const tone = d === "ship" ? "go" : d === "ship_with_fix" ? "fix" : "stop";
          const isSel = p.model === selected;
          const right = sx(p.x) < W - 150;
          return (
            <g key={p.model} className={`chart-point tone-${tone}${isSel ? " is-selected" : ""}`}>
              {isSel && <circle className="chart-halo" cx={sx(p.x)} cy={sy(p.y)} r={14} />}
              <circle cx={sx(p.x)} cy={sy(p.y)} r={7} />
              <text x={sx(p.x) + (right ? 12 : -12)} y={sy(p.y) + 4} textAnchor={right ? "start" : "end"}>
                Spark {versionOf(p.model)}{p.model.endsWith("-contributor") ? " contributor" : ""}
              </text>
            </g>
          );
        })}
      </svg>
      <figcaption>
        The cheapest endpoints save over 90% per conversation, but the judge rated more of their answers
        worse than today. The shaded area is what the gate allows.
      </figcaption>
    </figure>
  );
}

export function Evidence({ report, selected }: { report: RunReport; selected: string }) {
  return (
    <section id="evidence" className="section" aria-labelledby="evidence-title">
      <header className="section-head">
        <h2 id="evidence-title">The evidence</h2>
        <p>
          Each candidate replayed the same {report.n_traces} recorded conversations. Tool answers came from the
          recording, so every model saw the same orders, the same refund rules and the same customers.
        </p>
      </header>
      <div className="evidence-grid">
        <Matrix report={report} selected={selected} />
        <TradeoffChart report={report} selected={selected} />
      </div>
    </section>
  );
}
