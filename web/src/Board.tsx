import { useEffect, useState } from "react";
import type { RunReport } from "./types";
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
function Flaps({ text, width, delay, className = "" }: { text: string; width: number; delay: number; className?: string }) {
  const target = text.toUpperCase().padEnd(width, " ").slice(0, width);
  const reduced = useReducedMotion();
  const [shown, setShown] = useState(reduced ? target : " ".repeat(width));

  useEffect(() => {
    if (reduced) {
      setShown(target);
      return;
    }
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
  }, [target, width, delay, reduced]);

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
  selected,
  onSelect,
}: {
  report: RunReport;
  selected: string;
  onSelect: (model: string) => void;
}) {
  const costOf = (m: string) => report.metrics.find((x) => x.model === m && x.variant === "original")?.cost_ratio;

  return (
    <section className="board" aria-labelledby="board-title">
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
              <span role="cell">
                <Flaps text={`Spark ${versionOf(v.candidate_model)}`} width={9} delay={150 + i * 220} />
              </span>
              <span role="cell">
                <Flaps
                  text={endpointOf(v.candidate_model).replace(" endpoint", "")}
                  width={11}
                  delay={250 + i * 220}
                  className="flaps-dim"
                />
              </span>
              <span role="cell">
                <Flaps text={ratio(costOf(v.candidate_model))} width={5} delay={350 + i * 220} />
              </span>
              <span role="cell" className="status-cell">
                <span className={`lamp lamp-${d.tone}`} aria-hidden="true" />
                <Flaps text={d.word} width={8} delay={450 + i * 220} className={`flaps-${d.tone}`} />
              </span>
              <span className="board-remark" role="cell">
                {heldBecause(v.checks)}
              </span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
