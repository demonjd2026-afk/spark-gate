import type { Calibration } from "./types";
import { pct, prettyDate } from "./format";

export function Trust({ calibration }: { calibration: Calibration | null }) {
  if (!calibration) return null;
  const control = calibration.operators.find((o) => !o.should_catch && o.operator === "unchanged");
  const faults = calibration.operators.filter((o) => o.should_catch);
  const blind = calibration.operators.filter((o) => !o.should_catch && o.operator !== "unchanged");
  const total = faults.reduce((s, o) => s + o.mutants, 0);
  const caught = faults.reduce((s, o) => s + o.caught, 0);

  return (
    <section id="trust" className="section" aria-labelledby="trust-title">
      <header className="section-head">
        <h2 id="trust-title">Can you trust the gate?</h2>
        <p>
          Before trusting a verdict, Spark Gate tests itself. It copies each of the {calibration.n_traces} recorded
          conversations, plants one known mistake in the copy, and checks whether the gate notices. No model calls,
          no cost.
        </p>
      </header>

      <div className="trust-headline">
        <p>
          <span className="big">{pct(caught / total)}</span>
          of {total} planted mistakes caught
        </p>
        {control && (
          <p>
            <span className="big">{pct(control.caught / control.mutants)}</span>
            false alarms on {control.mutants} untouched copies
          </p>
        )}
      </div>

      <table className="trust-table">
        <caption className="sr-only">Detection rate by planted mistake</caption>
        <thead>
          <tr>
            <th scope="col">Planted mistake</th>
            <th scope="col">Copies</th>
            <th scope="col">Caught</th>
            <th scope="col">Caught by</th>
          </tr>
        </thead>
        <tbody>
          {faults.map((o) => (
            <tr key={o.operator}>
              <th scope="row">{o.description}</th>
              <td>{o.mutants}</td>
              <td>
                <span className="bar" style={{ ["--w" as string]: `${(o.caught / o.mutants) * 100}%` }}>
                  {pct(o.caught / o.mutants)}
                </span>
              </td>
              <td className="caught-by">
                {Object.entries(o.caught_by)
                  .map(([k, v]) => `${k} (${v})`)
                  .join(", ")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {calibration.judge && calibration.judge.asked_pairs > 0 && (
        <div className="judge-audit">
          <h3>And the judge itself?</h3>
          <p>
            Quality is the one check a model decides, so the judge ({calibration.judge.model}) was tested too: each
            real answer was paired with a wrong one taken from another customer, and with a reformatted copy of itself.
          </p>
          <dl className="audit-facts">
            <div>
              <dt>Picked the real answer over a wrong one</dt>
              <dd>{calibration.judge.degraded_caught} of {calibration.judge.degraded_pairs}</dd>
            </div>
            <div>
              <dt>Picked the wrong answer</dt>
              <dd>{calibration.judge.degraded_inverted} of {calibration.judge.degraded_pairs}</dd>
            </div>
            <div>
              <dt>Called a reformatted copy a tie</dt>
              <dd>{calibration.judge.control_ties} of {calibration.judge.control_pairs}</dd>
            </div>
            <div>
              <dt>Both orderings agreed</dt>
              <dd>{calibration.judge.orders_agreed} of {calibration.judge.asked_pairs}</dd>
            </div>
          </dl>
        </div>
      )}

      {blind.length > 0 && (
        <p className="blind">
          Known blind spot, reported rather than hidden: {blind.map((b) => b.description.toLowerCase()).join(", ")}.
          Deterministic checks can't see it, so only the judge can.
        </p>
      )}
      <p className="fine">Calibration run {calibration.calibration_id}, {prettyDate(calibration.created_at)}.</p>
    </section>
  );
}
