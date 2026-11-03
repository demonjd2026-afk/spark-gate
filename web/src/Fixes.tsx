import type { RunReport } from "./types";
import { checkValue, endpointOf, versionOf, DECISION } from "./format";

export function Fixes({
  report,
  selected,
  onOpenTrace,
}: {
  report: RunReport;
  selected: string;
  onOpenTrace: (traceId: string) => void;
}) {
  const verdict = report.verdicts.find((v) => v.candidate_model === selected)!;
  const patterns = report.patterns.filter((p) => p.candidate_model === selected);
  const d = DECISION[verdict.decision];
  const fixed = verdict.patched_checks.length > 0;

  return (
    <section id="fixes" className="section" aria-labelledby="fixes-title">
      <header className="section-head">
        <h2 id="fixes-title">What broke, and whether a prompt fix helps</h2>
        <p>
          Regressions are grouped by what went wrong. Muse Spark 1.3 explains each group and drafts a short
          addition to the system prompt, which is then replayed on every conversation before it can count.
        </p>
      </header>

      <div className="fixes-layout">
        <div className="fixes-summary">
          <p className="fixes-candidate">
            Spark {versionOf(selected)}
            <span>{endpointOf(selected)}</span>
          </p>
          <p className={`verdict-chip tone-${d.tone}`}>{d.word}</p>
          <p className="fixes-text">{verdict.summary}</p>

          {fixed && (
            <table className="beforeafter">
              <caption>Failing checks, before and after the fix</caption>
              <thead>
                <tr>
                  <th scope="col">Check</th>
                  <th scope="col">Before</th>
                  <th scope="col">With fix</th>
                </tr>
              </thead>
              <tbody>
                {verdict.checks
                  .filter((c) => {
                    const after = verdict.patched_checks.find((p) => p.name === c.name);
                    return c.passed === false || after?.passed === false;
                  })
                  .map((c) => {
                    const after = verdict.patched_checks.find((p) => p.name === c.name)!;
                    return (
                      <tr key={c.name}>
                        <th scope="row">{c.name}</th>
                        <td className={c.passed ? "ok" : "bad"}>{checkValue(c)}</td>
                        <td className={after.passed ? "ok" : "bad"}>{checkValue(after)}</td>
                      </tr>
                    );
                  })}
              </tbody>
            </table>
          )}
        </div>

        <div className="fixes-patterns">
          {patterns.length === 0 && <p className="empty">No regressions were found for this candidate.</p>}
          {patterns.map((p) => {
            const tried = p.suggested_fix != null;
            return (
              <article className="pattern" key={p.id}>
                <div className="pattern-top">
                  <h3>{p.title}</h3>
                  {tried ? (
                    <span className={`pattern-tag ${p.fix_resolved_ids.length === p.trace_ids.length ? "ok" : "bad"}`}>
                      Fix repaired {p.fix_resolved_ids.length} of {p.trace_ids.length}
                    </span>
                  ) : (
                    <span className="pattern-tag muted">No fix drafted</span>
                  )}
                </div>
                <p>{p.description}</p>
                <p className="pattern-traces">
                  Seen in{" "}
                  {p.trace_ids.map((t, i) => (
                    <span key={t}>
                      {i > 0 && ", "}
                      <button type="button" className="linkish" onClick={() => onOpenTrace(t)}>
                        {t}
                      </button>
                    </span>
                  ))}
                </p>
              </article>
            );
          })}

          {verdict.prompt_addendum && (
            <div className="addendum">
              <h3>Proposed prompt addition</h3>
              <pre>{verdict.prompt_addendum}</pre>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
