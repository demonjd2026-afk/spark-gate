// Mirrors the report written by `sparkgate run` (src/sparkgate/schemas.py).

export type Decision = "ship" | "ship_with_fix" | "dont_ship";
export type Quality = "candidate_better" | "tie" | "baseline_better" | "not_judged";
export type Fidelity = "exact" | "args_changed" | "sequence_changed";

export interface Check {
  name: string;
  direction: "min" | "max";
  threshold: number;
  actual: number | null;
  passed: boolean | null;
}

export interface Verdict {
  candidate_model: string;
  decision: Decision;
  checks: Check[];
  patched_checks: Check[];
  prompt_addendum: string | null;
  summary: string;
}

export interface Metrics {
  model: string;
  variant: string;
  traces: number;
  errors: number;
  tool_fidelity_rate: number;
  schema_valid_rate: number;
  policy_pass_rate: number;
  quality_win_rate: number | null;
  quality_loss_rate: number | null;
  quality_net_loss_rate: number | null;
  latency_p95_ratio: number | null;
  cost_ratio: number | null;
  regressions: number;
}

export interface Pattern {
  id: string;
  candidate_model: string;
  code: string;
  title: string;
  description: string;
  trace_ids: string[];
  suggested_fix: string | null;
  fix_rationale: string | null;
  fix_resolved_ids: string[];
  fix_accepted: boolean;
}

export interface Diff {
  trace_id: string;
  candidate_model: string;
  variant: "original" | "patched";
  repeat: number;
  candidate_status: string;
  tool_fidelity: Fidelity;
  baseline_tools: string[];
  candidate_tools: string[];
  missing_tools: string[];
  extra_tools: string[];
  arg_changes: string[];
  changed_side_effect_tools: string[];
  unrecorded_calls: number;
  candidate_policy_violations: string[];
  baseline_schema_valid: boolean;
  candidate_schema_valid: boolean;
  schema_errors: string[];
  quality: Quality;
  quality_reason: string;
  judge_orders_agreed: boolean | null;
  baseline_latency_ms: number;
  candidate_latency_ms: number;
  baseline_cost_usd: number;
  candidate_cost_usd: number;
  regression_codes: string[];
}

export interface ToolCall {
  name: string;
  arguments: Record<string, unknown>;
  result: string | null;
  matched: string;
}

export interface Replay {
  trace_id: string;
  model: string;
  variant: string;
  repeat: number;
  status: string;
  final_text: string;
  calls: ToolCall[];
  latency_ms: number;
  usage: { input_tokens: number; output_tokens: number; cost_usd: number };
  error: string | null;
}

export interface RunReport {
  run_id: string;
  created_at: string;
  baseline_model: string;
  candidate_models: string[];
  n_traces: number;
  repeats: number;
  noise_floor: { model: string; traces: number; tool_sequence_drift_rate: number } | null;
  baseline_policy: { model: string; traces_checked: number; pass_rate: number; failures: Record<string, string[]> } | null;
  metrics: Metrics[];
  diffs: Diff[];
  replays: Replay[];
  patterns: Pattern[];
  verdicts: Verdict[];
  total_cost_usd: number;
}

export interface CalibrationOperator {
  operator: string;
  description: string;
  should_catch: boolean;
  mutants: number;
  caught: number;
  caught_by: Record<string, number>;
}

export interface JudgeAudit {
  model: string;
  degraded_pairs: number;
  degraded_caught: number;
  degraded_inverted: number;
  control_pairs: number;
  control_ties: number;
  asked_pairs: number;
  orders_agreed: number;
  not_judged: number;
  cost_usd: number;
}

export interface Calibration {
  calibration_id: string;
  created_at: string;
  n_traces: number;
  operators: CalibrationOperator[];
  judge: JudgeAudit | null;
}

export interface RunCandidateSummary {
  decision: Decision;
  quality_loss_rate: number | null;
  quality_net_loss_rate: number | null;
  tool_fidelity_rate: number | null;
  policy_pass_rate: number | null;
  cost_ratio: number | null;
  losses: number;
  wins: number;
  fidelity_ok: number;
  traces: number;
}

export interface RunSummary {
  run_id: string;
  created_at: string;
  baseline: string;
  repeats: number;
  total_cost_usd: number;
  candidates: Record<string, RunCandidateSummary>;
}

export interface Conversation {
  user: string;
  tags: string[];
}

export interface DataIndex {
  runs: RunSummary[];
  calibration: Calibration | null;
  conversations: Record<string, Conversation>;
}
