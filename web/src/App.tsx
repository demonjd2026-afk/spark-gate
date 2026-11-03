import { useEffect, useState } from "react";
import type { DataIndex, RunReport } from "./types";
import { pct, prettyDate, usd } from "./format";
import { Board } from "./Board";
import { Evidence } from "./Evidence";
import { Fixes } from "./Fixes";
import { Conversations } from "./Conversations";
import { Trust } from "./Trust";
import { Stability } from "./Stability";

const REPO = "https://github.com/demonjd2026-afk/spark-gate";
const PR = `${REPO}/pull/1`;
const data = (p: string) => `${import.meta.env.BASE_URL}data/${p}`;

const STEPS = [
  ["Record", "Real conversations of the agent on the model it runs on today, with every tool call and result."],
  ["Replay", "The same conversations on each candidate. Tool answers come from the recording, so nothing is refunded twice."],
  ["Compare", "Which tools were called, in what order, with what arguments, and whether the answer still fits the schema."],
  ["Judge", "Muse Spark 1.3 compares old and new answers twice, swapping their order. Only agreement counts."],
  ["Gate", "Seven checks against thresholds in sparkgate.yaml decide: cleared or held."],
  ["Fix", "Regressions are grouped, and Muse Spark drafts a short prompt addition for each group."],
  ["Re-test", "The fix is replayed on every conversation. It only counts if the whole suite passes."],
] as const;

/** A boom barrier: post, lamp, and a lowered striped arm. */
function Mark() {
  return (
    <svg className="mark" viewBox="0 0 32 32" aria-hidden="true">
      <rect className="mark-post" x="4" y="9" width="6" height="20" rx="1.5" />
      <circle className="mark-lamp" cx="7" cy="5" r="3" />
      <rect className="mark-arm" x="9" y="15" width="20" height="5" rx="1.2" />
      <rect className="mark-stripe" x="14" y="15" width="4" height="5" />
      <rect className="mark-stripe" x="22" y="15" width="4" height="5" />
    </svg>
  );
}

