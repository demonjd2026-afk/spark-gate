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
