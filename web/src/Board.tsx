import { useEffect, useRef, useState } from "react";
import type { RunReport, RunSummary } from "./types";
import { DECISION, endpointOf, heldBecause, ratio, versionOf } from "./format";

const GLYPHS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.×";

function useReducedMotion() {
  const [reduced, setReduced] = useState(
    () => typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const on = () => setReduced(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return reduced;
}

/** A row of split-flap tiles that riffles through glyphs before settling on `text`. */
function Flaps({
  text, width, delay, armed, className = "",
}: { text: string; width: number; delay: number; armed: boolean; className?: string }) {
  const target = text.toUpperCase().padEnd(width, " ").slice(0, width);
  const reduced = useReducedMotion();
  const [shown, setShown] = useState(reduced ? target : " ".repeat(width));

  useEffect(() => {
    if (reduced) {
      setShown(target);
      return;
    }
    if (!armed) return;
    let frame = 0;
    let timer: number | undefined;
    const start = window.setTimeout(() => {
      timer = window.setInterval(() => {
        frame += 1;
        const settled = Math.floor(frame / 2);
        setShown(
          Array.from(target, (ch, i) =>
            i < settled || ch === " " ? ch : GLYPHS[Math.floor(Math.random() * GLYPHS.length)],
          ).join(""),
        );
        if (settled >= width) window.clearInterval(timer);
      }, 38);
    }, delay);
    return () => {
      window.clearTimeout(start);
      window.clearInterval(timer);
    };
  }, [target, width, delay, reduced, armed]);

  return (
    <span className={`flaps ${className}`} aria-label={text}>
      {Array.from(shown, (ch, i) => (
        <span className="flap" aria-hidden="true" key={i}>
          {ch === " " ? " " : ch}
        </span>
      ))}
    </span>
  );
}

export function Board({
  report,
  runs,
  selected,
  onSelect,
}: {
  report: RunReport;
  runs: RunSummary[];
  selected: string;
  onSelect: (model: string) => void;
}) {
  const record = (m: string) => {
    const seen = runs.map((r) => r.candidates[m]?.decision).filter(Boolean);
    if (seen.length < 2) return "";
    const same = seen.filter((d) => d === report.verdicts.find((v) => v.candidate_model === m)?.decision).length;
    return `Same verdict in ${same} of ${seen.length} runs`;
  };
  // Status tiles are only as wide as the longest verdict word on this board.
  const statusTiles = Math.max(4, ...report.verdicts.map((v) => DECISION[v.decision].word.length));
  const costOf = (m: string) => report.metrics.find((x) => x.model === m && x.variant === "original")?.cost_ratio;

  // The tiles flip when the board first scrolls into view, not on page load.
  const ref = useRef<HTMLElement>(null);
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || armed) return;
    if (!("IntersectionObserver" in window)) {
      setArmed(true);
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setArmed(true);
          io.disconnect();
        }
      },
      { threshold: 0.35 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [armed]);

  return (
    <section
      ref={ref}
      className="board"
      aria-labelledby="board-title"
      style={{ ["--status-tiles" as string]: statusTiles }}
    >
      <div className="board-head">
        <h2 id="board-title" className="board-title">
          Departures from {report.baseline_model}
        </h2>
        <p className="board-sub">Select a candidate to inspect its evidence below.</p>
      </div>

      <div className="board-grid" role="table" aria-label="Gate verdict per candidate">
        <div className="board-row board-labels" role="row">
          <span role="columnheader">Version</span>
          <span role="columnheader">Endpoint</span>
          <span role="columnheader">Cost vs today</span>
          <span role="columnheader">Status</span>
        </div>

        {report.verdicts.map((v, i) => {
          const d = DECISION[v.decision];
          const isSel = v.candidate_model === selected;
          return (
            <button
              type="button"
              role="row"
              key={v.candidate_model}
              className={`board-row board-entry tone-${d.tone}${isSel ? " is-selected" : ""}`}
              onClick={() => onSelect(v.candidate_model)}
              aria-pressed={isSel}
            >
              <span role="cell" className="c-version">
                <Flaps text={`Spark ${versionOf(v.candidate_model)}`} width={9} delay={150 + i * 220} armed={armed} />
              </span>
              <span role="cell" className="c-endpoint">
                <Flaps
                  text={endpointOf(v.candidate_model).replace(" endpoint", "")}
                  width={11}
                  delay={250 + i * 220}
                  armed={armed}
                  className="flaps-dim"
                />
              </span>
              <span role="cell" className="c-cost">
                <Flaps text={ratio(costOf(v.candidate_model))} width={5} delay={350 + i * 220} armed={armed} />
              </span>
              <span role="cell" className="status-cell">
                <span className={`lamp lamp-${d.tone}`} aria-hidden="true" />
                <Flaps text={d.word} width={statusTiles} delay={450 + i * 220} armed={armed} className={`flaps-${d.tone}`} />
              </span>
              <span className="board-remark" role="cell">
                {heldBecause(v.checks)}
                {record(v.candidate_model) && <span className="board-record">{record(v.candidate_model)}</span>}
              </span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