export default function App() {
  const [index, setIndex] = useState<DataIndex | null>(null);
  const [runId, setRunId] = useState<string>("");
  const [report, setReport] = useState<RunReport | null>(null);
  const [error, setError] = useState<string>("");
  const [selected, setSelected] = useState<string>("");
  const [openTrace, setOpenTrace] = useState<string>("");

  useEffect(() => {
    fetch(data("index.json"))
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`index.json: HTTP ${r.status}`))))
      .then((ix: DataIndex) => {
        setIndex(ix);
        setRunId(ix.runs[0]?.run_id ?? "");
      })
      .catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    if (!runId) return;
    fetch(data(`run-${runId}.json`))
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`run ${runId}: HTTP ${r.status}`))))
      .then((rep: RunReport) => {
        setReport(rep);
        const firstHeld = rep.verdicts.find((v) => v.decision !== "ship") ?? rep.verdicts[0];
        setSelected(firstHeld?.candidate_model ?? "");
      })
      .catch((e) => setError(String(e)));
  }, [runId]);

  const openTraceAndScroll = (id: string) => {
    setOpenTrace(id);
    document.getElementById("conversations")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const selectCandidate = (m: string) => {
    setSelected(m);
    document.getElementById("evidence")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <>
      <a className="skip" href="#main">Skip to content</a>
      <header className="topbar">
        <div className="topbar-inner">
          <a className="brand" href="#top">
            <Mark />
            <span>Spark Gate</span>
          </a>
          <nav className="topnav" aria-label="Sections">
            <a href="#evidence">Evidence</a>
            <a href="#fixes">Fixes</a>
            <a href="#conversations">Conversations</a>
            {index && index.runs.length > 1 && <a href="#stability">Stability</a>}
            <a href="#trust">Trust</a>
            <a href="#how">How it works</a>
          </nav>
          <div className="topbar-end">
            {index && index.runs.length > 1 && (
              <label className="runpick">
                <span className="sr-only">Gate run</span>
                <select value={runId} onChange={(e) => setRunId(e.target.value)}>
                  {index.runs.map((r) => (
                    <option key={r.run_id} value={r.run_id}>{prettyDate(r.created_at)}</option>
                  ))}
                </select>
              </label>
            )}
            <a className="gh" href={REPO} target="_blank" rel="noreferrer">
              <svg viewBox="0 0 16 16" aria-hidden="true">
                <path d="M8 0C3.58 0 0 3.58 0 8a8 8 0 0 0 5.47 7.59c.4.07.55-.17.55-.38v-1.33c-2.23.48-2.7-1.07-2.7-1.07-.36-.92-.89-1.17-.89-1.17-.73-.5.06-.49.06-.49.8.06 1.23.83 1.23.83.71 1.23 1.87.87 2.33.67.07-.52.28-.87.5-1.07-1.78-.2-3.65-.89-3.65-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.6 7.6 0 0 1 4 0c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48v2.2c0 .21.15.46.55.38A8 8 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
              </svg>
              <span>Source</span>
            </a>
          </div>
        </div>
      </header>

      <main id="main">
        <div id="top" className="hero">
          <div className="hero-copy">
            <h1>Is it safe to upgrade the model under your agent?</h1>
            <p className="lede">
              Spark Gate replays an agent's real conversations on newer Muse Spark versions and on Meta's cheaper
              contributor endpoint, measures exactly what changed, and blocks the pull request when the answer is no.
            </p>
          </div>
          {report && (
            <dl className="hero-facts">
              <div><dt>Agent under test</dt><dd>Outdoor-gear support agent with 5 tools</dd></div>
              <div><dt>Running today on</dt><dd>{report.baseline_model}</dd></div>
              <div>
                <dt>Conversations replayed</dt>
                <dd>
                  {report.n_traces} per candidate
                  {(report.repeats ?? 1) > 1 ? `, ${report.repeats} times each` : ""}
                </dd>
              </div>
              <div><dt>Cost of this run</dt><dd>{usd(report.total_cost_usd)}</dd></div>
              {report.noise_floor && (
                <div>
                  <dt>Baseline noise</dt>
                  <dd>{pct(report.noise_floor.tool_sequence_drift_rate)} of re-runs differ</dd>
                </div>
              )}
              <div><dt>Run</dt><dd>{prettyDate(report.created_at)}</dd></div>
            </dl>
          )}
        </div>

        {error && (
          <p className="error" role="alert">
            The gate results could not be loaded ({error}). Check that results/ contains a report and rebuild the site.
          </p>
        )}
        {!report && !error && <p className="loading">Loading the latest gate run…</p>}

        {report && index && (
          <>
            <Board report={report} runs={index.runs} selected={selected} onSelect={selectCandidate} />
            <Evidence report={report} selected={selected} />
            <Fixes report={report} selected={selected} onOpenTrace={openTraceAndScroll} />
            <Conversations
              report={report}
              conversations={index.conversations}
              selected={selected}
              openTrace={openTrace}
              onOpenTrace={setOpenTrace}
            />
            <Stability
              runs={index.runs}
              maxLoss={report.verdicts[0]?.checks.find((c) => c.name === "quality loss rate")?.threshold ?? 0.1}
            />
            <Trust calibration={index.calibration} />

            <section className="ci" aria-labelledby="ci-title">
              <div>
                <h2 id="ci-title">It runs where the upgrade happens</h2>
                <p>
                  Spark Gate ships as a GitHub Action. When a pull request moved this agent to muse-spark-1.3, the
                  gate replayed all {report.n_traces} conversations, posted its report on the pull request and failed
                  the check: tool-call fidelity 88% and 17% of answers judged worse.
                </p>
              </div>
              <a className="button" href={PR} target="_blank" rel="noreferrer">See the blocked pull request</a>
            </section>

            <section id="how" className="section" aria-labelledby="how-title">
              <header className="section-head">
                <h2 id="how-title">How a verdict is made</h2>
              </header>
              <ol className="steps-strip">
                {STEPS.map(([name, text]) => (
                  <li key={name}>
                    <h3>{name}</h3>
                    <p>{text}</p>
                  </li>
                ))}
              </ol>
            </section>
          </>
        )}
      </main>

      <footer className="footer">
        <div className="footer-inner">
          <div className="footer-brand">
            <a className="brand" href="#top"><Mark /><span>Spark Gate</span></a>
            <p>A CI safety gate for Muse Spark upgrades. Built by Jayanth Dolai for the Meta Global AI Developer Hackathon, Indie track.</p>
          </div>
          <nav className="footer-links" aria-label="Project">
            <a href={REPO} target="_blank" rel="noreferrer">Source code</a>
            <a href={`${REPO}#quickstart`} target="_blank" rel="noreferrer">Quickstart</a>
            <a href={PR} target="_blank" rel="noreferrer">Example pull request</a>
            <a href={`${REPO}/blob/main/LICENSE`} target="_blank" rel="noreferrer">MIT license</a>
          </nav>
          <p className="footer-fine">
            Every number on this page comes from the gate reports committed in the repository{report ? ` (run ${report.run_id})` : ""}.
            Models served by the Meta Model API.
          </p>
        </div>
      </footer>
    </>
  );
}
