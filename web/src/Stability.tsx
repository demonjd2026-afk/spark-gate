import type { RunSummary } from "./types";
import { DECISION, endpointOf, pct, prettyDate, versionOf, wilson } from "./format";

/** The same suite replayed in separate runs: does the verdict hold, and how wide is the uncertainty? */
export function Stability({ runs, maxLoss }: { runs: RunSummary[]; maxLoss: number }) {
  if (runs.length < 2) return null;
  const ordered = [...runs].sort((a, b) => a.created_at.localeCompare(b.created_at));
  const models = Object.keys(ordered[ordered.length - 1].candidates);
  const spend = ordered.reduce((s, r) => s + r.total_cost_usd, 0);

  return (
    <section id="stability" className="section" aria-labelledby="stab-title">
      <header className="section-head">
        <h2 id="stab-title">Is the verdict stable, or just noise?</h2>
        <p>
          24 conversations is a small sample, so one or two answers can move a rate by several points. The whole
          suite was run {ordered.length} separate times. A verdict only means something if it holds across runs, and
          the pooled range shows how sure each number is.
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
                  <span className="stab-date">{prettyDate(r.created_at)}</span>
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
              const n = rows.reduce((s, c) => s + c.traces, 0);
              const [lo, hi] = wilson(losses, n);
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
                        <span className="stab-loss">{pct(c.quality_loss_rate)} worse</span>
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
                      {pct(losses / n)} judged worse, likely between {pct(lo)} and {pct(hi)}
                      {hi < maxLoss ? ", safely under the limit" : lo > maxLoss ? ", clearly over the limit" : ", the limit is inside this range"}
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
