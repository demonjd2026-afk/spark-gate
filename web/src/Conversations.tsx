import { useMemo } from "react";
import type { Conversation, Diff, Replay, RunReport } from "./types";
import { seconds, usd, versionOf } from "./format";

type Outcome = "same" | "changed" | "worse" | "better";

function outcomeOf(d: Diff | undefined): Outcome {
  if (!d) return "same";
  if (d.regression_codes.length || d.quality === "baseline_better") return "worse";
  if (d.quality === "candidate_better") return "better";
  if (d.tool_fidelity !== "exact") return "changed";
  return "same";
}

const OUTCOME_TEXT: Record<Outcome, string> = {
  same: "Same as today",
  changed: "Changed, not worse",
  worse: "Regression",
  better: "Judged better",
};

function parseAnswer(text: string): Record<string, unknown> | null {
  try {
    const v = JSON.parse(text);
    return v && typeof v === "object" ? (v as Record<string, unknown>) : null;
  } catch {
    const m = text.match(/\{[\s\S]*\}/);
    if (!m) return null;
    try {
      return JSON.parse(m[0]);
    } catch {
      return null;
    }
  }
}

function argText(args: Record<string, unknown>) {
  return Object.entries(args)
    .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(", ");
}

function RunColumn({
  title,
  replay,
  highlight,
}: {
  title: string;
  replay: Replay | undefined;
  highlight: Set<string>;
}) {
  if (!replay) return <div className="run-col"><h4>{title}</h4><p className="empty">No run recorded.</p></div>;
  const answer = parseAnswer(replay.final_text);
  return (
    <div className="run-col">
      <h4>{title}</h4>
      <ol className="steps">
        {replay.calls.map((c, i) => (
          <li key={i} className={highlight.has(c.name) ? "step is-diff" : "step"}>
            <span className="step-name">{c.name}</span>
            <span className="step-args">{argText(c.arguments)}</span>
          </li>
        ))}
        {replay.calls.length === 0 && <li className="step step-none">No tools called</li>}
      </ol>
      <div className="answer">
        {answer ? (
          <>
            <p className="answer-action">
              <span>{String(answer.action ?? "no action")}</span>
              {answer.refund_amount != null && <span>refund ${String(answer.refund_amount)}</span>}
              {answer.ticket_id != null && <span>ticket {String(answer.ticket_id)}</span>}
            </p>
            {typeof answer.customer_reply === "string" && <p className="answer-reply">{answer.customer_reply}</p>}
          </>
        ) : (
          <p className="answer-raw">{replay.final_text || replay.error || "No final answer"}</p>
        )}
      </div>
      <p className="run-meta">
        {seconds(replay.latency_ms)} and {usd(replay.usage.cost_usd)}
      </p>
    </div>
  );
}

export function Conversations({
  report,
  conversations,
  selected,
  openTrace,
  onOpenTrace,
}: {
  report: RunReport;
  conversations: Record<string, Conversation>;
  selected: string;
  openTrace: string;
  onOpenTrace: (id: string) => void;
}) {
  const diffs = useMemo(() => {
    const m = new Map<string, Diff>();
    report.diffs
      .filter((d) => d.candidate_model === selected && d.variant === "original")
      .forEach((d) => m.set(d.trace_id, d));
    return m;
  }, [report, selected]);

  const replay = (trace: string, model: string) =>
    report.replays.find((r) => r.trace_id === trace && r.model === model && r.variant === "original");

  const ids = useMemo(() => {
    const all = Array.from(new Set(report.diffs.map((d) => d.trace_id)));
    const rank: Record<Outcome, number> = { worse: 0, changed: 1, better: 2, same: 3 };
    return all.sort((a, b) => rank[outcomeOf(diffs.get(a))] - rank[outcomeOf(diffs.get(b))] || a.localeCompare(b));
  }, [report, diffs]);

  const current = ids.includes(openTrace) ? openTrace : ids[0];
  const d = diffs.get(current);
  const conv = conversations[current];
  const changedTools = new Set([...(d?.missing_tools ?? []), ...(d?.extra_tools ?? [])]);
  const counts = ids.reduce(
    (acc, id) => ({ ...acc, [outcomeOf(diffs.get(id))]: acc[outcomeOf(diffs.get(id))] + 1 }),
    { same: 0, changed: 0, worse: 0, better: 0 } as Record<Outcome, number>,
  );

  return (
    <section id="conversations" className="section" aria-labelledby="conv-title">
      <header className="section-head">
        <h2 id="conv-title">Every conversation, side by side</h2>
        <p>
          Spark {versionOf(selected)} against today on each recorded customer. {counts.worse} regressed,{" "}
          {counts.changed} changed without getting worse, {counts.better} were judged better and {counts.same} matched.
        </p>
      </header>

      <div className="conv-layout">
        <nav className="conv-list" aria-label="Conversations">
          <ul>
            {ids.map((id) => {
              const o = outcomeOf(diffs.get(id));
              return (
                <li key={id}>
                  <button
                    type="button"
                    className={`conv-item${id === current ? " is-open" : ""}`}
                    onClick={() => onOpenTrace(id)}
                    aria-current={id === current ? "true" : undefined}
                  >
                    <span className={`dot dot-${o}`} aria-hidden="true" />
                    <span className="conv-id">{id}</span>
                    <span className="sr-only">{OUTCOME_TEXT[o]}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        </nav>

        <article className="conv-detail" aria-live="polite">
          <div className="conv-top">
            <h3>{current}</h3>
            <span className={`outcome outcome-${outcomeOf(d)}`}>{OUTCOME_TEXT[outcomeOf(d)]}</span>
          </div>
          {conv && <blockquote className="customer">{conv.user}</blockquote>}

          {d && (
            <ul className="findings">
              {d.regression_codes.map((c) => (
                <li key={c} className="finding bad">{c.replace(/_/g, " ").replace(":", ": ")}</li>
              ))}
              {d.candidate_policy_violations.map((c) => (
                <li key={c} className="finding bad">Rule broken: {c}</li>
              ))}
              {d.schema_errors.map((c) => (
                <li key={c} className="finding bad">Answer format: {c}</li>
              ))}
              {d.arg_changes.map((c) => (
                <li key={c} className="finding">{c}</li>
              ))}
            </ul>
          )}

          <div className="runs">
            <RunColumn title={`Today: ${report.baseline_model}`} replay={replay(current, report.baseline_model)} highlight={new Set()} />
            <RunColumn title={`Candidate: ${selected}`} replay={replay(current, selected)} highlight={changedTools} />
          </div>

          {d && d.quality !== "not_judged" && (
            <p className="judge">
              <strong>Judge, both orderings:</strong> {d.quality_reason}
            </p>
          )}
        </article>
      </div>
    </section>
  );
}
