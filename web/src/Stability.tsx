import type { RunSummary } from "./types";
import { DECISION, endpointOf, pct, prettyDate, versionOf, wilson } from "./format";

/** The same suite replayed in separate runs: does the verdict hold, and how wide is the uncertainty? */
export function Stability({ runs, maxLoss }: { runs: RunSummary[]; maxLoss: number }) {
  if (runs.length < 2) return null;
  const ordered = [...runs].sort((a, b) => a.created_at.localeCompare(b.created_at));
  const models = Object.keys(ordered[ordered.length - 1].candidates);
  const spend = ordered.reduce((s, r) => s + r.total_cost_usd, 0);
  const replays = ordered.reduce((s, r) => s + (r.repeats || 1), 0);
  const singles = ordered.filter((r) => (r.repeats || 1) === 1).length;

  return (
    <section id="stability" className="section" aria-labelledby="stab-title">
      <header className="section-head">
        <h2 id="stab-title">Is the verdict stable, or just noise?</h2>
        <p>
          Reasoning models are not deterministic even at temperature 0, and 24 conversations is a small sample: one
          or two answers move a rate by several points. The suite was run {ordered.length} separate times
          {singles > 0 && singles < ordered.length
            ? `, ${singles} with a single replay per conversation and ${ordered.length - singles} with several`
            : ""}
          , {replays} replays of every conversation in all. Single replays disagreed with each other, which is why the
          gate now pools several replays before it decides. The last column shows the range the evidence supports.
        </p>
      </header>

      <div className="stab-wrap" tabIndex={0} aria-label="Verdicts per run, scrollable">
        <table className="stab">
          <caption className="sr-only">Gate verdict and quality loss for each candidate in each run</caption>
          <thead>
            <tr>
              <th scope="col">Candidate</th>
              {ordered.map((r, i) => (
                <th scope="col" key={r.run_id}>
                  Run {i + 1}
                  <span className="stab-date">
                    {prettyDate(r.created_at)}
                    {r.repeats > 1 ? ` · ${r.repeats} replays each` : ""}
                  </span>
                </th>
              ))}
              <th scope="col">Across all runs</th>
            </tr>
          </thead>
          <tbody>
            {models.map((m) => {
              const rows = ordered.map((r) => r.candidates[m]).filter(Boolean);
              const held = rows.filter((c) => c.decision === "dont_ship").length;
              const losses = rows.reduce((s, c) => s + c.losses, 0);
              const wins = rows.reduce((s, c) => s + (c.wins ?? 0), 0);
              const fidOk = rows.reduce((s, c) => s + c.fidelity_ok, 0);
              const n = rows.reduce((s, c) => s + c.traces, 0);
              const net = (losses - wins) / n;
              const [lo, hi] = wilson(fidOk, n);
              const same = rows.every((c) => c.decision === rows[0].decision);
              return (
                <tr key={m}>
                  <th scope="row">
                    Spark {versionOf(m)}
                    <span className="stab-endpoint">{endpointOf(m)}</span>
                  </th>
                  {ordered.map((r) => {
                    const c = r.candidates[m];
                    if (!c) return <td key={r.run_id} className="cell-na">–</td>;
                    const d = DECISION[c.decision];
                    return (
                      <td key={r.run_id}>
                        <span className={`mini tone-${d.tone}`}>{d.word}</span>
                        <span className="stab-loss">
                          fidelity {pct(c.tool_fidelity_rate)} · net quality {(c.quality_net_loss_rate ?? 0) > 0 ? "+" : ""}{pct(c.quality_net_loss_rate)}
                        </span>
                      </td>
                    );
                  })}
                  <td className="stab-total">
                    <strong>
                      {same
                        ? `${DECISION[rows[0].decision].word} in ${rows.length} of ${rows.length}`
                        : `Held in ${held} of ${rows.length}`}
                    </strong>
                    <span>
                      Tool fidelity {pct(fidOk / n)} over {n} replays, likely between {pct(lo)} and {pct(hi)}.
                      Net quality {net > 0 ? "+" : ""}{pct(net)} ({losses} worse, {wins} better)
                      {net <= maxLoss ? ", within the gate's limit" : ", over the gate's limit"}.
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="fine">
        Ranges are 95% Wilson intervals over every replayed conversation in all {ordered.length} runs. Because every
        run repeats the same 24 customers, these ranges are narrower than a fresh sample would give. Total spend for these runs: ${spend.toFixed(2)}.
      </p>
    </section>
  );
}
